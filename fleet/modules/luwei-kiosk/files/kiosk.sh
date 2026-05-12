#!/bin/bash
# 等 Flask 服務就緒才開瀏覽器
until curl -sf http://localhost:5000 > /dev/null; do
    sleep 1
done

# 關螢幕保護 / 防止自動熄屏
xset s off
xset -dpms
xset s noblank

# Kiosk 全螢幕
exec chromium \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    http://localhost:5000
