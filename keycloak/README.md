# keycloak/ —— OIDC realm 导入样例（脱敏占位）

`realm-export.json` 在 Keycloak 启动时经 `--import-realm` 导入，提供本地可跑的 OIDC 身份源。

> 🔴 全部为虚构脱敏占位（`acme` / `tenant-demo` / `*.example.com` / 演示口令），严禁用于任何真实环境。

## realm 内容

| 对象 | 值 | 说明 |
|--|--|--|
| realm | `hashmatrix` | 单 realm；`organizationsEnabled: true`（Keycloak Organizations） |
| organizations | `acme`、`tenant-demo` | 体现「org = 租户」模型（公网=企业 / 私有化=部门） |
| client | `apisix`（公开客户端，开启 Direct Access Grants） | 网关侧 OIDC 客户端；公开客户端 + JWKS 本地验签，无需密钥 |
| users | 见下表，口令均 `Passw0rd!` | 演示 / 契约用户 |

### 演示用户与 org membership

| user | `tenant` 属性（→`organization`） | `active_tenant`（→`active_organization`） | 期望解析 | 覆盖 ICD §3.4 分支 |
|--|--|--|--|--|
| `alice` | `[acme]` | — | `acme`、200 | 单一 membership（b） |
| `bob` | `[tenant-demo]` | — | `tenant-demo`、200 | 单一 membership（b） |
| `carol` | `[acme, tenant-demo]` | `acme` | `acme`、200 | 活动 org 优先（a） |
| `dave` | `[acme, tenant-demo]` | — | 边缘 `403` | 多 membership 无活动声明（c） |

`carol` / `dave` 为**契约用户**：覆盖修订后 ICD §3.4「单活动租户 + 结构预留多 membership」的三分支（见 `scripts/smoke_test.py`）。

## 租户声明（org → JWT → X-Tenant-*）

客户端 `apisix` 上配置两个 *User Attribute* 协议映射器，把用户属性写入 JWT：

| 映射器 | 用户属性 | claim | 形态 | 语义 |
|--|--|--|--|--|
| `tenant-org` | `tenant` | `organization` | **多值数组**（`multivalued`） | 用户全部 org membership |
| `active-org` | `active_tenant` | `active_organization` | 字符串 | org-scoped token 选定的活动 org |

```
alice (tenant=[acme])                       →  access_token { "organization": ["acme"], ... }
carol (tenant=[acme,tenant-demo], active=acme) → access_token { "organization": ["acme","tenant-demo"], "active_organization": "acme", ... }
```

网关的 `tenant-context` 插件按 §3.4 优先级（`active_organization` → 单一 `organization` → 回退 `tenant`）解析**单一活动租户**并注入 `X-Tenant-*`。

> **为什么用属性映射器而非 Organization 成员关系映射器**：realm 导入对「组织成员关系」的还原在不同 Keycloak 版本上保真度不稳，为让本地冒烟**确定性通过**，demo 采用 *User Attribute → claim* 这一可靠路径（也使「多 membership / 活动 org」可被合成 token **确定性覆盖**），同时仍在 realm 内启用 Organizations 并建好 `acme` / `tenant-demo` 以体现架构模型。
> **生产**：改由真正的 *Organization Membership* / 活动 org 映射器产出 `organization` / `active_organization` 声明即可——网关插件与声明来源解耦（claim 名经插件配置项可调），无需改动。

> ⚠️ **改动 realm 后需重新导入**：`start-dev --import-realm` 仅在 realm 不存在时导入；已存在则跳过。本地改完用 `docker compose -f docker-compose.local.yml down`（容器 H2 为临时态，随容器销毁）再 `up`，新用户/映射器方可生效。CI 每次 `down -v` 起新栈，自动重导。

## issuer 一致性（本地常见坑）

APISIX 与冒烟容器都经容器网络内 `http://keycloak:8080` 访问 Keycloak，故 token 的 `iss` 与网关 discovery 解析出的 issuer 一致。若改用宿主机 `localhost:8081` 直接取 token 再打网关，会因 issuer 不一致导致验签失败——这是 Keycloak-in-Docker 的已知现象，请保持二者主机名一致。
