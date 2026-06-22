#!/usr/bin/env python3
"""集群末端 e2e（#9 · I2 验收）：经网关 OIDC → 真实 in-cluster governance，回归安全不变量。

本脚本是依赖图的**末端汇聚点**（纯测试、零生产风险）。它依赖：
  · 主仓 hashmatrix#17（gateway chart 装进 ns）—— 已 CLOSED；
  · 主仓 hashmatrix#15（M1 贯通主线 · governance 经网关可达）—— 进行中。
集群未就绪时**优雅 SKIP（exit 0）**，故可先入库、先接 CI 触发位，待 #15 落地即真跑。

两档断言（与 compose 冒烟 scripts/smoke_test.py 同一不变量族，但跑在集群路径）：
  A. 边缘可观测（无需上游回显）：fail-closed 401/403、per-tenant 限流隔离 —— 网关就绪即跑。
  B. 上游回显依赖（需上游回显其收到的请求头）：X-Tenant-* 注入正确性、防伪、
     ICD §8 头名一致性 —— 需 UPSTREAM_ECHO_PATH 指向回显端点；未接则 SKIP（标注 pending #15）。

  ③ privacy 末端（主仓 #29 已落 chart 的 privacy-api 路由 + privacy-upstream）：
     A 档加 privacy 路由边缘 fail-closed；B 档加「privacy 经 gateway 可达 + X-Tenant-* 注入正确」，
     依 PRIVACY_ECHO_PATH（privacy 上游回显端点，形态待 #15/privacy 约定）；未接则 SKIP（pending #15）。

环境变量（脱敏 demo 默认；集群细节随 #15 落地再定）：
  GATEWAY_URL        默认 http://platform-gateway:9080（集群 Service；本地 port-forward 改 http://127.0.0.1:9080）
  KEYCLOAK_URL       默认 http://keycloak:8080（in-cluster；token 的 iss 须与网关 discovery 一致，见 keycloak/README）
  REALM / CLIENT_ID  默认 hashmatrix / apisix
  PROTECTED_PATH     默认 /api/get（受保护路由；fail-closed 检查在边缘返回，不依赖上游存在该路径）
  RATELIMIT_PATH     默认 /ratelimit/get（按租户限流样例；集群 chart 未部署该路由时自动 SKIP 隔离档）
  RATELIMIT_MAX      默认 2（限流阈值；与所测路由的 count 对齐）
  UPSTREAM_ECHO_PATH 默认 空 —— 网关上「回显请求头」的受保护路径。compose 干跑可设 /api/headers；
                     集群接真 governance 的回显端点（形态待 #15 与 governance 约定，回显体须含收到的请求头）。
  PRIVACY_PATH       默认 /api/privacy/get（privacy 受保护路由；边缘 fail-closed 检查不依赖上游存在该路径）
  PRIVACY_ECHO_PATH  默认 空 —— /api/privacy/* 下「privacy 上游回显其收到请求头」的网关路径（形态待 #15/privacy 约定）；
                     未设则 privacy 注入档 SKIP（pending #15）。本地 compose 干跑可设 /api/privacy/get。
"""
import base64
import json
import os
import sys

from _gwlib import Checker, get_token, header_value, http, reachable

# `or 默认` 而非 get(k, 默认)：空字符串环境变量（如 CI 未填的 workflow input）也回退默认。
GW = os.environ.get("GATEWAY_URL") or "http://platform-gateway:9080"
KC = os.environ.get("KEYCLOAK_URL") or "http://keycloak:8080"
REALM = os.environ.get("REALM") or "hashmatrix"
CLIENT = os.environ.get("CLIENT_ID") or "apisix"
TOKEN_URL = f"{KC}/realms/{REALM}/protocol/openid-connect/token"
DISCOVERY = f"{KC}/realms/{REALM}/.well-known/openid-configuration"
PROTECTED = os.environ.get("PROTECTED_PATH") or "/api/get"
RATELIMIT = os.environ.get("RATELIMIT_PATH") or "/ratelimit/get"
RMAX = int(os.environ.get("RATELIMIT_MAX") or "2")
ECHO = (os.environ.get("UPSTREAM_ECHO_PATH") or "").strip()
PRIVACY_PATH = os.environ.get("PRIVACY_PATH") or "/api/privacy/get"
PRIVACY_ECHO = (os.environ.get("PRIVACY_ECHO_PATH") or "").strip()
# REQUIRE_FULL=1：集群就绪后置位——B 档（注入/防伪/头名一致性，#9 验收核心）因条件不具备而跳过即视为 FAIL，
# 防「#15 落地后有人忘设 UPSTREAM_ECHO_PATH → 核心断言从不真跑却长期显绿」。
REQUIRE_FULL = (os.environ.get("REQUIRE_FULL") or "").strip().lower() not in ("", "0", "false", "no")

