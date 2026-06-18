# plugins/ —— 自定义 APISIX 插件（Lua）

采用 APISIX 原生 **Lua 插件**（进程内执行），无需 plugin-runner 边车，保持本地栈最小化。

| 插件 | 阶段 / 优先级 | 作用 |
|--|--|--|
| `tenant-context.lua` | `rewrite` / 2598 | **核心**：从 openid-connect 验签后注入的 `X-Userinfo` 提取 org/tenant → 注入 `X-Tenant-Id` / `X-Tenant-Org` / `X-Tenant-Subject`，并暴露 `$tenant_id` 变量；进入即剥离客户端伪造的同名头 |
| `audit-log.lua` | `log` / 397 | 结构化审计行（方法/路径/状态/租户/主体/耗时/IP）输出到 APISIX 日志 |

## tenant-context 设计要点

- **信任根来自 openid-connect 的验签产物**：插件**不自行验签**，而是读取 openid-connect 在校验通过后注入的 `X-Userinfo`（base64(JSON)）。该头由 openid-connect **覆盖**客户端伪造的同名值（已实测验证），故内容可信。
- 🔒 **安全约束（违反即可能跨租户越权）**：本插件**必须**与 openid-connect 配置在**同一路由、且在其后**。
  - 任何**未配 openid-connect** 的路由上 `X-Userinfo` 不存在 → 插件 **fail-closed**：不注入任何租户头并返回 401（`require_tenant=true`）。
  - 故「漏配 openid-connect」是显式失败而非静默放行——把隐式顺序假设变成了强约束。本仓 `apisix.yaml` 通过共享 `plugin_config: auth-tenant` 把二者绑定在一起。
- **防越权**：进入即清除客户端可能携带的 `X-Tenant-*`，再写入网关可信值。
- **声明解析确定性**：`tenant_claim`（默认 `organization`）兼容 字符串 / **单元素**数组 / **单键**对象；**多 org 成员视为歧义并拒绝（403）**，避免 `pairs` 顺序不确定导致不可预测注入；对象形态优先取 `alias`/`name` 子字段（而非 org id/UUID 键）。缺失时回退 `fallback_claim`（默认 `tenant`）。
- **健壮性**：`X-Userinfo` 超过 `max_userinfo_len`（默认 16KB）直接拒绝，避免超长头开销。
- **限流联动**：注入的租户经 `core.ctx.register_var` 暴露为 `$tenant_id`，供 `limit-count` 以 `key_type: var` / `key: tenant_id` **按租户**限流。不要用 `$http_x_tenant_id`——那读的是客户端原始头（会被缓存且可伪造）。

### 配置项

| 字段 | 默认 | 说明 |
|--|--|--|
| `userinfo_header` | `X-Userinfo` | 须与 openid-connect 的 `set_userinfo_header` 对齐 |
| `tenant_claim` | `organization` | 承载租户的声明名 |
| `fallback_claim` | `tenant` | 回退声明名 |
| `id_header` / `org_header` / `subject_header` | `X-Tenant-Id` / `X-Tenant-Org` / `X-Tenant-Subject` | 注入的头名 |
| `require_tenant` | `true` | 无 userinfo / 无租户声明时是否拒绝 |
| `max_userinfo_len` | `16384` | `X-Userinfo` 长度上限 |

## audit-log 说明

审计默认 `log_level: info`（正常请求是 info 语义，避免污染 warn/error 告警信号）。审计行形如 `[audit] {...}`，**只记 `X-Tenant-*` 与状态/耗时，不记 token/Authorization**。本地若要查看，可临时把 `apisix/config.yaml` 的 `error_log_level` 调为 `info` 后 `grep '[audit]'`。

## 启用方式

1. 在 `apisix/config.yaml` 的 `plugins:` 列表登记插件名。
2. 将 `*.lua` 挂载到容器 `/usr/local/apisix/apisix/plugins/`（见 `docker-compose.local.yml`）。
3. 在 `apisix/apisix.yaml` 的路由 / `plugin_config` 中按需配置。

> 本地用 `luacheck plugins` 做静态检查（CI 已集成）；`.luacheckrc` 已声明 `ngx` 等 OpenResty 全局。

## 已知简化与后续（落地前需处理）

- **声明形态测试缺口**：demo 用 *User Attribute* 映射器产出**字符串** `organization`，冒烟只覆盖 `resolve_tenant` 的字符串分支。**数组 / 单键对象 / 多 org 歧义拒绝** 分支尚无测试。**生产切换到 Keycloak Organization Membership 映射器**（claim 变为数组/对象形态，见 [`../keycloak/README.md`](../keycloak/README.md)）**之前**，应补这些分支的用例。
- **`X-Tenant-Id` = org alias（demo 简化）**：当前 `X-Tenant-Id` 与 `X-Tenant-Org` 同值（alias）。生产若以 `X-Tenant-Id` 作 schema/db/namespace 隔离键并改用 org UUID，需在插件内做 alias→UUID 映射并同步下游约定。
