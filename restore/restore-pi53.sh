#!/bin/bash
# ============================================================
# pi53 一鍵還原腳本
# 用法：bash restore-pi53.sh
# 適用：新 SD 卡、全新 Raspberry Pi OS，SSH 可連入後執行
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()  { echo -e "${GREEN}✓ $1${NC}"; }
warn(){ echo -e "${YELLOW}⚠ $1${NC}"; }
err() { echo -e "${RED}✗ $1${NC}"; exit 1; }
step(){ echo -e "\n${YELLOW}━━ $1 ━━${NC}"; }

echo "=================================================="
echo "  pi53 一鍵還原腳本"
echo "  $(date '+%Y-%m-%d %H:%M')"
echo "=================================================="

# ── 收集必要 Token ──────────────────────────────────────────
step "收集設定"

read -p "GitHub Token (ghp_... 或 github_pat_...): " GITHUB_TOKEN
[ -z "$GITHUB_TOKEN" ] && err "GitHub Token 必填"

read -p "Tailscale Auth Key (tskey-auth-...，留空跳過): " TS_KEY

read -p "Cloudflare Tunnel Token (eyJ...，留空跳過): " CF_TOKEN

# ── 系統套件 ───────────────────────────────────────────────
step "1/9 安裝系統套件"
sudo apt-get update -qq
sudo apt-get install -y git tmux curl python3-pip python3-venv sqlite3 \
  python3-flask nodejs npm 2>&1 | tail -3
ok "套件安裝完成"

# ── sudo 免密 ──────────────────────────────────────────────
step "2/9 設定 sudo 免密"
echo "$(whoami) ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/$(whoami)-nopasswd > /dev/null
sudo chmod 440 /etc/sudoers.d/$(whoami)-nopasswd
ok "sudo 免密設定完成"

# ── Clone Repos ────────────────────────────────────────────
step "3/9 Clone 所有 Repos"
cd ~

PRIVATE="https://GxiHub:${GITHUB_TOKEN}@github.com/GxiHub"
PUBLIC="https://github.com/GxiHub"

for repo in dashboard-v2 luwei-manager pi-backups raspi-dotfiles; do
  [ -d "$repo" ] && { warn "$repo 已存在，跳過"; continue; }
  git clone "${PRIVATE}/${repo}.git" 2>&1 | tail -1 && ok "$repo"
done

for repo in luwei-accounting menu-dashboard raspi-system; do
  [ -d "$repo" ] && { warn "$repo 已存在，跳過"; continue; }
  git clone "${PUBLIC}/${repo}.git" 2>&1 | tail -1 && ok "$repo"
done

# ── 建立目錄結構 ───────────────────────────────────────────
step "4/9 建立目錄結構"
mkdir -p ~/dashboard/{current,staging_dir,incoming,logs,bin,_prod_backup,_work}
mkdir -p ~/dashboard/current/data
mkdir -p ~/dashboard/staging_dir/dashboard/slides
mkdir -p ~/accounting/data
mkdir -p ~/db_backups ~/logs

