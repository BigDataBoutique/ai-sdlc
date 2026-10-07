#!/usr/bin/env bash
# Keeps logs/ bounded: deletes per-run logs (logs/<dir>/...) older than LOG_RETENTION_DAYS and trims
# the append-only logs/*.log to their last LOG_KEEP_LINES lines. env.sh runs it; it does the work at
# most once a day (--force skips that check). LOG_RETENTION_DAYS=0 turns it off.
set -u
ROOT=${ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
LOGS="$ROOT/logs"
DAYS=${LOG_RETENTION_DAYS:-14}
KEEP=${LOG_KEEP_LINES:-20000}
STAMP="$LOGS/.pruned"
[ -d "$LOGS" ] && [ "$DAYS" -gt 0 ] || exit 0
[ "${1:-}" = --force ] || [ -z "$(find "$STAMP" -mmin -1440 2>/dev/null)" ] || exit 0
touch "$STAMP"
# Lock directories (*.lock.d) are left to the runner that owns them.
find "$LOGS" -mindepth 2 -type f -mtime +"$DAYS" ! -path '*.lock.d/*' -delete
for f in "$LOGS"/*.log; do
  [ -f "$f" ] && [ "$(wc -l < "$f")" -gt "$KEEP" ] || continue
  # Rewrite in place rather than mv: sdlc.py and cron redirects append to the same inode.
  tail -n "$KEEP" "$f" > "$f.tmp" && cat "$f.tmp" > "$f"
  rm -f "$f.tmp"
done
