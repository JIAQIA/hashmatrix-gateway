#!/usr/bin/env python3
"""网关测试共享原语（纯标准库，无第三方依赖）。

被 `smoke_test.py`（compose 契约冒烟）与 `cluster_e2e.py`（集群末端 e2e）共用，
避免两处复制 HTTP/取 token/断言逻辑造成漂移——单一事实源。
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def http(method, url, data=None, headers=None, timeout=10):
    """发起一次 HTTP 请求，返回 (status_code, body_text)。HTTPError 也归一为 (code, body)。"""
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def wait_for(name, ok, timeout=240):
    """轮询 ok() 直至为真或超时；超时 fatal 退出。首次起栈/集群就绪可能较慢。"""
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


def reachable(url, timeout=3):
    """轻量探活：URL 是否返回任意 HTTP 状态（用于「集群不可达即跳过」判定）。"""
    try:
        http("GET", url, timeout=timeout)
        return True
    except Exception:
        return False


def get_token(token_url, client_id, user, password):
    """以 password grant 取 access_token（仅用于本地/CI 脱敏 demo 用户）。"""
    code, body = http("POST", token_url, data={
        "grant_type": "password",
        "client_id": client_id,
        "username": user,
        "password": password,
        "scope": "openid",
    })
    if code != 200:
        sys.exit(f"[fatal] token request for {user} failed: {code} {body}")
    return json.loads(body)["access_token"]


def header_value(headers, name):
    """go-httpbin 以 {"Name": ["v"]} 形式回显请求头；兼容字符串与数组两种形态。"""
    v = headers.get(name)
    if isinstance(v, list):
        return v[0] if v else None
    return v


class Checker:
    """累计断言结果：check() 记录单条；finish() 打印总结并按是否有失败决定退出码。

    支持 skip()：记录「条件不具备而跳过」（不计为失败），用于集群未就绪 / 上游 echo 端点未接的场景。
    """

    def __init__(self):
        self.fails = []
        self.skips = []
        self.passed = 0

    def check(self, desc, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {desc}{(' — ' + detail) if detail else ''}")
        if cond:
            self.passed += 1
        else:
            self.fails.append(desc)
        return cond

    def skip(self, desc, reason=""):
        print(f"[SKIP] {desc}{(' — ' + reason) if reason else ''}")
        self.skips.append(desc)

    def finish(self, title="CHECKS"):
        print()
        if self.skips:
            # 醒目列出「未真跑、仅 SKIP」的档——避免 skip 被误当 pass、长期静默显绿。
            print(f"⚠ {title} PENDING（以下档未真跑，仅 SKIP）：")
            for d in self.skips:
                print(f"    - {d}")
        print(f"{title}: {self.passed} passed, {len(self.fails)} failed, {len(self.skips)} skipped")
        if self.fails:
            sys.exit(f"{title} FAILED ({len(self.fails)}): {', '.join(self.fails)}")
        print(f"{title} PASSED ✓")
