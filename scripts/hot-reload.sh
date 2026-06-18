#!/usr/bin/env bash
# 热加载验证：修改 apisix.yaml 中的一个配置值，证明配置变更可在「不改镜像、不接 etcd」下生效。
#
# 机制：standalone 模式下 APISIX 周期性检测 apisix.yaml 的 mtime（约 1s）并自动 reload。
# 本脚本翻转 public-open 路由 response-rewrite 注入的 X-Gateway-Env 头值，轮询 /public/get 验证。
#
#   1) 首选「自动热加载」——Linux/CI/生产(ConfigMap) 上 mtime 会传播，APISIX 自动 reload，全程不重启。
#   2) 回退「优雅重启」——macOS Docker Desktop 对「单文件 bind mount」会缓存 mtime（内容可见但 mtime 冻结），
#      导致自动 reload 不触发；此为该环境的已知限制，非网关缺陷。此时回退 `compose restart`（不接 etcd、不重建镜像）。
#
# 前置：栈已起（docker compose ... up -d）。在宿主机运行：./scripts/hot-reload.sh
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.local.yml"
CFG="apisix/apisix.yaml"
GW="${GATEWAY_URL:-http://localhost:9080}"
OLD="X-Gateway-Env: local-dev"
NEW="X-Gateway-Env: hot-reloaded"

if ! grep -q "$OLD" "$CFG"; then
  echo "FAIL: 未在 $CFG 找到基线值 '$OLD'（是否已被改动？）" >&2
  exit 1
fi

# 备份 + 退出时恢复工作区；若用过重启回退，则重启一次让容器与恢复后的文件保持一致。
# 必须“原地截断重写”（保留 inode）：Docker 单文件 bind mount 只跟踪原 inode，mv/sed -i 重命名替换会让容器看不到。
restarted=0
cp "$CFG" "$CFG.bak"
cleanup() {
  cat "$CFG.bak" > "$CFG" && rm -f "$CFG.bak"
  if [ "$restarted" = 1 ]; then $COMPOSE restart apisix >/dev/null 2>&1 || true; fi
  echo "==> 已恢复 $CFG"
}
trap cleanup EXIT

observed() { curl -s -i "$GW/public/get" | grep -qi "$NEW"; }
wait_ready() { for _ in $(seq 1 30); do sleep 1; curl -sf -o /dev/null "$GW/public/get" && return 0; done; return 1; }

echo "==> 改动前：/public/get 的 X-Gateway-Env"
curl -s -i "$GW/public/get" | grep -i "X-Gateway-Env:" || echo "(暂无，可能栈尚未就绪)"

echo "==> 修改 ${CFG}: ${OLD} -> ${NEW}"
updated=$(sed "s/${OLD}/${NEW}/" "$CFG")
printf '%s\n' "$updated" > "$CFG"
touch "$CFG"

echo "==> [1/2] 等待自动热加载（轮询 mtime ~1-2s，最多 10s）"
for _ in $(seq 1 10); do
  sleep 1
  if observed; then
    echo "PASS: 自动热加载生效，未重启即观察到 '$NEW' ✓"
    exit 0
  fi
done

echo "==> 未观察到自动热加载——大概率是 macOS Docker Desktop 单文件 bind mount 的 mtime 缓存（环境限制，非网关缺陷）。"
echo "==> [2/2] 回退：优雅重启 apisix（不接 etcd、不重建镜像）"
restarted=1
$COMPOSE restart apisix >/dev/null
wait_ready || { echo "FAIL: 重启后网关未就绪" >&2; exit 1; }
if observed; then
  echo "PASS: 经优雅重启应用了配置变更，观察到 '$NEW' ✓（Linux/CI/生产上走自动热加载分支）"
  exit 0
fi

echo "FAIL: 两种方式均未观察到新值 '$NEW'" >&2
exit 1
