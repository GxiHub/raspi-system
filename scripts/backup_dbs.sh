#!/bin/bash
# 每日 DB dump → pi-backups repo
set -e
BACKUP_DIR=$HOME/pi-backups/pi5
LOG() { echo "[$(date '+%H:%M:%S')] $1"; }

LOG 'DB backup 開始'

dump_db() {
  local src="$1" dst="$2" name="$3"
  [ -f "$src" ] || { LOG "跳過 $name（不存在）"; return; }
  sqlite3 "$src" .dump > "$dst" && LOG "✓ $name dump 完成" || LOG "✗ $name dump 失敗"
}

dump_db ~/dashboard/current/data/menu.db              $BACKUP_DIR/menu.sql              'menu.db'
dump_db ~/accounting/data/accounting.db               $BACKUP_DIR/accounting.sql        'accounting.db'
dump_db ~/accounting/data/luzhou.db                   $BACKUP_DIR/accounting_luzhou.sql 'luzhou.db'
dump_db ~/luwei-manager/instance/luwei.db             $BACKUP_DIR/luwei_pos.sql         'luwei.db'

# push 到 GitHub
cd ~/pi-backups
git add -A
git diff --cached --quiet || (
  git commit -m "auto-backup $(date +'%Y-%m-%d %H:%M')" && git push origin main
)
LOG 'DB backup 完成，已推上 GitHub'
