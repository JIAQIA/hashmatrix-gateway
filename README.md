# hashmatrix-gateway

> hashmatrix 数据中台子模块 · 所属：网关层（南北向）
>
> 主仓：[HashMatrixData/hashmatrix](https://github.com/HashMatrixData/hashmatrix)

## 角色与位置（一眼看懂）

- **所属**：南北向网关 · 位于**接入层与应用服务层之间**的统一入口。
- **一句话**：所有外部请求的总闸——路由 / 限流 / 鉴权 / 审计，并且是**租户上下文进入系统的第一关**。
- **调用流**：webui / 开放 API → **gateway(APISIX)** →（OIDC 校验 + 注入 `X-Tenant-*`）→ 各应用服务。

## 职责与边界

- **做**：路由转发、限流熔断、Keycloak OIDC 校验、审计日志、从 JWT 注入 `X-Tenant-*` 头、统一开放 API 出口。
- **不做（边界）**：不写业务逻辑；不做服务注册（服务发现走 **K8s Service/DNS**）；认证由 Keycloak 负责，网关只校验。

## 骨架技术选型（首选 · 平台级）

| 维度 | 选型 |
|--|--|
| 网关 | **APISIX**（首选，备 Spring Cloud Gateway） |
| 认证 | **Keycloak**（OIDC/OAuth2），网关侧校验、应用无感 |
| 形态 | 配置驱动 + 插件（路由 / 限流 / 审计 / 租户头注入），与发版解耦、热加载 |

> 服务发现走 K8s Service/DNS，不引服务注册中心（去 Nacos 注册，见架构 02）。

## 产品形态与多租户（北极星）

**双模交付**：公网 SaaS（我们运营 · 统一**我们品牌** · 租户=企业）／私有化部署（客户环境 · **客户品牌**部署级 · 租户=客户部门）。品牌**部署级**、不按租户运行期换肤。多租户走 **C 分层桥接**：控制平面共享 + 数据平面按租户隔离（Keycloak Organizations 单 realm · schema/db-per-tenant · namespace-per-tenant），由 `control-plane` 编排开通。

**本仓视角**：校验 Keycloak OIDC，从 JWT org 声明**注入 `X-Tenant-*` 头**——租户上下文入口。

> 详见主仓 `docs/00-主仓初始化-spec.md`、`docs/architecture/05-多租户与控制平面.md`。

## 说明

本仓库作为 `hashmatrix` 主仓的 git submodule，挂载于 `services/gateway`。架构背景见主仓 `docs/architecture/`。

## License

Apache-2.0