# 複製 menu-dashboard 內容到 current
cp -r ~/menu-dashboard/staging_dir/* ~/dashboard/current/ 2>/dev/null || true
cp ~/menu-dashboard/staging_dir/dashboard/slides/*.md \
   ~/dashboard/staging_dir/dashboard/slides/ 2>/dev/null || true
cp -r ~/menu-dashboard/bin/* ~/dashboard/bin/ 2>/dev/null || true
ok "目錄結構建立完成"

# ── 還原資料庫 ─────────────────────────────────────────────
step "5/9 還原資料庫（從 pi-backups）"
BACKUP_DIR=~/pi-backups/pi5

restore_db() {
  local sql="$1" dest="$2" name="$3"
  if [ -f "$sql" ]; then
    [ -f "$dest" ] && cp "$dest" "${dest}.bak.$(date +%Y%m%d)" 2>/dev/null || true
    sqlite3 "$dest" < "$sql" && ok "$name 還原完成" || warn "$name 還原失敗"
  else
    warn "$name：備份 SQL 不存在（$sql）"
  fi
}

restore_db "$BACKUP_DIR/menu.sql"              ~/dashboard/current/data/menu.db    "menu.db（PIN碼/品項）"
restore_db "$BACKUP_DIR/accounting.sql"        ~/accounting/data/accounting.db     "accounting.db（中和帳務）"
restore_db "$BACKUP_DIR/accounting_luzhou.sql" ~/accounting/data/luzhou.db         "luzhou.db（蘆洲帳務）"
restore_db "$BACKUP_DIR/luwei_pos.sql"         ~/luwei-manager/instance/luwei.db  "luwei.db（POS）"

# ── 安裝 Python 依賴 ───────────────────────────────────────
step "6/9 安裝 Python 依賴"
for dir in dashboard-v2 luwei-manager luwei-accounting; do
  [ -f ~/$dir/requirements.txt ] && \
    pip install -r ~/$dir/requirements.txt --break-system-packages -q 2>&1 | tail -1 && ok "$dir"
done

# ── Systemd Services ───────────────────────────────────────
step "7/9 建立 Systemd Services"
USER=$(whoami)

create_service() {
  local name="$1" desc="$2" dir="$3" cmd="$4"
  sudo tee /etc/systemd/system/${name}.service > /dev/null << EOF
[Unit]
Description=${desc}
After=network.target

[Service]
User=${USER}
WorkingDirectory=${dir}
ExecStart=${cmd}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl enable "$name" 2>/dev/null
  ok "$name service 建立完成"
}

create_service "dashboard-v2"    "Dashboard V2 (tong)"        "/home/${USER}/dashboard-v2"    "/usr/bin/python3 app.py"
create_service "luwei-manager"   "Luwei Manager POS"          "/home/${USER}/luwei-manager"   "/usr/bin/python3 run.py"
create_service "luwei-accounting" "Luwei Accounting System"   "/home/${USER}/luwei-accounting" "/usr/bin/python3 app.py"

# 設定 luwei-accounting DB 路徑
sudo sed -i "s|ExecStart=/usr/bin/python3 app.py|ExecStart=/usr/bin/python3 app.py\nEnvironment=DB_PATH=/home/${USER}/accounting/data/accounting.db|" \
  /etc/systemd/system/luwei-accounting.service 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl start dashboard-v2 luwei-manager luwei-accounting
ok "所有服務啟動完成"

# ── Tailscale ──────────────────────────────────────────────
step "8/9 Tailscale"
if ! command -v tailscale &> /dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sudo sh 2>&1 | tail -2
fi

if [ -n "$TS_KEY" ]; then
  sudo tailscale up --auth-key="$TS_KEY" --accept-routes 2>&1 && ok "Tailscale 自動授權完成"
else
  warn "Tailscale 需手動授權：執行 'sudo tailscale up' 並用瀏覽器登入"
fi

# ── Docker + cloudflared ───────────────────────────────────
step "9/9 Docker + Cloudflared Tunnel"
if ! command -v docker &> /dev/null; then
  curl -fsSL https://get.docker.com | sudo sh 2>&1 | tail -2
  sudo usermod -aG docker $USER
  ok "Docker 安裝完成"
fi

# cloudflared
if ! command -v cloudflared &> /dev/null; then
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | \
    sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
  echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' | \
    sudo tee /etc/apt/sources.list.d/cloudflared.list
  sudo apt-get update -qq && sudo apt-get install -y cloudflared 2>&1 | tail -2
  ok "cloudflared 安裝完成"
fi

if [ -n "$CF_TOKEN" ]; then
  # 從 token 解碼 credentials
  TUNNEL_ID=$(echo "$CF_TOKEN" | base64 -d 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['t'])" 2>/dev/null || echo "")
  if [ -n "$TUNNEL_ID" ]; then
    sudo mkdir -p /etc/cloudflared
    echo "$CF_TOKEN" | base64 -d | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps({'AccountTag': d['a'], 'TunnelSecret': d['s'], 'TunnelID': d['t']}, indent=2))
" | sudo tee /etc/cloudflared/${TUNNEL_ID}.json > /dev/null
    sudo chmod 644 /etc/cloudflared/${TUNNEL_ID}.json

    sudo tee /etc/cloudflared/config.yml > /dev/null << EOF
tunnel: ${TUNNEL_ID}
credentials-file: /etc/cloudflared/${TUNNEL_ID}.json

ingress:
  - hostname: tong.tfooddata.com
    service: http://localhost:5010
  - hostname: dashboard.tfooddata.com
    service: http://localhost:8501
  - service: http_status:404
EOF

    sudo docker stop cloudflared 2>/dev/null || true
    sudo docker rm cloudflared 2>/dev/null || true
    sudo docker run -d --name cloudflared --restart unless-stopped \
      --network host \
      -v /etc/cloudflared:/etc/cloudflared:ro \
      cloudflare/cloudflared:latest \
      tunnel --no-autoupdate --config /etc/cloudflared/config.yml run 2>&1 | tail -2
    ok "Cloudflare Tunnel 啟動完成（$TUNNEL_ID）"
  fi
else
  warn "Cloudflare Tunnel Token 未提供，請手動設定"
  warn "執行：cloudflared tunnel login → cloudflared tunnel token <tunnel-name>"
fi

# ── GitHub Token 儲存 ──────────────────────────────────────
git config --global user.email "pi53@local"
git config --global user.name "pi53"
git config --global credential.helper store
echo "https://GxiHub:${GITHUB_TOKEN}@github.com" > ~/.git-credentials
chmod 600 ~/.git-credentials

# 設定各 repo remote 帶 token
for repo in dashboard-v2 luwei-manager pi-backups; do
  [ -d ~/$repo/.git ] && \
    cd ~/$repo && \
    git remote set-url origin "https://GxiHub:${GITHUB_TOKEN}@github.com/GxiHub/${repo}.git"
done
ok "GitHub 認證設定完成"

# ── Crontab ────────────────────────────────────────────────
cat > /tmp/pi53_crontab << 'CRONEOF'
# luwei-accounting 每天 00:00 auto-backup
0 0 * * * cd ~/luwei-accounting && git add -A && git diff --cached --quiet || (git commit -m "auto-backup $(date +'%Y-%m-%d %H:%M')" && git push origin main) >> ~/logs/backup.log 2>&1

# menu-dashboard 每小時 auto-backup
0 * * * * cd ~/menu-dashboard && git add -A && git diff --cached --quiet || (git commit -m "auto-backup $(date +'%Y-%m-%d %H:%M')" && git push origin main) >> ~/logs/backup.log 2>&1

# dashboard-v2 每天 00:30 auto-backup
30 0 * * * cd ~/dashboard-v2 && git add -A && git diff --cached --quiet || (git commit -m "auto-backup $(date +'%Y-%m-%d %H:%M')" && git push origin main) >> ~/logs/backup.log 2>&1

# DB 每天 01:00 dump 到 pi-backups（所有 DB 一次備份）
0 1 * * * bash ~/raspi-system/scripts/backup_dbs.sh >> ~/logs/backup.log 2>&1

# pi52 rsync DB 備份
15 1 * * * mkdir -p ~/db_backups && rsync -av ~/db_backups/ pi52@100.98.225.85:/home/pi52/db_backups/ >> ~/logs/backup.log 2>&1
CRONEOF
crontab /tmp/pi53_crontab
ok "Crontab 設定完成"

# ── 完成 ───────────────────────────────────────────────────
echo ""
echo "=================================================="
echo -e "${GREEN}  ✅ 還原完成！${NC}"
echo "=================================================="
echo ""
echo "服務狀態："
systemctl is-active dashboard-v2 luwei-manager luwei-accounting | paste - - - <(echo -e "dashboard-v2\nluwei-manager\nluwei-accounting")
echo ""
echo "⚠️  注意事項："
[ -z "$TS_KEY"  ] && echo "  • Tailscale：執行 'sudo tailscale up' 完成授權"
[ -z "$CF_TOKEN" ] && echo "  • Cloudflare Tunnel：手動設定 /etc/cloudflared/"
echo "  • 重新登入 SSH 讓 docker group 生效"
echo ""
