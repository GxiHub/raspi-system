#!/usr/bin/env bash
set -euo pipefail

WATCH_DIR="${WATCH_DIR:-/watch}"
LOG_DIR="$WATCH_DIR/_logs"
LOCK_DIR="$WATCH_DIR/_locks"
DONE_DIR="$WATCH_DIR/_done"
JOBS_DIR="$WATCH_DIR/jobs"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT:-/host_dashboard/deploy_zip.sh}"
VERIFY_SCRIPT="${VERIFY_SCRIPT:-}"

mkdir -p "$LOG_DIR" "$LOCK_DIR" "$DONE_DIR" "$JOBS_DIR"
touch "$LOG_DIR/watcher.log" "$LOG_DIR/deploy.log"

# wlog 同時輸出到 stdout（docker logs 可見）和 log 檔
wlog() {
  local level="${1:-INFO}"
  local msg="$2"
  local line="[${level}] $(date '+%F %T') $msg"
  echo "$line"
  echo "$line" >> "$LOG_DIR/watcher.log"
}

wlog INFO "════════════════════════════════════════"
wlog INFO "zip-deploy-watcher 啟動"
wlog INFO "watch dir   : $WATCH_DIR"
wlog INFO "jobs dir    : $JOBS_DIR"
wlog INFO "deploy script: $DEPLOY_SCRIPT"
wlog INFO "verify script: ${VERIFY_SCRIPT:-disabled}"
wlog INFO "════════════════════════════════════════"

if [ ! -x "$DEPLOY_SCRIPT" ]; then
  wlog ERR "deploy script 不可執行：$DEPLOY_SCRIPT"
  exit 1
fi

cooldown_restart_staging() {
  local cooldown="$LOCK_DIR/_restart_staging.cooldown"
  local now last
  now="$(date +%s)"
  last="$(cat "$cooldown" 2>/dev/null || echo 0)"
  if [ $((now-last)) -ge 30 ]; then
    echo "$now" > "$cooldown"
    wlog INFO "▶ [RESTART] 重啟 menu-dashboard-staging..."
    if docker restart menu-dashboard-staging >>"$LOG_DIR/watcher.log" 2>&1; then
      wlog OK  "✓ [RESTART] menu-dashboard-staging 重啟完成"
    else
      wlog ERR "✗ [RESTART] menu-dashboard-staging 重啟失敗"
    fi
  else
    local remain=$((30 - (now-last)))
    wlog INFO "  [RESTART] cooldown 中，跳過重啟（剩 ${remain}s）"
  fi
}

wait_stable() {
  local f="$1" s1 s2
  s1="$(stat -c%s "$f" 2>/dev/null || echo -1)"
  sleep 0.4
  s2="$(stat -c%s "$f" 2>/dev/null || echo -1)"
  [[ "$s1" -gt 0 && "$s1" == "$s2" ]]
}

handle_zip() {
  local zpath="$1"
  local fname lock rc ts job_id job_dir job_zip fsize

  fname="$(basename "$zpath")"
  [[ "$fname" == *.zip ]] || return 0
  [[ "$fname" == _* ]] && return 0
  [[ "$fname" == .tmp_* ]] && { wlog INFO "  跳過暫存：$fname"; return 0; }
  [[ "$fname" == *.part ]] && { wlog INFO "  跳過暫存：$fname"; return 0; }

  if [ ! -f "$zpath" ]; then
    wlog WARN "  檔案已不存在（可能已被移動）：$zpath"
    return 0
  fi

  if ! wait_stable "$zpath"; then
    wlog WARN "  檔案尚未穩定，跳過：$fname"
    return 0
  fi

  fsize="$(du -sh "$zpath" 2>/dev/null | cut -f1 || echo '?')"

  lock="$LOCK_DIR/${fname}.lock"
  if ! ( set -o noclobber; echo "$(date +%s)" > "$lock" ) 2>/dev/null; then
    wlog WARN "  已鎖定，跳過：$fname"
    return 0
  fi
  trap "rm -f \"$lock\" 2>/dev/null || true" RETURN

  ts="$(date +%Y%m%d_%H%M%S)"
  job_id="${ts}__${fname%.zip}"
  job_id="${job_id// /_}"
  job_dir="$JOBS_DIR/$job_id"
  job_zip="$job_dir/upload.zip"

  mkdir -p "$job_dir/logs"

  wlog INFO "════════════════════════════════════════"
  wlog INFO "▶ [RECEIVED]  偵測到新 zip：$fname ($fsize)"
  wlog INFO "  [JOB]       job_id = $job_id"

  # 移動 zip 到 job 目錄
  if mv -f "$zpath" "$job_zip" 2>>"$LOG_DIR/watcher.log"; then
    wlog INFO "  [MOVE]      zip 已移入 job 目錄"
  else
    wlog WARN "  [MOVE]      mv 失敗，改用 cp"
    cp -f "$zpath" "$job_zip" >>"$LOG_DIR/watcher.log" 2>&1 || true
    mv -f "$zpath" "$DONE_DIR/${fname}.${ts}" >>"$LOG_DIR/watcher.log" 2>&1 || true
  fi

  # verify 階段
  if [ -n "$VERIFY_SCRIPT" ] && [ -x "$VERIFY_SCRIPT" ]; then
    wlog INFO "▶ [VERIFY]   執行 verify..."
    set +e
    bash "$VERIFY_SCRIPT" "$job_zip" "$job_id" >>"$job_dir/logs/verify.log" 2>&1
    vrc=$?
    set -e
    if [ "$vrc" -ne 0 ]; then
      wlog ERR "✗ [VERIFY]   失敗 rc=$vrc，部署中止 (job=$job_id)"
      wlog INFO "════════════════════════════════════════"
      return
    fi
    wlog OK  "✓ [VERIFY]   通過"
  fi

  # deploy 階段
  wlog INFO "▶ [DEPLOY]   開始部署 (job=$job_id)..."
  local t_start t_end elapsed
  t_start=$(date +%s)

  set +e
  bash "$DEPLOY_SCRIPT" "$job_zip" "$job_id" 2>&1 | tee -a "$job_dir/logs/deploy.log" | while IFS= read -r line; do
    echo "  $line"
    echo "  $line" >> "$LOG_DIR/watcher.log"
  done
  rc="${PIPESTATUS[0]}"
  set -e

  t_end=$(date +%s)
  elapsed=$((t_end - t_start))

  if [ "$rc" -eq 0 ]; then
    wlog OK  "✓ [DEPLOY]   部署成功，耗時 ${elapsed}s (job=$job_id)"
    wlog INFO "  [LOG]       $job_dir/logs/deploy.log"
    cooldown_restart_staging
  else
    wlog ERR "✗ [DEPLOY]   部署失敗 rc=$rc，耗時 ${elapsed}s (job=$job_id)"
    wlog ERR "  [LOG]       $job_dir/logs/deploy.log"
  fi

  wlog INFO "════════════════════════════════════════"
}

wlog INFO "開始監看 $WATCH_DIR ..."
inotifywait -m -e close_write,moved_to --format "%f" "$WATCH_DIR" 2>>"$LOG_DIR/watcher.log" | while read -r fname; do
  handle_zip "$WATCH_DIR/$fname" || true
done
