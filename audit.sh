#!/bin/bash
# Pi Fleet Audit Bot
# 掃所有節點，偵測變化，用 Claude 分析，送 Telegram

set -euo pipefail

AUDIT_DIR="$HOME/logs/audit"
SNAP_DIR="$AUDIT_DIR/snapshots"
TODAY=$(date '+%Y-%m-%d_%H%M')
RAW_REPORT="$AUDIT_DIR/raw_$TODAY.md"
FINAL_REPORT="$AUDIT_DIR/report_$TODAY.md"

# Telegram
ENV_FILE="$HOME/dashboard/.telegram_env"
TELEGRAM_TOKEN=$(grep TELEGRAM_TOKEN "$ENV_FILE" | cut -d= -f2)
TELEGRAM_CHAT_ID=$(grep TELEGRAM_CHAT_ID "$ENV_FILE" | cut -d= -f2)

tg_send() {
  local text="$1"
  python3 - "$TELEGRAM_TOKEN" "$TELEGRAM_CHAT_ID" "$text" << 'PY'
import sys, urllib.request, urllib.parse
token, chat_id, text = sys.argv[1], sys.argv[2], sys.argv[3]
if len(text) > 4000: text = text[:4000] + "\n...(截斷)"
data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
try:
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15)
except Exception as e:
    data2 = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data2, timeout=15)
