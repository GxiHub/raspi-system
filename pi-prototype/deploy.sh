#!/bin/bash
# 用法：./deploy_prototype.sh <username>
# 範例：./deploy_prototype.sh pi53
set -e

USER="${1:-$(whoami)}"
REPO="$HOME/raspi-system"
PROTO="$REPO/pi-prototype"

echo "=== 部署 prototype 到 $USER ==="

# 1. 更新 repo
cd "$REPO"
GIT_TERMINAL_PROMPT=0 git pull

# 2. 部署前端
cp "$PROTO/templates/index.html" /var/www/html/templates/index.html
echo "✓ index.html"

# 3. 部署 kiosk
cp "$PROTO/kiosk.sh" "$HOME/kiosk.sh"
chmod +x "$HOME/kiosk.sh"
mkdir -p "$HOME/.config/autostart"
sed "s/__USER__/$USER/g" "$PROTO/autostart/kiosk.desktop" > "$HOME/.config/autostart/kiosk.desktop"
echo "✓ kiosk.sh + kiosk.desktop"

# 4. 部署 udev rules
sudo cp "$PROTO/99-rs485.rules" /etc/udev/rules.d/99-rs485.rules
sudo udevadm control --reload-rules
echo "✓ 99-rs485.rules"

# 5. 部署 noodle.service
sed "s/__USER__/$USER/g" "$PROTO/noodle.service" > /tmp/noodle.service
sudo cp /tmp/noodle.service /etc/systemd/system/noodle.service
sudo systemctl daemon-reload
echo "✓ noodle.service"

# 6. 部署 settings.json（只在沒有的情況下）
if [ ! -f /var/www/html/settings.json ]; then
    cp "$PROTO/settings.json" /var/www/html/settings.json
    echo "✓ settings.json（新建）"
else
    echo "- settings.json 已存在，跳過"
fi

# 7. 重啟服務
sudo systemctl restart noodle.service
sleep 2
if systemctl is-active --quiet noodle.service; then
    echo "✓ noodle.service 啟動成功"
else
    echo "✗ noodle.service 啟動失敗"
    journalctl -u noodle.service -n 10 --no-pager
fi

echo "=== 完成 ==="
