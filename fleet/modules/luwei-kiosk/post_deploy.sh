#!/bin/bash
# post_deploy for luwei-kiosk on pi51
set -e
NODE_USER=${NODE_USER:-pi51}

# udev RS485 rule
sudo tee /etc/udev/rules.d/99-rs485.rules > /dev/null << 'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="ttyRS485", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger

# systemd service for Flask app
sudo tee /etc/systemd/system/luwei-kiosk.service > /dev/null << 'EOF'
[Unit]
Description=升降滷台控制系統
After=network.target

[Service]
ExecStart=/usr/bin/python3 -u /var/www/html/app.py
WorkingDirectory=/var/www/html
Restart=always
RestartSec=5
User=${NODE_USER}
StandardOutput=append:/var/log/luwei-kiosk.log
StandardError=append:/var/log/luwei-kiosk.log

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable luwei-kiosk.service

# lightdm autologin
sudo groupadd -f autologin
sudo usermod -aG autologin ${NODE_USER}
sudo sed -i "s/autologin-user=.*/autologin-user=${NODE_USER}/" /etc/lightdm/lightdm.conf
sudo sed -i "s/autologin-session=.*/autologin-session=LXDE/" /etc/lightdm/lightdm.conf
sudo sed -i "s/user-session=.*/user-session=LXDE/" /etc/lightdm/lightdm.conf
sudo sed -i "s/greeter-session=.*/greeter-session=lightdm-gtk-greeter/" /etc/lightdm/lightdm.conf
sudo sed -i "s/#autologin-user-timeout=0/autologin-user-timeout=0/" /etc/lightdm/lightdm.conf

chmod +x ~/kiosk.sh
echo '[luwei-kiosk] post_deploy done'
