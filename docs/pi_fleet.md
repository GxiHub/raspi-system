# Pi Fleet 總覽

**最後更新：2026-07-01**

## 機器清單

| 機器 | 型號 | 角色 | Tailscale | 區網 IP | 狀態 |
|------|------|------|-----------|---------|------|
| pi51 | Pi 3 | 升降滷台控制（舊） | 100.118.76.98 | 192.168.1.113 | ⚠️ SSH key 未設定，無法連線 |
| pi52 | Pi 5 | 出單機（煮麵+印表機） | 100.98.225.85 | — | ✅ 運行中 |
| pi53 | Pi 5 | 升降台控制 | 100.75.88.124 | — | ✅ 運行中 |
| pi54 | Pi 5 | 升降台控制 | 100.114.165.24 | 192.168.1.114 | ✅ 運行中 |

## 各機器服務

### pi52
- `noodle.service` — Flask web app（port 5000）
- `printer-proxy.service` — 印表機代理
- Desktop: LXDE / X11，kiosk 模式（Chromium）
- 出單機 UI（煮麵機操作介面）

### pi53
- `noodle.service` — Flask web app（port 5000）
- Desktop: LXDE / X11，kiosk 模式（Chromium）
- 升降台控制 UI（prototype 版本）
- RS485：步進電機（英鹏飞）

### pi54
- `noodle.service` — Flask web app（port 5000）
- Desktop: LXDE / X11，kiosk 模式（Chromium）
- 升降台控制 UI（prototype 版本）
- RS485：推桿通道 × 6、步進電機、溫控感測器

## 程式碼架構

```
raspi-system/
├── pi-prototype/           ← 共用基底（三台共用）
│   ├── app.py              ← 共用後端（pi52/pi53/pi54 全 symlink 指這裡）
│   ├── deploy.sh           ← 一鍵部署腳本
│   ├── noodle.service      ← User=__USER__ 佔位符
│   ├── 99-rs485.rules
│   ├── kiosk.sh
│   ├── settings.json
│   ├── requirements.txt
│   ├── autostart/kiosk.desktop
│   └── templates/          ← 升降台 UI（index + orders + temp_curve + water_timer + printer_changelog）
│
├── pi52/                   ← pi52 專屬（proxy.py、出單機 templates、test_render.py）
├── pi53/                   ← pi53 機器設定備份
├── pi54/                   ← pi54 機器設定備份
└── docs/                   ← 文件
```

## 部署方式

### 新機器從 prototype 開始
```bash
git clone https://github.com/GxiHub/raspi-system.git
cd raspi-system
bash pi-prototype/deploy.sh <username>   # 例：pi55
```

### 更新現有機器
```bash
cd ~/raspi-system
GIT_TERMINAL_PROMPT=0 git pull
bash pi-prototype/deploy.sh <username>
```

## RS485 udev 規則（pi53/pi54）

```
/etc/udev/rules.d/99-rs485.rules
```

USB 轉 RS485 適配器對應 `/dev/ttyRS485` symlink：
- VID `1a86` PID `55d3`（序號 5658002095）
- VID `0403` PID `6001`（序號 BG00OOM8）
- VID `1a86` PID `7523`（無序號，fallback）

## SSH 連線方式

```bash
ssh pi52       # Tailscale 100.98.225.85
ssh pi53       # alias: ssh pi — Tailscale 100.75.88.124
ssh pi54-ts    # Tailscale 100.114.165.24
ssh pi51-ts    # Tailscale 100.118.76.98（SSH key 未設定）
```

## Git 操作規範

**多台機器共用同一 repo，git 操作必須指定路徑，禁止 `git add -A`。**

```bash
# ✅ 正確：指定要提交的檔案
git add pi-prototype/app.py
git add pi52/proxy.py

# ❌ 禁止：會掃入其他機器的本地修改，造成跨機汙染
git add -A
git add .
```

**原因**：pi52/pi53/pi54 各自 clone 同一份 repo，若在 pi54 執行 `git add -A`，
會把 pi54 本地的修改（包括 pi53 相關路徑的變動）一起掃入，
導致其他機器 pull 後 app.py 被覆蓋成空檔（2026-06-30 曾發生）。

## 已知問題與待辦

詳見 GitHub Issues。

## 架構決策紀錄

### 2026-06-30：統一 prototype 架構
- 以 pi54 UI 為基底建立 `pi-prototype/`
- pi53 和 pi54 從同一 prototype 部署
- app.py 三台共用，不分機型

### 2026-06-30：app.py 空檔事件
- 起因：pi54 `git add -A` 意外掃入 pi53 的舊 app.py
- 影響：pi53 noodle.service 啟動後 62ms 退出，kiosk 無法啟動
- 修復：`git checkout HEAD -- pi52/noodle-app/app.py`
- 教訓：應指定路徑 `git add <file>`，不用 `git add -A`

