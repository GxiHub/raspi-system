#!/bin/bash
until curl -sf http://localhost:5000 > /dev/null; do
    sleep 1
done

gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true

xset s off 2>/dev/null || true
xset -dpms 2>/dev/null || true
xset s noblank 2>/dev/null || true

exec chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --password-store=basic \
    --disable-translate \
    --lang=zh-TW \
    --remote-debugging-port=9222 \
    --remote-allow-origins=* \
    http://localhost:5000
