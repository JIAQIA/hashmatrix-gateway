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

# 共享原语（HTTP/取 token/回显头解析）抽到 _gwlib，与 cluster_e2e.py 单一事实源、不重复。
from _gwlib import get_token, header_value, http, wait_for

GW = os.environ.get("GATEWAY_URL", "http://apisix:9080")
KC = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080")
REALM = os.environ.get("REALM", "hashmatrix")
CLIENT = os.environ.get("CLIENT_ID", "apisix")
TOKEN_URL = f"{KC}/realms/{REALM}/protocol/openid-connect/token"
DISCOVERY = f"{KC}/realms/{REALM}/.well-known/openid-configuration"

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
    token = get_token(TOKEN_URL, CLIENT, "alice", "Passw0rd!")
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
    token2 = get_token(TOKEN_URL, CLIENT, "bob", "Passw0rd!")
    code, body = http("GET", f"{GW}/api/headers",
                      headers={"Authorization": f"Bearer {token2}"})
    org2 = header_value(json.loads(body).get("headers", {}), "X-Tenant-Org") if code == 200 else None
    check("第二租户 X-Tenant-Org=tenant-demo", org2 == "tenant-demo", f"got {org2!r}")

    # 4) 多 membership + 已选定活动 org（carol：organization=[acme, tenant-demo]、active_organization=acme）
    #    → 解析到单一活动租户 acme、放行 200（修订后 ICD §3.4：活动 org 优先，不再「多 org 一律 403」）
    token_carol = get_token(TOKEN_URL, CLIENT, "carol", "Passw0rd!")
    code, body = http("GET", f"{GW}/api/headers",
                      headers={"Authorization": f"Bearer {token_carol}"})
    check("多 membership 携 active_organization 放行 200（非 403）", code == 200, f"got {code}")
    headers_c = json.loads(body).get("headers", {}) if code == 200 else {}
    tid_c = header_value(headers_c, "X-Tenant-Id")
    org_c = header_value(headers_c, "X-Tenant-Org")
    check("活动 org 优先：X-Tenant-Id=acme（非 tenant-demo）", tid_c == "acme", f"got {tid_c!r}")
    check("X-Tenant-Org 同为活动 org acme", org_c == "acme", f"got {org_c!r}")

    # 5) 多 membership 且无活动声明（dave：organization=[acme, tenant-demo]、无 active_organization）
    #    → 不可判定唯一活动租户 → 边缘 fail-closed 403（绝不静默挑选）
    token_dave = get_token(TOKEN_URL, CLIENT, "dave", "Passw0rd!")
    code, _ = http("GET", f"{GW}/api/headers",
                   headers={"Authorization": f"Bearer {token_dave}"})
    check("多 membership 无 active_organization → fail-closed 403", code == 403, f"got {code}")

    # 6) 安全：客户端伪造 X-Userinfo 无效（openid-connect 用验签结果覆盖）
    forged = base64.b64encode(
        json.dumps({"organization": "evil-tenant", "sub": "attacker"}).encode()).decode()
    code, body = http("GET", f"{GW}/api/headers",
                      headers={"Authorization": f"Bearer {token}", "X-Userinfo": forged})
    org = header_value(json.loads(body).get("headers", {}), "X-Tenant-Org") if code == 200 else None
    check("伪造 X-Userinfo 被 openid-connect 覆盖（非 evil-tenant）", org == "acme", f"got {org!r}")

    # 7) 负路径：无效 token → 401
    code, _ = http("GET", f"{GW}/api/headers", headers={"Authorization": "Bearer not-a-valid-jwt"})
    check("无效 token 返回 401", code == 401, f"got {code}")

    # 8) 按租户限流：alice 在 /ratelimit (2/60s) 第 3 次 → 429；bob 独立配额仍 200
    codes = [http("GET", f"{GW}/ratelimit/get",
                  headers={"Authorization": f"Bearer {token}"})[0] for _ in range(3)]
    check("同租户超限触发 429（每租户独立配额生效）",
          codes[0] == 200 and codes[1] == 200 and codes[2] == 429, f"codes={codes}")
    code_bob, _ = http("GET", f"{GW}/ratelimit/get", headers={"Authorization": f"Bearer {token2}"})
    check("不同租户配额互不影响（bob 仍 200）", code_bob == 200, f"got {code_bob}")

    # 9) admin 路由仍验签：未登录 / 坏 token → 401
    #    守护「openid-connect 经 plugin_config 在 admin 路由依然生效」这一安全前提——
    #    若误把本路由改成不绑 plugin_config_id（只留 require_tenant=false），将退化为无验签放行，本用例可发现。
    code, _ = http("GET", f"{GW}/admin/headers")
    check("admin 路由无 token → 401（OIDC 仍验签）", code == 401, f"got {code}")
    code, _ = http("GET", f"{GW}/admin/headers", headers={"Authorization": "Bearer not-a-valid-jwt"})
    check("admin 路由坏 token → 401", code == 401, f"got {code}")

    # 10) admin 平面：superadmin（不绑 org、无租户声明）经 admin 路由放行 200（OIDC 校验通过、不要求租户）；
    #     既不注入租户头，也剥离客户端伪造的 X-Tenant-*（信任根与租户路由一致）
    token_su = get_token(TOKEN_URL, CLIENT, "superadmin", "Passw0rd!")
    code, body = http("GET", f"{GW}/admin/headers", headers={
        "Authorization": f"Bearer {token_su}",
        "X-Tenant-Id": "spoofed-by-client",
    })
    check("superadmin 经 admin 路由放行 200（require_tenant=false）", code == 200, f"got {code}")
    headers_su = json.loads(body).get("headers", {}) if code == 200 else {}
    tid_su = header_value(headers_su, "X-Tenant-Id")
    org_su = header_value(headers_su, "X-Tenant-Org")
    check("admin 平面不注入且剥离 X-Tenant-Id（superadmin 无租户上下文）",
          tid_su is None, f"got {tid_su!r}")
    check("admin 平面不注入 X-Tenant-Org", org_su is None, f"got {org_su!r}")

    # 11) superadmin 打租户隔离路由 → 无 org/tenant 声明 → fail-closed 403（绝不错注租户，符合 ICD §3）
    code, _ = http("GET", f"{GW}/api/headers",
                   headers={"Authorization": f"Bearer {token_su}"})
    check("superadmin 访问租户路由 → fail-closed 403（require_tenant）", code == 403, f"got {code}")

    # 12) privacy 路由：/api/privacy/* 经 auth-tenant 注入 X-Tenant-* 后路由到**独立** privacy-upstream。
    #     守护 M1「privacy 经 gateway 可达 + 租户注入」这条验收的本地回归（结构镜像主仓 chart 的 privacy-api）。
    #     可达性证明的精妙处：privacy-mock 以 `-prefix=/api` 起，只应答 /api/get；
    #     若 priority 失效、被 /api/* 兜底抢走，其 rewrite(^/api/(.*)→/$1) 会把请求改写成 /privacy/get
    #     打到**无 prefix** 的 mock-upstream → 404。故此处拿到 200 即证明 privacy 路由（priority 10）确实命中了 privacy-upstream。
    # 12a) 无 token → fail-closed 401（与其它受保护路由一致）
    code, _ = http("GET", f"{GW}/api/privacy/get")
    check("privacy 路由无 token → 401（fail-closed）", code == 401, f"got {code}")
    # 12b) alice → 200（即证明命中 privacy-upstream），上游收到注入的 X-Tenant-*，客户端伪造头被剥离
    code, body = http("GET", f"{GW}/api/privacy/get", headers={
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": "spoofed-by-client",
    })
    check("privacy 路由 alice token 放行 200（命中 privacy-upstream，非 /api/* 兜底）", code == 200, f"got {code}")
    ph = json.loads(body).get("headers", {}) if code == 200 else {}
    ptid = header_value(ph, "X-Tenant-Id")
    porg = header_value(ph, "X-Tenant-Org")
    check("privacy 上游收到注入的 X-Tenant-Id=acme", ptid == "acme", f"got {ptid!r}")
    check("privacy 上游收到注入的 X-Tenant-Org=acme", porg == "acme", f"got {porg!r}")
    check("privacy 路由客户端伪造 X-Tenant-Id 被剥离", ptid not in ("spoofed-by-client", None), f"got {ptid!r}")

    print()
    if FAILS:
        sys.exit(f"SMOKE FAILED ({len(FAILS)} check(s)): {', '.join(FAILS)}")
    print("SMOKE PASSED ✓")


if __name__ == "__main__":
    main()
