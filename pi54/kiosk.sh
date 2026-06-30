#!/bin/bash
until curl -sf http://localhost:5000 > /dev/null; do
    sleep 1
done

gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true

exec chromium     --kiosk --noerrdialogs --disable-infobars --no-first-run     --disable-session-crashed-bubble --disable-restore-session-state     --enable-features=UseOzonePlatform --ozone-platform=wayland     --password-store=basic --use-mock-keychain --touch-events=enabled     --disable-translate --lang=zh-TW     --remote-debugging-port=9222     --remote-allow-origins=*     http://localhost:5000
