#!/usr/bin/env bash
# 把本地代码推送到远端并重启服务 —— 在**开发机**上运行
#
# 前置：已配置 SSH 公钥登录（不要把密码写进脚本）
#   ssh-copy-id you@your-host
#
# 用法：
#   HOST=192.0.2.10 USER=you bash deploy.sh
#   HOST=nas.local USER=you REMOTE_ROOT=/srv/pruna2api bash deploy.sh
set -euo pipefail

HOST="${HOST:?请设置 HOST，例如 HOST=192.0.2.10 bash deploy.sh}"
RUSER="${USER:-$(id -un)}"
PORT="${PORT:-22}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/pruna2api}"
SERVICE="${SERVICE:-pruna2api}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 需要同步的文件（不含运行时数据与凭据）
FILES=(main.py pruna_client.py ui.html check_ui.py ui_e2e.html test_firstlast.py)

echo "==> 推送到 $RUSER@$HOST:$REMOTE_ROOT"

for f in "${FILES[@]}"; do
  [ -f "$HERE/$f" ] || { echo "   跳过（不存在）$f"; continue; }
  echo "   → $f"
  scp -P "$PORT" -q "$HERE/$f" "$RUSER@$HOST:$REMOTE_ROOT/$f"
done

echo "==> 清缓存并重启"
ssh -p "$PORT" "$RUSER@$HOST" "rm -rf $REMOTE_ROOT/__pycache__ && sudo systemctl restart $SERVICE && sleep 4 && systemctl is-active $SERVICE"

echo "==> 健康检查"
curl -s --max-time 8 "http://$HOST:3020/health" || echo "   （如为内网地址，请从内网访问）"
echo
