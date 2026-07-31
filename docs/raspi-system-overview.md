# raspi-system 完整架構說明

**最後更新：2026-07-01**

這份文件說明 `raspi-system` repo 裡每個資料夾、每個檔案、每個設定在做什麼。

---

## 一、機器總覽

| 機器 | 角色 | Tailscale IP | 狀態 |
|------|------|--------------|------|
| pi51 | 升降滷台控制（舊機，已退役） | 100.118.76.98 | ⚠️ SSH key 未設定 |
| pi52 | 出單機（煮麵 + 印表機） | 100.98.225.85 | ✅ 運行中 |
| pi53 | 升降台控制 | 100.75.88.124 | ✅ 運行中 |
| pi54 | 升降台控制 | 100.114.165.24 | ✅ 運行中 |

SSH 連線：
```bash
ssh pi52       # Tailscale
ssh pi         # pi53 alias
ssh pi54-ts    # pi54 Tailscale
```

---

## 二、repo 目錄結構總覽

```
raspi-system/
├── pi-prototype/     ← 三台共用的程式碼基底
├── pi52/             ← pi52 專屬設定（出單機）
├── pi53/             ← pi53 機器設定備份
├── pi54/             ← pi54 機器設定備份
├── docs/             ← 文件
├── fleet/            ← 舊架構節點設定（參考用）
├── scripts/          ← 維運腳本
├── audit.sh          ← Pi 機隊健康檢查腳本
├── ubereats_auto.py  ← UberEats 自動整合（已停用）
├── docker-compose.yml
├── .gitignore
└── zip_watcher/      ← 舊功能（已停用）
```

---

## 三、pi-prototype/（核心：三台共用）

所有三台機器（pi52/pi53/pi54）的共用基底。新機器從這裡一鍵部署。

### `app.py`（59KB）
Flask 後端主程式。pi52/pi53/pi54 都透過 symlink 指向這一份：
```
/var/www/html/app.py → /home/<user>/raspi-system/pi-prototype/app.py
```

**API 路由一覽：**

| 路由 | 功能 |
|------|------|
| `GET /` | 升降台控制主頁 |
| `GET /orders` | 訂單列表頁 |
| `GET /temp_curve` | 溫度曲線頁 |
| `GET /water_timer` | 計時器頁 |
| `GET /api/health` | 健康檢查 |
| `GET/POST /api/settings` | 讀寫 settings.json（升降行程設定） |
| `GET /api/status` | 系統狀態（RS485、PT100 溫度、uptime） |
| `POST /api/up` | 升降台上升 |
| `POST /api/down_and_off` | 升降台下降並關電 |
| `POST /api/induction` | 電磁爐控制 |
| `POST /api/fan` | 風扇控制 |
| `POST /api/auto_temp` | 自動溫控開關 |
| `GET /api/temp_log` | 溫度歷史紀錄 |
| `POST /api/temp_log/clear` | 清除溫度紀錄 |
| `GET/POST /api/versions` | 查看 app.py 版本快照 |
| `POST /api/versions/save` | 儲存當前 app.py 快照 |
| `POST /api/versions/restore` | 還原指定快照 |
| `POST /api/session/start` | 開始一次烹飪 session |
| `POST /api/session/stop` | 結束 session |
| `GET /api/session/current` | 當前 session 狀態 |
| `GET /api/alerts` | 警報列表 |
| `GET /api/sessions` | 歷史 sessions |
| `GET /api/sessions/<sid>` | 單一 session 詳情 |
| `GET /api/pi_info` | Pi 硬體資訊 |
| `GET /api/orders` | 訂單列表（JSON） |
| `GET /api/orders/<id>` | 單筆訂單 |
| `POST /api/motor` | 步進電機控制 |
| `POST /api/orders/<id>/reprint` | 重新列印收據 |
| `GET /api/orders/<id>/meta` | 訂單 metadata |
| `POST /api/print_receipt` | 出單（PIL 渲染 → ESC/POS 送印） |
| `GET /epson_eposdevice/...` | Epson ePOS 模擬端點（讓收銀平板找到印表機） |

**出單格式（`/api/print_receipt`）：**
PIL 用 NotoSansCJK-Bold 字型，在 576px（80mm 熱感紙）上繪製點陣圖，再轉 ESC/POS 送印。
字型尺寸：96pt（標題）/ 48pt（副標）/ 76pt（號碼牌）/ 62pt（口味）/ 54pt（合計）/ 44pt（訂單/付款）/ 38pt（時間/備註）/ 36pt（折扣）/ 35pt（品項）

---

