# scripts/ —— 本地验证脚本

| 脚本 | 运行位置 | 作用 |
|--|--|--|
| `smoke_test.py` | compose 网络内（`run --rm smoke`） | 冒烟断言：无 token→401；合法 token→200 且上游可见 `X-Tenant-*`；伪造头被剥离；双租户隔离 |
| `cluster_e2e.py` | 集群（集群内 Job / port-forward） | 末端 e2e（#9）：经网关 OIDC → **真实 governance**，回归 fail-closed / per-tenant 隔离 / 防伪 / ICD §8 头名一致性；集群未就绪即优雅 **SKIP** |
| `hot-reload.sh` | 宿主机 | 热加载验证：翻转 `apisix.yaml` 中一个配置值，证明 APISIX **不重启**即生效 |
| `_gwlib.py` | （被 import，不单独运行） | `smoke_test.py` 与 `cluster_e2e.py` 共享原语（HTTP / 取 token / 回显头解析 / 断言累计），单一事实源、避免漂移 |

## 用法

```bash
# 1) 起栈
docker compose -f docker-compose.local.yml up -d apisix keycloak mock-upstream

# 2) 冒烟（纯 Python 标准库，无需本机装 curl/jq）
docker compose -f docker-compose.local.yml run --rm smoke

# 3) 热加载验证（宿主机需有 curl）
./scripts/hot-reload.sh

# 4) 拆栈
docker compose -f docker-compose.local.yml down -v
```

`smoke_test.py` 内置依赖就绪轮询（最长约 4 分钟），首次起栈 Keycloak 导入 realm 需要一点时间，属正常现象。

## 集群末端 e2e（`cluster_e2e.py` · #9）

`smoke_test.py` 跑在 compose + `mock-upstream`；`cluster_e2e.py` 是**末端汇聚点**——经网关 OIDC 打到 **kind 单 ns 内的真实 governance**，在集群路径回归同一安全不变量族。它依赖主仓 **hashmatrix#17**（chart 装进 ns · 已 CLOSED）与 **hashmatrix#15**（governance 经网关可达 · 进行中）；**集群不可达时整体 SKIP（exit 0）**，故可先入库、先接 CI 触发位（`.github/workflows/cluster-e2e.yml`，手动 `workflow_dispatch`、非阻塞），待 #15 落地即真跑。

**两档断言**（与 compose 冒烟同一不变量族，跑在集群路径）：

| 档 | 覆盖 | 是否需上游回显 |
|--|--|--|
| **A · 边缘可观测** | fail-closed（无/坏 token→401、dave 多 membership 无 active→403、superadmin 打租户路由→403）；per-tenant 限流隔离 | 否——状态码在网关边缘返回 |
| **B · 上游回显依赖** | `X-Tenant-*` 注入正确性（alice→acme / bob→tenant-demo / carol(active)→acme）；防伪（伪造 `X-Tenant-*` / `X-Userinfo` 被剥离/覆盖）；ICD §8 头名一致性 | 是——需 governance **回显其收到的请求头** |

> ⚠️ **集群 chart 仅落 `/api`（100/60）+ `/public`**：未部署低阈值限流样例路由（`/ratelimit` 2/60）时，A 档的「per-tenant 隔离」自动 **SKIP**（该不变量由 compose smoke 守护）；脚本以 `RATELIMIT_PATH` 返回 404 自动判定。

**环境变量**（脱敏 demo 默认；空值回退默认）：

| 变量 | 默认 | 说明 |
|--|--|--|
| `GATEWAY_URL` | `http://platform-gateway:9080` | 集群网关 Service；本地 port-forward 改 `http://127.0.0.1:9080` |
| `KEYCLOAK_URL` | `http://keycloak:8080` | in-cluster Keycloak；**token 的 `iss` 须与网关 discovery 一致**（见 [`../keycloak/README.md`](../keycloak/README.md) issuer 一致性） |
| `REALM` / `CLIENT_ID` | `hashmatrix` / `apisix` | OIDC realm / 客户端 |
| `PROTECTED_PATH` | `/api/get` | 受保护路由；fail-closed 在边缘返回，不依赖上游实现该路径 |
| `RATELIMIT_PATH` / `RATELIMIT_MAX` | `/ratelimit/get` / `2` | 限流样例路由与阈值；路由未部署（404）即 SKIP 隔离档 |
| `UPSTREAM_ECHO_PATH` | 空 | 网关上「回显请求头」的受保护路径。**空=跳过 B 档**（标注 pending #15）；接 governance 回显端点后填入 |

**先行干跑（compose，无需集群）**——`mock-upstream` 的 `/headers` 即回显端点，可在 #15 前验证脚本本身。

> ⚠️ **issuer 一致性前提**：从宿主 `127.0.0.1:8080` 取的 token，其 `iss` 须与网关 discovery 解析出的 issuer 一致，否则验签失败、B 档全 401（见 [`../keycloak/README.md`](../keycloak/README.md)「issuer 一致性」）。这正是 `smoke_test.py` 放进 compose 网络内跑的原因。**最稳妥**是把 cluster_e2e 也放进 compose 网络（与 smoke 同 issuer 主机名）：

```bash
docker compose -f docker-compose.local.yml up -d apisix keycloak mock-upstream
# 在 compose 网络内运行（issuer 主机名与网关一致，免 issuer 不一致坑）：
docker compose -f docker-compose.local.yml run --rm \
  -e UPSTREAM_ECHO_PATH=/api/headers -e GATEWAY_URL=http://apisix:9080 \
  -e KEYCLOAK_URL=http://keycloak:8080 smoke python cluster_e2e.py
```

> 🔌 **待 #15 与 governance 约定的接线点**：`UPSTREAM_ECHO_PATH` 指向的 governance 端点须**回显其收到的请求头**（形态宜与 go-httpbin 一致：`{"headers": {...}}`）。这是把「网关注入 → 上游收到」闭环断言所需的唯一测试 affordance；端点形态确定后更新本变量即可，脚本断言无需改。

## 热加载机制说明（重要）

`apisix.yaml` 是 standalone 模式的事实源，APISIX 周期性检测其 **mtime** 并自动 reload（**不重启、不接 etcd**）。`hot-reload.sh` 据此验证：

- **Linux / CI / 生产（ConfigMap）**：mtime 变更正常传播 → 走「自动热加载」分支，全程不重启。CI 已包含该步骤以真实验证。
- **macOS Docker Desktop**：对「单文件 bind mount」会**缓存 mtime**（内容可见但 mtime 冻结），自动 reload 不触发——这是该环境的**已知限制，非网关缺陷**。脚本会自动**回退优雅重启**（`compose restart`，仍不接 etcd、不重建镜像）并明确提示。

> 即：网关的热加载能力本身是成立的；本地 macOS 看到的「回退重启」仅是 Docker Desktop 文件共享层的元数据缓存所致。
