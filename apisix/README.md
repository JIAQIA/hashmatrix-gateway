# apisix/ —— 网关声明式配置（config-as-code）

本目录是 APISIX 的全部配置，采用 **standalone / 声明式 YAML 模式**：配置事实源(SoT)就在 Git 里，**无 etcd**。

| 文件 | 作用 | 是否热加载 |
|--|--|--|
| `config.yaml` | 启动期静态配置：监听端口、`deployment.role=data_plane`、`config_provider=yaml`、启用的插件集 | 否（改后需重启容器） |
| `apisix.yaml` | 运行期声明式资源：routes / upstreams / 插件配置 | **是**（保存即热加载，约 1s） |

## 为什么是 standalone（而非 etcd 传统模式）

- **配置即代码**：`apisix.yaml` 本身就是唯一事实源，不存在「Git 文件 ↔ etcd」两份配置漂移的问题。
- **去状态耦合**：不把业务路由/插件配置塞进 etcd，少运维一套有状态组件，契合 K8s/GitOps（ConfigMap + Reloader / ArgoCD）。
- **热加载**：APISIX 周期性检测 `apisix.yaml` 的 mtime 并自动 reload，无需重启、无需 Admin API。

> ⚠️ 本地 macOS Docker Desktop 对「单文件 bind mount」会缓存 mtime，导致自动 reload 在本机不触发（内容可见但 mtime 冻结）——属环境限制。`scripts/hot-reload.sh` 会自动回退优雅重启；Linux/CI/生产(ConfigMap) 上走真正的自动热加载分支。详见 [`../scripts/README.md`](../scripts/README.md)。

> 生产部署：本目录的 `apisix.yaml` / `config.yaml` 经主仓 `deploy/charts/gateway` 渲染为 ConfigMap 注入 APISIX（见该子 chart）。

## 关键约定

- `apisix.yaml` **必须以 `#END` 结尾**（standalone 模式的结束标记）。
- 启用自定义插件：在 `config.yaml` 的 `plugins:` 列表登记插件名（如 `tenant-context`），并把 `plugins/*.lua` 挂载到容器的 `apisix/plugins/` 路径（见 `docker-compose.local.yml`）。`plugins:` 显式声明会**覆盖**默认插件集，故需列全用到的插件。

## 路由与插件链

受保护路由共享 `plugin_config: auth-tenant`（`openid-connect` + `tenant-context` + `audit-log`，DRY），再各自叠加 `proxy-rewrite` 与 `limit-count`：

```
请求 → proxy-rewrite → openid-connect(验签, 无/坏 token→401, 注入 X-Userinfo)
     → tenant-context(读 X-Userinfo → 注入 X-Tenant-* + 暴露 $tenant_id; 无 X-Userinfo→fail-closed)
     → limit-count(key=$tenant_id, 按租户配额) → audit-log → 上游
```

| 路由 | 鉴权 | 说明 |
|--|--|--|
| `/api/*` | ✅ auth-tenant | 受保护 API，按租户 100/min |
| `/ratelimit/*` | ✅ auth-tenant | 限流样例，按租户 2/60s（便于冒烟验证每租户独立配额） |
| `/public/*` | ❌ | 公共开放端点；含 `response-rewrite`，用于热加载演示 |

> 🔒 `openid-connect` 与 `tenant-context` 通过共享 `plugin_config` 绑定为一对——漏配前者时后者 fail-closed，杜绝静默注入伪造租户。详见 [`../plugins/README.md`](../plugins/README.md)。
