# CLAUDE.md — hashmatrix-gateway 协作与合规指引

本文件为 Claude Code 及所有协作者在本仓库工作的**强制约束**。违反「信息红线」的内容一律不得提交。

## 🔴 信息红线（强制 · 不可协商）

本仓库为**公开开源仓库**。所有内容（代码、注释、文档、配置样例、提交信息、Issue/PR、分支与标签名）必须满足：

1. **禁止出现任何甲方/客户可识别信息**，包括但不限于：真实单位名称/简称/品牌、人员姓名或账号、招标/合同/立项编号、内部项目代号、甲方专有业务术语、真实数据、具体部署地点、客户网络或系统拓扑。
2. **禁止透漏任何项目机密**：商务/合同条款、里程碑与报价、验收细节、甲方环境参数、真实业务数据样本。
3. **仅允许记录可面向大众公开的内容**：通用技术方案、代码实现、系统架构与产品决策、开源组件选型、通用工程最佳实践。
4. **示例/测试数据一律虚构脱敏**，使用通用占位（如 `example.com`、`acme`、`tenant-demo`），严禁使用任何真实甲方数据。
5. **敏感原始资料一律置于 `.gitignore`、不得入库**（仅本地留存）。

> 判定标准：把本仓任意文件公开到互联网，不会泄露任何客户身份或项目机密。不确定时一律按「不写入」处理。

## 提交前自检（每次 commit / PR 必过）

- [ ] 无甲方名称 / 编号 / 代号 / 人员 / 地点等可识别信息
- [ ] 无商务 / 合同 / 验收 / 报价等项目机密
- [ ] 示例数据均为虚构 / 脱敏
- [ ] 敏感原始资料未入库（已在 `.gitignore`）
- [ ] 提交信息与分支/标签名同样不含上述敏感信息

## 🧭 北极星：产品形态与多租户模式（开发者时刻谨记）

本平台**双模交付**，所有设计与代码都须按此模式思考：

| | 公网 SaaS | 私有化部署 |
|--|--|--|
| 运营 / 品牌 | 我们运营 · **我们公司统一品牌** | 客户环境 · **客户品牌（部署级）** |
| 租户 = | 企业客户 | 客户的部门 |

- **品牌是部署级**（部署期配置注入），**不按租户在运行期动态换肤**。
- **多租户隔离（C 分层桥接）**：控制平面共享 + 数据平面按租户隔离。身份 = Keycloak **Organizations 单 realm**（org=租户，JWT 带 tenant 声明）；数据 = **schema/db-per-tenant**；计算 = **namespace-per-tenant**；由 `control-plane` 编排开通。

**本仓视角（gateway）**：网关是**租户上下文进入系统的第一道关**——校验 Keycloak OIDC（单 realm + Organizations），从 JWT 的 org/tenant 声明**注入 `X-Tenant-*` 头**下发各服务；按部署 / 租户做路由与限流。

> 全局定义见主仓 `docs/00-主仓初始化-spec.md` 与 `docs/architecture/05-多租户与控制平面.md`。

## 🔗 契约（Contracts）—— 跨子系统集成

本项目经**契约**与其它子系统集成。契约的**单一事实源在主仓** `HashMatrixData/hashmatrix` 的 `contracts/`：
- 索引（机器可读）`contracts/registry.yaml` · 规范 `contracts/CONVENTIONS.md` · 设计 `docs/architecture/06-契约治理.md`
- 在线：https://github.com/HashMatrixData/hashmatrix/tree/main/contracts

**铁律**：先改契约、再改实现；加法兼容默认放行，破坏性走 MAJOR + 弃用期双跑 + 通知消费方；消费方一律 tolerant reader。

**本仓契约**：
- producer：**`icd/tenant-context-headers`**（本仓是**唯一产生方**）——网关边缘注入的租户上下文头线契约。
- consumer：暂无

### producer · `icd/tenant-context-headers`（租户上下文头 · 唯一事实源 = 主仓 ICD）

网关校验 Keycloak OIDC 后，从验签产物解析**单一活动租户**，向上游**注入**下列头（产生方实现见 `plugins/tenant-context.lua`）：

| 头 | 语义 | 来源（Keycloak claim） | 必需 | 消费方（库绑定） |
|--|--|--|--|--|
| `X-Tenant-Id` | 稳定租户标识——数据/计算隔离的路由键（schema/catalog/namespace） | `active_organization` → 单一 `organization` → `tenant`（选择规则见 ICD §3.4） | 是 | `starter-tenant` → `TenantContext.tenantId` |
| `X-Tenant-Org` | 活动 org 原始标识/别名（信息性，**不用于**隔离路由） | `organization`（活动 org 子项） | 否 | `starter-tenant` → `TenantContext.org`（可选） |
| `X-Tenant-Subject` | 终端用户主体 | `sub` | 否 | 预留（当前未消费，consumer 须 tolerant） |

- **消费方（服务，须遵循本契约）**：governance / security / tools-bi / privacy / data-foundation / platform-common / control-plane。
- **头名唯一事实源是 ICD §2**；上表为速查，头名须与 `plugins/tenant-context.lua` 默认值（`id_header`/`org_header`/`subject_header`）及 ICD §8 一致性校验保持一致——当前均为 `X-Tenant-Id` / `X-Tenant-Org` / `X-Tenant-Subject`。
- **信任根 = 网关**：进入即清洗客户端伪造的 `X-Tenant-*`，仅注入 `openid-connect` 验签后的可信值；缺身份/不可判定即 fail-closed（401/403）。信任与解析模型见 ICD §3–§5。

**如何查阅（随时拉最新，勿存本地副本）**：
- 在 superproject（`hashmatrix/services/gateway`）下：直接读 `../../contracts/icd/tenant-context-headers-icd.md`。
- 独立 clone：WebFetch `https://raw.githubusercontent.com/HashMatrixData/hashmatrix/main/contracts/icd/tenant-context-headers-icd.md`（公开仓免鉴权）；或 `gh api repos/HashMatrixData/hashmatrix/contents/contracts/<path> -H "Accept: application/vnd.github.raw"`。

## 仓库定位

南北向网关子模块：路由 / 限流 / 鉴权(OIDC) / 审计 的配置与插件。APISIX 或 Spring Cloud Gateway 待定。

技术栈与具体选型**待独立讨论后逐步丰富**，当前为初始脚手架。