PY
}

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── 掃描單一節點 ───────────────────────────────────────────────────────
scan_node() {
  local name=$1 addr=$2
  log "掃描 $name ($addr)..."

  local snap_file="$SNAP_DIR/${name}_services.txt"
  local prev_snap=""
  [ -f "$snap_file" ] && prev_snap=$(cat "$snap_file")

  # 遠端收集
  local data
  data=$(ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no "$addr" \
    "timeout 25 bash -s" << 'REMOTE' 2>/dev/null || echo "UNREACHABLE"
echo "##SERVICES##"
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -v -E '^(systemd|dbus|getty|user@|polkit|accounts|udisks|avahi|wpa_)'

echo "##ENABLED_CUSTOM##"
systemctl list-unit-files --state=enabled --no-pager --no-legend 2>/dev/null \
  | awk '{print $1}' | grep -v -E '^(systemd|apt|cups|e2scrub|fstrim|logrotate|man-db|dpkg|rpcbind|nfs|cloud|console|keyboard|getty|glamor|rp1|sshswitch|regenerate|rpi-|wayvnc|NetworkManager|ModemManager|bluetooth|avahi|wpa_|apparmor|accounts|udisks|upower|polkit)'

echo "##GIT_STATUS##"
for d in $(find ~ -maxdepth 3 -name '.git' -type d 2>/dev/null | head -10); do
  repo=$(dirname "$d")
  name_r=$(basename "$repo")
  remote=$(cd "$repo" && git remote get-url origin 2>/dev/null || echo "no-remote")
  uncommitted=$(cd "$repo" && git status --short 2>/dev/null | wc -l | tr -d ' ')
  unpushed=$(cd "$repo" && git log origin/main..HEAD 2>/dev/null | grep -c "^commit" || echo "?")
  echo "$name_r | remote=$remote | uncommitted=$uncommitted | unpushed=$unpushed"
done

echo "##UNTRACKED_FILES##"
# 找 home 下不在任何 git repo 的 .py .sh .js 檔（近 30 天修改）
find ~ -maxdepth 3 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.js' -o -name '*.service' \) \
  -newer ~/.bash_logout \
  -not -path '*/.git/*' -not -path '*/__pycache__/*' \
  -not -path '*/venv/*' -not -path '*/.cache/*' \
  -not -path '*/node_modules/*' -not -path '*/releases/*' \
  2>/dev/null | while read f; do
    in_git=$(cd "$(dirname "$f")" && git rev-parse --show-toplevel 2>/dev/null || echo "")
    [ -z "$in_git" ] && echo "$f"
  done | head -20

echo "##OPEN_PORTS##"
ss -tlnp 2>/dev/null | grep LISTEN | awk '{print $4}' | grep -oP ':\K\d+' | sort -n | uniq

echo "##DISK##"
df -h / | tail -1 | awk '{print $5 " used (" $3 "/" $2 ")"}'

echo "##CRONTAB##"
crontab -l 2>/dev/null | grep -v '^#' | grep -v '^$' | wc -l
REMOTE
  )

  if echo "$data" | grep -q "UNREACHABLE"; then
    echo -e "\n### $name ❌ 無法連線\n" >> "$RAW_REPORT"
    return
  fi

  # 儲存 services snapshot，計算 delta
  local curr_services
  curr_services=$(echo "$data" | awk '/##SERVICES##/{f=1;next}/##/{f=0}f')
  local new_services=""
  if [ -n "$prev_snap" ]; then
    new_services=$(comm -13 <(echo "$prev_snap" | sort) <(echo "$curr_services" | sort))
  fi
  echo "$curr_services" > "$snap_file"

  # 寫入 raw report
  {
    echo ""
    echo "### $name"
    echo ""

    echo "**執行中服務：**"
    echo "$curr_services" | sed 's/^/- /'

    if [ -n "$new_services" ]; then
      echo ""
      echo "**⚠️ 上次掃描後新增的服務：**"
      echo "$new_services" | sed 's/^/- 🆕 /'
    fi

    echo ""
    echo "**Git 狀態：**"
    echo "$data" | awk '/##GIT_STATUS##/{f=1;next}/##/{f=0}f' | sed 's/^/- /'

    local untracked
    untracked=$(echo "$data" | awk '/##UNTRACKED_FILES##/{f=1;next}/##/{f=0}f')
    if [ -n "$untracked" ]; then
      echo ""
      echo "**⚠️ 不在 git 的新檔案（近期修改）：**"
      echo "$untracked" | sed 's/^/- /'
    fi

    echo ""
    echo "**開放 Port：** $(echo "$data" | awk '/##OPEN_PORTS##/{f=1;next}/##/{f=0}f' | tr '\n' ' ')"
    echo "**磁碟：** $(echo "$data" | awk '/##DISK##/{f=1;next}/##/{f=0}f')"
    echo "**Crontab 條目：** $(echo "$data" | awk '/##CRONTAB##/{f=1;next}/##/{f=0}f')"
  } >> "$RAW_REPORT"
}

# ── 主流程 ─────────────────────────────────────────────────────────────
log "=== Pi Fleet Audit 開始 ==="

{
  echo "# Pi Fleet Audit Report"
  echo "**時間：** $(date '+%Y-%m-%d %H:%M')"
  echo ""
  echo "---"
} > "$RAW_REPORT"

# 掃描 pi53 自己
scan_node "pi53(self)" "pi53@localhost"

# 掃描其他節點
scan_node "pi52" "pi52@100.98.225.85"
scan_node "pi54" "pi54@100.70.170.115"
# scan_node "pi51" "pi51@100.118.76.98"  # 待上線

log "原始報告完成，送 Claude 分析..."

# ── Claude 分析 ────────────────────────────────────────────────────────
RAW_CONTENT=$(cat "$RAW_REPORT")

CLAUDE_PROMPT="你是 Pi 系統管理員助手。以下是多台 Raspberry Pi 的掃描報告，請幫我分析並輸出行動清單。

掃描報告：
$RAW_CONTENT

請輸出以下格式（用繁體中文）：

🔴 需要立刻處理
（列出：有未 commit 修改、有未 push commit、重要檔案不在 git、服務異常等）

🟡 建議處理
（列出：新服務沒有文件、腳本沒有 GitHub 備份、port 沒有 auth 保護等）

🟢 狀態良好
（列出確認 OK 的項目）

📊 整體評分：X/10

每條只寫一行，格式：• [節點名] 問題描述 → 建議動作
不要廢話，直接給清單。"

ANALYSIS=$(claude -p --dangerously-skip-permissions "$CLAUDE_PROMPT" 2>/dev/null || echo "Claude 分析失敗")

# 儲存完整報告
{
  echo ""
  echo "---"
  echo ""
  echo "## AI 分析結果"
  echo ""
  echo "$ANALYSIS"
} >> "$RAW_REPORT"

cp "$RAW_REPORT" "$FINAL_REPORT"

log "分析完成，送 Telegram..."

# ── 送 Telegram ────────────────────────────────────────────────────────
MSG="🤖 <b>Pi Fleet Audit - $(date '+%m/%d %H:%M')</b>

$ANALYSIS"

tg_send "$MSG"

log "=== Audit 完成 ==="
log "報告：$FINAL_REPORT"
