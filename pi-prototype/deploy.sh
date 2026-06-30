#!/bin/bash
# 用法：bash pi-prototype/deploy.sh <username>
# 範例：bash pi-prototype/deploy.sh pi53
set -e

USER="${1:-$(whoami)}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROTO="$REPO/pi-prototype"

echo "=== 部署 prototype 到 $USER ==="

# 1. 更新 repo
cd "$REPO"
GIT_TERMINAL_PROMPT=0 git pull

# 2. 確認 /var/www/html 結構
sudo mkdir -p /var/www/html/templates

# 3. 建立 app.py symlink（如果不存在）
if [ ! -L /var/www/html/app.py ]; then
    sudo ln -sf "$PROTO/app.py" /var/www/html/app.py
    echo "✓ app.py symlink 建立"
else
    echo "- app.py symlink 已存在"
fi

# 4. 部署所有 templates
sudo cp "$PROTO"/templates/*.html /var/www/html/templates/
sudo chown -R "$USER":"$USER" /var/www/html/templates/
echo "✓ templates（index + orders + temp_curve + water_timer + printer_changelog）"

# 5. 部署 kiosk
cp "$PROTO/kiosk.sh" "$HOME/kiosk.sh"
chmod +x "$HOME/kiosk.sh"
mkdir -p "$HOME/.config/autostart"
sed "s/__USER__/$USER/g" "$PROTO/autostart/kiosk.desktop" > "$HOME/.config/autostart/kiosk.desktop"
echo "✓ kiosk.sh + kiosk.desktop"

# 6. 部署 udev rules
sudo cp "$PROTO/99-rs485.rules" /etc/udev/rules.d/99-rs485.rules
sudo udevadm control --reload-rules
echo "✓ 99-rs485.rules"

# 7. 部署 noodle.service
sed "s/__USER__/$USER/g" "$PROTO/noodle.service" > /tmp/noodle_deploy.service
sudo cp /tmp/noodle_deploy.service /etc/systemd/system/noodle.service
sudo systemctl daemon-reload
echo "✓ noodle.service"

# 8. 部署 settings.json（只在沒有的情況下）
if [ ! -f /var/www/html/settings.json ]; then
    cp "$PROTO/settings.json" /var/www/html/settings.json
    echo "✓ settings.json（新建）"
else
    echo "- settings.json 已存在，跳過"
fi

# 9. 安裝 Python 套件
pip3 install -q --break-system-packages -r "$PROTO/requirements.txt"
echo "✓ Python 套件"

# 10. 重啟服務
sudo systemctl restart noodle.service
sleep 2
if systemctl is-active --quiet noodle.service; then
    echo "✓ noodle.service 啟動成功"
else
    echo "✗ noodle.service 啟動失敗"
    journalctl -u noodle.service -n 10 --no-pager
fi

echo "=== 完成 ==="