# ICD §8 头名（唯一事实源 = 主仓 contracts/icd/tenant-context-headers-icd.md §2/§8）：
# 必须 == plugins/tenant-context.lua 的 id_header/org_header/subject_header 默认值
# == starter-tenant TenantProperties 的 header/orgHeader 默认值。
CANON_ID = "X-Tenant-Id"
CANON_ORG = "X-Tenant-Org"
CANON_SUBJECT = "X-Tenant-Subject"


def gated(c, desc, reason):
    """关键档的条件不具备时：REQUIRE_FULL 下记 FAIL（防静默变绿），否则 SKIP（待 #15）。"""
    if REQUIRE_FULL:
        c.check(desc, False, f"{reason}（REQUIRE_FULL 要求该档必跑）")
    else:
        c.skip(desc, reason)


def auth(token, extra=None):
    h = {"Authorization": f"Bearer {token}"}
    if extra:
        h.update(extra)
    return h


def extract_headers(body):
    """从上游回显体解析「它收到的请求头」。兼容 go-httpbin 形态 {"headers": {...}}
    与「顶层即头字典」两种回显；governance 回显端点宜采用前者（与 compose mock 一致）。"""
    try:
        d = json.loads(body)
    except Exception:
        return {}
    if isinstance(d, dict) and isinstance(d.get("headers"), dict):
        return d["headers"]
    return d if isinstance(d, dict) else {}


