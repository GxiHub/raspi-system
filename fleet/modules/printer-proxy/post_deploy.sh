#!/bin/bash
set -e
CUR_USER=$(whoami)
CUR_HOME=$(eval echo ~$CUR_USER)

# 修正 service 檔 placeholder
sed -i "s|__NODE_HOME__|$CUR_HOME|g" "$CUR_HOME/printer-proxy.service"
sed -i "s|__NODE_USER__|$CUR_USER|g" "$CUR_HOME/printer-proxy.service"

# 修正 proxy.py 中寫死的 /var/www/html/orders.db → 本機 home
sed -i "s|/var/www/html/orders.db|$CUR_HOME/orders.db|g" "$CUR_HOME/proxy.py"

# 修正 MY_IP / MY_MAC（若有寫死則清空讓程式自動偵測）
# 取得本機 IP（wlan0 或 eth0）
LOCAL_IP=$(ip route get 1 2>/dev/null | awk "{print \$7}" | head -1)
LOCAL_MAC=$(cat /sys/class/net/wlan0/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null || echo "")
[ -n "$LOCAL_IP" ] && sed -i "s|^MY_IP\s*=.*|MY_IP = \"$LOCAL_IP\"|" "$CUR_HOME/proxy.py"
[ -n "$LOCAL_MAC" ] && sed -i "s|^MY_MAC\s*=.*|MY_MAC = \"$LOCAL_MAC\"|" "$CUR_HOME/proxy.py"

# 安裝並啟動 service
sudo cp "$CUR_HOME/printer-proxy.service" /etc/systemd/system/printer-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable printer-proxy
sudo systemctl restart printer-proxy
echo "[post_deploy] printer-proxy installed and started on $CUR_USER@$LOCAL_IP"
