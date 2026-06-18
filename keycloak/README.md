# keycloak/ —— OIDC realm 导入样例（脱敏占位）

`realm-export.json` 在 Keycloak 启动时经 `--import-realm` 导入，提供本地可跑的 OIDC 身份源。

> 🔴 全部为虚构脱敏占位（`acme` / `tenant-demo` / `*.example.com` / 演示口令），严禁用于任何真实环境。

## realm 内容

| 对象 | 值 | 说明 |
|--|--|--|
| realm | `hashmatrix` | 单 realm；`organizationsEnabled: true`（Keycloak Organizations） |
| organizations | `acme`、`tenant-demo` | 体现「org = 租户」模型（公网=企业 / 私有化=部门） |
| client | `apisix`（公开客户端，开启 Direct Access Grants） | 网关侧 OIDC 客户端；公开客户端 + JWKS 本地验签，无需密钥 |
| users | `alice`（→acme）、`bob`（→tenant-demo），口令 `Passw0rd!` | 演示用户 |

## 租户声明（org → JWT → X-Tenant-*）

客户端 `apisix` 上配置了协议映射器 `tenant-org`（类型 *User Attribute*），把用户属性 `tenant` 写入 JWT 的 `organization` 声明：

```
alice (attribute tenant=acme)  →  access_token { "organization": "acme", ... }
```

网关的 `tenant-context` 插件读取该声明并注入 `X-Tenant-*`。

> **为什么用属性映射器而非 Organization 成员关系映射器**：realm 导入对「组织成员关系」的还原在不同 Keycloak 版本上保真度不稳，为让本地冒烟**确定性通过**，demo 采用 *User Attribute → claim* 这一可靠路径，同时仍在 realm 内启用 Organizations 并建好 `acme` / `tenant-demo` 以体现架构模型。
> **生产**：改由真正的 *Organization Membership* 映射器产出 `organization` 声明即可——网关插件与声明来源解耦，无需改动。

## issuer 一致性（本地常见坑）

APISIX 与冒烟容器都经容器网络内 `http://keycloak:8080` 访问 Keycloak，故 token 的 `iss` 与网关 discovery 解析出的 issuer 一致。若改用宿主机 `localhost:8081` 直接取 token 再打网关，会因 issuer 不一致导致验签失败——这是 Keycloak-in-Docker 的已知现象，请保持二者主机名一致。