def main():
    # ── 前置：集群网关不可达 → 整体 SKIP（exit 0），安全地先于 #15 入库 ──
    if not reachable(f"{GW}/public/get"):
        msg = f"集群网关不可达：{GW}（pending hashmatrix#15）"
        if REQUIRE_FULL:
            sys.exit(f"[FAIL] {msg} —— REQUIRE_FULL 置位但集群不可达，拒绝静默通过")
        print(f"[SKIP] {msg}")
        print("       chart 装进 ns + governance 经网关可达后重跑，见 scripts/README.md「集群末端 e2e」。")
        return

    c = Checker()
    if not reachable(DISCOVERY):
        c.check(f"Keycloak discovery 可达（{DISCOVERY}）", False,
                "token 无法获取——检查 in-cluster Keycloak 与 issuer 一致性")
        c.finish("CLUSTER-E2E")
        return

    token_alice = get_token(TOKEN_URL, CLIENT, "alice", "Passw0rd!")
    token_bob = get_token(TOKEN_URL, CLIENT, "bob", "Passw0rd!")
    token_dave = get_token(TOKEN_URL, CLIENT, "dave", "Passw0rd!")
    token_su = get_token(TOKEN_URL, CLIENT, "superadmin", "Passw0rd!")

    # ── A 档：边缘可观测不变量（fail-closed + per-tenant 隔离），无需上游回显 ──
    # fail-closed：以下状态码在网关边缘返回，不依赖上游是否实现 PROTECTED 路径。
    c.check("fail-closed：无 token → 401",
            http("GET", f"{GW}{PROTECTED}")[0] == 401)
    c.check("fail-closed：无效 token → 401",
            http("GET", f"{GW}{PROTECTED}", headers={"Authorization": "Bearer not-a-jwt"})[0] == 401)
    c.check("fail-closed：dave 多 membership 无 active_organization → 403",
            http("GET", f"{GW}{PROTECTED}", headers=auth(token_dave))[0] == 403)
    c.check("fail-closed：superadmin（无 org 声明）打租户路由 → 403",
            http("GET", f"{GW}{PROTECTED}", headers=auth(token_su))[0] == 403)

    # privacy 路由边缘 fail-closed（③ · 边缘可观测，无需上游回显；privacy 路由复用 auth-tenant 链）。
    # no-token/dave 在网关边缘即被 openid-connect/tenant-context 拒绝，状态码不依赖 privacy 上游是否存在；
    # 「privacy 路由确实命中独立 privacy-upstream」由下方 B 档回显或 compose smoke 守护。
    c.check("fail-closed：privacy 路由无 token → 401",
            http("GET", f"{GW}{PRIVACY_PATH}")[0] == 401)
    c.check("fail-closed：privacy 路由 dave 多 membership 无 active_organization → 403",
            http("GET", f"{GW}{PRIVACY_PATH}", headers=auth(token_dave))[0] == 403)

    # per-tenant 限流隔离：集群 chart 仅落 /api（100/60），未部署低阈值样例路由时自动 SKIP。
    # 注意：假定 alice 在 60s 窗口内配额新鲜、且网关单副本（limit-count policy=local 各副本独立计数）。
    # 多副本集群下该档应靠 SKIP 或改用 redis policy 验证——故隔离不变量主要由 compose smoke（单副本）守护。
    codes = [http("GET", f"{GW}{RATELIMIT}", headers=auth(token_alice))[0] for _ in range(RMAX + 1)]
    if 404 in codes:
        # 路由未部署是 chart 的结构性决策（非配置疏漏），故即便 REQUIRE_FULL 也保持 SKIP，仅在 PENDING 区呈现。
        c.skip("per-tenant 限流隔离",
               f"{RATELIMIT} 未部署（集群 chart 仅 /api 100/60）——该不变量由 compose smoke 守护")
    else:
        c.check(f"per-tenant 隔离：同租户超 {RMAX} 次 → 429",
                codes[:RMAX] == [200] * RMAX and codes[RMAX] == 429, f"codes={codes}")
        c.check("per-tenant 隔离：不同租户配额独立（bob 仍 200）",
                http("GET", f"{GW}{RATELIMIT}", headers=auth(token_bob))[0] == 200)

    # ── B 档：上游回显依赖（X-Tenant-* 注入正确性 / 防伪 / ICD §8 头名一致性）──
    if not ECHO:
        gated(c, "X-Tenant-* 注入正确性（alice→acme / bob→tenant-demo / carol(active)→acme）",
              "UPSTREAM_ECHO_PATH 未设——pending governance 回显端点(#15)；compose 干跑可设 /api/headers")
        gated(c, "防伪：客户端伪造 X-Tenant-* / X-Userinfo 被网关剥离/覆盖", "同上（需上游回显）")
        gated(c, "ICD §8 头名一致性：上游收到的就是 canonical X-Tenant-Id/Org", "同上（需上游回显）")
    else:
        code, body = http("GET", f"{GW}{ECHO}", headers=auth(token_alice))
        if code != 200:
            gated(c, "X-Tenant-* 注入正确性 / 防伪 / 头名一致性",
                  f"回显端点 {ECHO} 返回 {code}（期望 200）——检查 UPSTREAM_ECHO_PATH 与 governance 回显实现")
        else:
            h_alice = extract_headers(body)
            c.check("alice → X-Tenant-Id=acme",
                    header_value(h_alice, CANON_ID) == "acme", f"got {header_value(h_alice, CANON_ID)!r}")
            c.check("alice → X-Tenant-Org=acme",
                    header_value(h_alice, CANON_ORG) == "acme", f"got {header_value(h_alice, CANON_ORG)!r}")

            token_carol = get_token(TOKEN_URL, CLIENT, "carol", "Passw0rd!")
            h_bob = extract_headers(http("GET", f"{GW}{ECHO}", headers=auth(token_bob))[1])
            c.check("bob → X-Tenant-Id=tenant-demo（隔离正确）",
                    header_value(h_bob, CANON_ID) == "tenant-demo", f"got {header_value(h_bob, CANON_ID)!r}")
            h_carol = extract_headers(http("GET", f"{GW}{ECHO}", headers=auth(token_carol))[1])
            c.check("carol 多 membership + active=acme → X-Tenant-Id=acme（活动 org 优先）",
                    header_value(h_carol, CANON_ID) == "acme", f"got {header_value(h_carol, CANON_ID)!r}")

            # 防伪：客户端伪造租户头 + 伪造 X-Userinfo，都应被网关剥离/覆盖为可信值。
            forged_userinfo = base64.b64encode(
                json.dumps({"organization": "evil-tenant", "sub": "attacker"}).encode()).decode()
            h_spoof = extract_headers(http("GET", f"{GW}{ECHO}", headers=auth(token_alice, {
                CANON_ID: "spoofed-by-client",
                CANON_ORG: "spoofed-by-client",
                "X-Userinfo": forged_userinfo,
            }))[1])
            tid_spoof = header_value(h_spoof, CANON_ID)
            c.check("防伪：伪造 X-Tenant-Id 被剥离/改写为可信值（acme，非 spoofed/evil）",
                    tid_spoof == "acme", f"got {tid_spoof!r}")

            # ICD §8 头名一致性（本运行期脚本可验证的子集）：上游收到的 X-Tenant-* 头名只能是 canonical
            # 三者，不得出现大小写变体/别名/多余项。这验证「网关注入端头名」这一可见子集。
            # 注意：§8 还要求「gateway 默认头名 == starter-tenant TenantProperties 默认头名」这一**跨仓静态项**——
            # starter-tenant 在另一 Java 仓、本脚本看不到，**属本脚本范围外**，由主仓/contracts CI 守护。
            tenant_keys = {k for k in h_alice if k.lower().startswith("x-tenant-")}
            canon = {CANON_ID, CANON_ORG, CANON_SUBJECT}
            c.check("ICD §8 头名一致性（注入端子集）：上游 X-Tenant-* 头名 ⊆ canonical 且含 Id/Org",
                    tenant_keys <= canon and CANON_ID in tenant_keys and CANON_ORG in tenant_keys,
                    f"got {sorted(tenant_keys)}")

    # ── B 档 · ③ privacy 末端（pending #15）：privacy 经 gateway 可达 + X-Tenant-* 注入正确 ──
    # privacy 路由 /api/privacy/* 在集群 rewrite→/api/$1 命中真实编排层 /api/v1/psi/*，无通用 echo 端点；
    # 故需 PRIVACY_ECHO_PATH 指向「privacy 上游回显其收到请求头」的网关路径（形态待 #15 与 privacy 约定）。
    # 与 governance 的 B 档同构：未接则 gated（REQUIRE_FULL 下 FAIL 防静默变绿，否则 SKIP pending #15）。
    if not PRIVACY_ECHO:
        gated(c, "privacy 经 gateway 可达 + X-Tenant-Id=acme 注入（alice，经 /api/privacy/*）",
              "PRIVACY_ECHO_PATH 未设——pending privacy 回显端点(#15)；privacy 路由+upstream 已在主仓 chart configmap")
    else:
        code, body = http("GET", f"{GW}{PRIVACY_ECHO}", headers=auth(token_alice))
        if code != 200:
            gated(c, "privacy 经 gateway 可达 + X-Tenant-* 注入",
                  f"privacy 回显端点 {PRIVACY_ECHO} 返回 {code}（期望 200）——检查 PRIVACY_ECHO_PATH 与 privacy 回显实现")
        else:
            h_p = extract_headers(body)
            c.check("privacy：alice 经 /api/privacy/* 可达且上游收到 X-Tenant-Id=acme",
                    header_value(h_p, CANON_ID) == "acme", f"got {header_value(h_p, CANON_ID)!r}")
            c.check("privacy：上游收到 X-Tenant-Org=acme",
                    header_value(h_p, CANON_ORG) == "acme", f"got {header_value(h_p, CANON_ORG)!r}")
            # 防伪在 privacy 路径同样成立（复用 auth-tenant 链）：伪造 X-Tenant-*/X-Userinfo 应被剥离/覆盖。
            forged_p = base64.b64encode(
                json.dumps({"organization": "evil-tenant", "sub": "attacker"}).encode()).decode()
            h_ps = extract_headers(http("GET", f"{GW}{PRIVACY_ECHO}", headers=auth(token_alice, {
                CANON_ID: "spoofed-by-client",
                "X-Userinfo": forged_p,
            }))[1])
            tid_ps = header_value(h_ps, CANON_ID)
            c.check("privacy：伪造 X-Tenant-Id/X-Userinfo 被剥离/覆盖为可信值（acme）",
                    tid_ps == "acme", f"got {tid_ps!r}")

    c.finish("CLUSTER-E2E")


if __name__ == "__main__":
    main()