### `deploy.sh`
一鍵部署腳本。在新機器上執行，自動完成所有設定：
```bash
bash pi-prototype/deploy.sh <username>
# 例：bash pi-prototype/deploy.sh pi53
```

**執行步驟：**
1. `git pull` 更新 repo
2. 建立 `/var/www/html/app.py` symlink → `pi-prototype/app.py`
3. 複製所有 templates 到 `/var/www/html/templates/`
4. 部署 `kiosk.sh` 到 `$HOME/kiosk.sh`
5. 用 `__USER__` 替換產生 `~/.config/autostart/kiosk.desktop`
6. 部署 `99-rs485.rules` 並 reload udev
7. 用 `__USER__` 替換產生 `noodle.service` 並安裝到 systemd
8. 如果 `settings.json` 不存在，複製預設值
9. `pip3 install -r requirements.txt --break-system-packages`
10. 重啟 `noodle.service`

---

### `noodle.service`
systemd 服務設定模板（`User=__USER__` 為佔位符，deploy 時替換）。

| 設定 | 說明 |
|------|------|
| `ExecStartPre` | 啟動前踢掉佔用 port 5000 的殭屍進程 |
| `nice -n 10` | 降低 CPU 優先度，讓 RS485/出單優先 |
| `Restart=on-failure` | 失敗自動重啟 |
| `StartLimitBurst=5` | 60 秒內最多重啟 5 次（防無限重啟風暴） |
| `TimeoutStopSec=10` | 10 秒內強制殺死舊進程 |

---

### `kiosk.sh`
開機後自動啟動 Chromium，進入 Kiosk 全螢幕模式。

**執行邏輯：**
1. 等待 Flask 服務啟動（`curl localhost:5000` 成功才繼續）
2. 關閉螢幕休眠（`gsettings` + `xset`）
3. 啟動 Chromium Kiosk 模式，開啟 `http://localhost:5000`

**重要**：不能加 `--ozone-platform=wayland`，pi52/pi53/pi54 都是 **LXDE/X11**。

---

### `autostart/kiosk.desktop`
LXDE 開機自動執行設定（`~/.config/autostart/kiosk.desktop`）。
`Exec=/home/__USER__/kiosk.sh`（deploy 時替換 `__USER__`）

---

### `99-rs485.rules`
udev 規則，讓 USB 轉 RS485 適配器固定對應 `/dev/ttyRS485`（不受插拔順序影響）。

支援三種 USB 晶片：
- VID `1a86` PID `55d3`（序號 5658002095）
- VID `0403` PID `6001`（序號 BG00OOM8）
- VID `1a86` PID `7523`（無序號，fallback）

---

### `settings.json`
升降台各動作的行程時間設定，由 `/api/settings` API 讀寫，儲存在 `/var/www/html/settings.json`。

格式：`act-<mode>-<channel>: <秒數>`，`mode-<n>-name`、`mode-<n>-sec`

---

### `requirements.txt`
```
flask, pyserial, pillow, requests, RPi.GPIO
```

---

### `templates/`

| 檔案 | 說明 |
|------|------|
| `index.html`（73KB） | 升降台控制主頁，RS485 推桿 6 通道、步進電機、溫控 |
| `orders.html` | 訂單列表頁 |
| `temp_curve.html`（29KB） | 溫度曲線圖表頁（PT100 感溫歷史） |
| `water_timer.html`（17KB） | 計時器頁 |
| `printer_changelog.html` | 出單機版本更新記錄頁（前端殼，對應 GCP changelog） |

---

## 四、pi52/（出單機專屬）

pi52 相對 prototype 多出的功能：印表機代理（proxy.py）。

### `proxy.py`（27KB）
UberEats 印表機代理核心，常駐執行（`printer-proxy.service`）。

**功能：**
- **UDP 3289**：廣播 ENPC 協定，偽裝成 Epson TM-m30II，讓 UberEats 平板自動發現 pi52
- **TCP 9100**：接收平板的 ESC/POS 列印資料，透過常駐連線轉發到真實印表機（192.168.1.124）
- **轉發鎖**：`_printer_send_lock` 保證同一時間只有一份資料在送（防多份收據互搶 buffer）
- **兩次 TCP**：收據和號碼牌分兩次獨立連線送出，中間 sleep 0.8s，避免 buffer overflow

**重要注意（曾踩雷）：**
- `WHO_IS_HOLDING` 回應的 IP 必須是 pi52 真實 IP，**不能寫死 `0.0.0.0`**
- `DLE EOT` 心跳回應（`0x10` 開頭）不能轉發給平板，否則平板誤判一直重連

