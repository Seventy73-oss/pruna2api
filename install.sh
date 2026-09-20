#!/usr/bin/env bash
# pruna2api 一次性安装脚本 —— 在**目标机**上运行
#
# 用法：
#   PRUNA_USER=yourname bash install.sh                    # 装到 /opt/pruna2api
#   PRUNA_ROOT=/srv/pruna2api PRUNA_USER=you bash install.sh
#
# 装完后：编辑 /etc/systemd/system/pruna2api.service 把 User= 改成你的用户名
set -euo pipefail

ROOT="${PRUNA_ROOT:-/opt/pruna2api}"
USER_NAME="${PRUNA_USER:-${SUDO_USER:-$(id -un)}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> 安装到 $ROOT（运行用户：$USER_NAME）"

echo "== 1. 建目录 =="
sudo mkdir -p "$ROOT"/{data,media,log,mihomo/log,mihomo/providers}
sudo chown -R "$USER_NAME" "$ROOT"
echo "   $ROOT 就绪"

echo "== 2. 拷贝文件 =="
sudo cp -f "$HERE"/main.py "$HERE"/pruna_client.py "$HERE"/ui.html "$HERE"/check_ui.py "$ROOT"/
sudo cp -f "$HERE"/ui_e2e.html "$HERE"/test_firstlast.py "$ROOT"/ 2>/dev/null || true
sudo chown -R "$USER_NAME" "$ROOT"

echo "== 3. 建 venv 并装依赖 =="
if [ ! -x "$ROOT/venv/bin/python" ]; then
  sudo -u "$USER_NAME" python3 -m venv "$ROOT/venv"
fi
sudo -u "$USER_NAME" "$ROOT/venv/bin/pip" install -q --upgrade pip
sudo -u "$USER_NAME" "$ROOT/venv/bin/pip" install -q -r "$HERE/requirements.txt"
"$ROOT/venv/bin/python" -c "import fastapi,uvicorn,requests,pydantic;print('   依赖 OK, fastapi', fastapi.__version__)"

echo "== 4. 装 systemd 单元 =="
sudo cp -f "$HERE/pruna2api.service" /etc/systemd/system/
sed -i.bak "s|^User=.*|User=$USER_NAME|" /etc/systemd/system/pruna2api.service
sudo rm -f /etc/systemd/system/pruna2api.service.bak

if [ -f "$HERE/mihomo-pruna.service" ]; then
  sudo mkdir -p /etc/systemd/system/pruna2api.service.d
  sudo cp -f "$HERE/mihomo-pruna.service" /etc/systemd/system/
  sudo cp -f "$HERE/pruna2api-mihomo.conf" /etc/systemd/system/pruna2api.service.d/mihomo.conf
  sed -i.bak "s|^User=.*|User=$USER_NAME|" /etc/systemd/system/mihomo-pruna.service
  sudo rm -f /etc/systemd/system/mihomo-pruna.service.bak
  echo "   mihomo 单元已装（还需放入 mihomo 二进制，见 README）"
fi

sudo systemctl daemon-reload
sudo systemctl enable pruna2api

echo
echo "==> 完成。启动："
echo "    sudo systemctl start pruna2api"
echo "    控制台 http://<本机>:3020/"
