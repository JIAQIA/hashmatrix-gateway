# scripts/ —— 本地验证脚本

| 脚本 | 运行位置 | 作用 |
|--|--|--|
| `smoke_test.py` | compose 网络内（`run --rm smoke`） | 冒烟断言：无 token→401；合法 token→200 且上游可见 `X-Tenant-*`；伪造头被剥离；双租户隔离 |
| `hot-reload.sh` | 宿主机 | 热加载验证：翻转 `apisix.yaml` 中一个配置值，证明 APISIX **不重启**即生效 |

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

## 热加载机制说明（重要）

`apisix.yaml` 是 standalone 模式的事实源，APISIX 周期性检测其 **mtime** 并自动 reload（**不重启、不接 etcd**）。`hot-reload.sh` 据此验证：

- **Linux / CI / 生产（ConfigMap）**：mtime 变更正常传播 → 走「自动热加载」分支，全程不重启。CI 已包含该步骤以真实验证。
- **macOS Docker Desktop**：对「单文件 bind mount」会**缓存 mtime**（内容可见但 mtime 冻结），自动 reload 不触发——这是该环境的**已知限制，非网关缺陷**。脚本会自动**回退优雅重启**（`compose restart`，仍不接 etcd、不重建镜像）并明确提示。

> 即：网关的热加载能力本身是成立的；本地 macOS 看到的「回退重启」仅是 Docker Desktop 文件共享层的元数据缓存所致。