### `printer-proxy.service`
| 設定 | 說明 |
|------|------|
| `ExecStart` | `python3 -u /home/pi52/proxy.py` |
| `Restart=always` | 永遠重啟（印表機代理不能停） |
| `StandardOutput` | 輸出到 `/home/pi52/proxy.log` |

### `noodle-app/test_render.py`（6.3KB）
本地出單格式測試腳本，不需要啟動服務，直接用 PIL 渲染一張測試收據 PNG。

```bash
python3 ~/raspi-system/pi52/noodle-app/test_render.py
# 輸出：/tmp/receipt_test.png
```

### `noodle-app/templates/`
pi52 專屬的 HTML 模板（和 `pi-prototype/templates/` 內容相同，**pi52 實際執行時用這裡的**）。

### `ocr_helper.py`（4.1KB）
OCR 輔助模組，解析 ESC/POS 點陣圖印表資料（UberEats 訂單識別用）。

### `crontab.txt`
```
0 3 * * *  truncate -s 0 /home/pi52/proxy.log
```
每天凌晨 3 點清空 proxy log，防止 log 無限增長。

### `settings.json`
pi52 的升降台動作設定（同 pi-prototype/settings.json 格式）。

---

## 五、pi53/ 和 pi54/（機器設定備份）

每台機器的專屬設定備份。和 `pi-prototype/` 的差異在於：
- `noodle.service`：`User=pi53` / `User=pi54`（已替換 `__USER__`）
- `autostart/kiosk.desktop`：路徑寫死為各機器 home 目錄
- `settings.json`：各機器的實際行程設定值
- `templates/index.html`：各機器的前端頁面（目前 pi53/pi54 相同）

**注意**：pi53/pi54 **沒有** `app.py`，因為三台共用 `pi-prototype/app.py`。

---

## 六、docs/（文件）

| 檔案 | 說明 |
|------|------|
| `pi_fleet.md` | Pi 機隊總覽（機器清單、架構、部署方式、Git 規範、ADR） |
| `zhongsheng_modbus.md` | 中盛科技 Modbus RTU 裝置手冊（繼電器、PT100、地址設定） |
| `01_深度相機視覺系統.md` | 深度相機視覺系統說明（早期研究，目前未啟用） |

---

## 七、fleet/nodes/（舊架構）

舊版 fleet 管理系統的節點設定檔，目前僅作參考。

```
pi51.conf / pi52.conf / pi53.conf / pi54.conf
```
每個檔案記錄：`NODE_USER`、`NODE_HOST`、`NODE_HOME`、`NODE_MODEL`、`NODE_ROLE`

---

## 八、scripts/

### `backup_dbs.sh`
每日自動備份 GCP 上的 SQLite DB 到 `pi-backups` repo（`git push`）。

備份對象：`menu.db`、`accounting.db`、`luzhou.db`、`luwei.db`

---

## 九、根目錄其他檔案

### `audit.sh`（7.2KB）
Pi 機隊健康檢查腳本。掃描所有節點，偵測異常，用 Claude API 分析，結果送 Telegram。

### `ubereats_auto.py`（8KB）
UberEats 自動整合腳本（**目前停用**）。
原本在 pi53 上每 5 秒 SSH 到 pi52 抓新訂單，OCR 解析後建到 luwei-manager DB。

### `.gitignore`
```
__pycache__/, *.pyc, *.pyo, .env, *.bak*, venv/, .venv/, *.db, *.sqlite
```
**DB 檔案不進 git**（訂單/設定資料在機器本地）。

### `docker-compose.yml`
早期 menu dashboard Docker 設定，目前未使用。

### `zip_watcher/`、`menu_dashboard/`
舊功能殘留，目前未使用。

---

## 十、各台機器的 symlink 關係

```
pi52: /var/www/html/app.py → /home/pi52/raspi-system/pi-prototype/app.py
pi53: /var/www/html/app.py → /home/pi53/raspi-system/pi-prototype/app.py
pi54: /var/www/html/app.py → /home/pi54/raspi-system/pi-prototype/app.py
```

每台機器都有自己的 repo clone，但三台都讀同一個路徑（`pi-prototype/app.py`），
所以只要 GitHub 上這一份更新，三台 `git pull` 後就全部同步。

---

## 十一、changelog 說明（補充）

出單機版本更新記錄存在 **GCP** 的 `~/data/printer.db`，可在以下頁面查看：
```
https://tong.tfooddata.com/ops/printer/changelog
```

目前 v9 記錄的就是現行的出單格式規格（字型尺寸、版面順序）。
此 changelog 尚未同步到 GitHub，僅在 GCP 維護。
