#!/usr/bin/env python3
"""网关冒烟测试：断言 401/200 与 X-Tenant-* 头注入（含防伪造）。

纯标准库实现（urllib），无需 curl/jq。默认在 compose 网络内运行：
    docker compose -f docker-compose.local.yml run --rm smoke

环境变量：
    GATEWAY_URL   默认 http://apisix:9080
    KEYCLOAK_URL  默认 http://keycloak:8080
    REALM         默认 hashmatrix
    CLIENT_ID     默认 apisix
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GW = os.environ.get("GATEWAY_URL", "http://apisix:9080")
KC = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080")
REALM = os.environ.get("REALM", "hashmatrix")
CLIENT = os.environ.get("CLIENT_ID", "apisix")
TOKEN_URL = f"{KC}/realms/{REALM}/protocol/openid-connect/token"
DISCOVERY = f"{KC}/realms/{REALM}/.well-known/openid-configuration"


def http(method, url, data=None, headers=None, timeout=10):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_for(name, ok, timeout=240):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if ok():
                print(f"[ready] {name}")
                return
        except Exception:
            pass
        time.sleep(3)
    sys.exit(f"[fatal] timeout waiting for {name}")


def get_token(user, password):
    code, body = http("POST", TOKEN_URL, data={
        "grant_type": "password",
        "client_id": CLIENT,
        "username": user,
        "password": password,
        "scope": "openid",
    })
    if code != 200:
        sys.exit(f"[fatal] token request for {user} failed: {code} {body}")
    return json.loads(body)["access_token"]


def header_value(headers, name):
    """go-httpbin 以 {"Name": ["v"]} 形式回显；兼容字符串与数组。"""
    v = headers.get(name)
    if isinstance(v, list):
        return v[0] if v else None
    return v


FAILS = []


def check(desc, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {desc}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(desc)


def main():
    # 0) 等待依赖就绪
    wait_for("keycloak", lambda: http("GET", DISCOVERY)[0] == 200)
    wait_for("gateway", lambda: http("GET", f"{GW}/public/get")[0] == 200)

    # 1) 无 token → 401
    code, _ = http("GET", f"{GW}/api/headers")
    check("受保护路由无 token 返回 401", code == 401, f"got {code}")

    # 2) 合法 token（alice@acme）→ 200，且上游可见 X-Tenant-*
    token = get_token("alice", "Passw0rd!")
    code, body = http("GET", f"{GW}/api/headers", headers={
        "Authorization": f"Bearer {token}",
        # 客户端尝试伪造租户头——应被网关清除并改写为可信值
        "X-Tenant-Id": "spoofed-by-client",
        "X-Tenant-Org": "spoofed-by-client",
    })
    check("合法 token 放行返回 200", code == 200, f"got {code}")
    headers = json.loads(body).get("headers", {}) if code == 200 else {}
    org = header_value(headers, "X-Tenant-Org")
    tid = header_value(headers, "X-Tenant-Id")
    check("上游收到 X-Tenant-Org=acme", org == "acme", f"got {org!r}")
    check("X-Tenant-Id 非空", bool(tid), f"got {tid!r}")
    check("客户端伪造的租户头被剥离", tid not in ("spoofed-by-client", None)
          and org != "spoofed-by-client", f"id={tid!r} org={org!r}")

    # 3) 第二租户（bob@tenant-demo）→ 隔离正确
    token2 = get_token("bob", "Passw0rd!")
    code, body = http("GET", f"{GW}/api/headers",
                      headers={"Authorization": f"Bearer {token2}"})
    org2 = header_value(json.loads(body).get("headers", {}), "X-Tenant-Org") if code == 200 else None
    check("第二租户 X-Tenant-Org=tenant-demo", org2 == "tenant-demo", f"got {org2!r}")

    # 4) 安全：客户端伪造 X-Userinfo 无效（openid-connect 用验签结果覆盖）
    forged = base64.b64encode(
        json.dumps({"organization": "evil-tenant", "sub": "attacker"}).encode()).decode()
    code, body = http("GET", f"{GW}/api/headers",
                      headers={"Authorization": f"Bearer {token}", "X-Userinfo": forged})
    org = header_value(json.loads(body).get("headers", {}), "X-Tenant-Org") if code == 200 else None
    check("伪造 X-Userinfo 被 openid-connect 覆盖（非 evil-tenant）", org == "acme", f"got {org!r}")

    # 5) 负路径：无效 token → 401
    code, _ = http("GET", f"{GW}/api/headers", headers={"Authorization": "Bearer not-a-valid-jwt"})
    check("无效 token 返回 401", code == 401, f"got {code}")

    # 6) 按租户限流：alice 在 /ratelimit (2/60s) 第 3 次 → 429；bob 独立配额仍 200
    codes = [http("GET", f"{GW}/ratelimit/get",
                  headers={"Authorization": f"Bearer {token}"})[0] for _ in range(3)]
    check("同租户超限触发 429（每租户独立配额生效）",
          codes[0] == 200 and codes[1] == 200 and codes[2] == 429, f"codes={codes}")
    code_bob, _ = http("GET", f"{GW}/ratelimit/get", headers={"Authorization": f"Bearer {token2}"})
    check("不同租户配额互不影响（bob 仍 200）", code_bob == 200, f"got {code_bob}")

    print()
    if FAILS:
        sys.exit(f"SMOKE FAILED ({len(FAILS)} check(s)): {', '.join(FAILS)}")
    print("SMOKE PASSED ✓")


if __name__ == "__main__":
    main()
