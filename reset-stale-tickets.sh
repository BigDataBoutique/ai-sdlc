#!/usr/bin/env bash
# Put stuck tickets back in the queue: every ticket that carries the work-queue label (any
# In Progress ticket when the project has none) and has sat In Progress for more than STALE_HOURS (default 6) goes back
# to Todo, with a comment saying so. The implement pass moves a ticket to In Progress before
# it starts and out of it when it ships or blocks, so one still there hours later belongs
# to a session that died. The next implement pass triages it again; if the dead session
# already opened a PR, that triage is a SKIP, so nothing is implemented twice.
#
# No LLM: scripts/tracker.py only (Linear via LINEAR_API_KEY in ./.env, or GitHub Issues via
# gh, per projects.yaml). The age is the time since the ticket last entered its current
# stage (`list --stage-since`: Linear's state history, GitHub's label events), not updatedAt,
# so a comment or an edit does not keep a dead ticket alive. Stateless: whatever moved is
# out of the query next pass.
#
# Nothing moves while an implement pass is running (its lock holds a live pid, see lock_live in
# scripts/env.sh): that pass claims its whole queue up front and may legitimately hold tickets
# for longer than STALE_HOURS, and it releases its own tickets when it ends. This script is for
# the pass that is gone without doing that.
#
# Cron (one pass per invocation):
#   */30 * * * * /path/to/ai-sdlc/reset-stale-tickets.sh >> /path/to/ai-sdlc/logs/reset-stale-tickets.log 2>&1
# Or long-running: INTERVAL=1800 ./reset-stale-tickets.sh    (sdlc.py does this for you)
#
# Env:
#   STALE_HOURS     default 6
#   INTERVAL        seconds between passes; unset = single pass (cron mode)
#   DRY_RUN=1       list what would move, change nothing
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
now() { date +%Y-%m-%dT%H:%M:%S%z; }   # `date -Is` is GNU-only; BSD date rejects it outright

ROOT=$(cd "$(dirname "$0")" && pwd)
. "$ROOT/scripts/env.sh"                     # TRACKER, QUEUE ... from projects.yaml
need_task reset                              # needs a todo stage in projects.yaml
STALE_HOURS=${STALE_HOURS:-6}
DRY_RUN=${DRY_RUN:-}
IMPLEMENT_LOCK=${IMPLEMENT_LOGDIR:-$ROOT/logs/$PROJECT/implement-tickets}/.lock.d   # implement-tickets.sh's LOGDIR

# Tickets older than STALE_HOURS in their stage: one {identifier, hours} per line.
stale_tickets() { # json array from `list --stage-since`
  jq -c --argjson h "$STALE_HOURS" '
    def ts: sub("\\.[0-9]+"; "") | fromdateiso8601;   # jq rejects fractional seconds
    .[] | ((.stageSince // .updatedAt) | ts) as $since
    | select(now - $since > $h * 3600)
    | {identifier, hours: ((now - $since) / 3600 | floor)}'
}

run_pass() {
  local tickets stale
  if lock_live "$IMPLEMENT_LOCK"; then
    echo "$(now) implement pass running (pid $(cat "$IMPLEMENT_LOCK/pid" 2>/dev/null || echo '?')), its tickets stay In Progress"; return 0
  fi
  tickets=$("$TRACKER" list --state in_progress ${QUEUE_ARGS[@]+"${QUEUE_ARGS[@]}"} --stage-since --json) || { echo "$(now) tracker query failed"; return 1; }
  stale=$(stale_tickets <<<"$tickets")
  echo "$(now) $(jq length <<<"$tickets") ${QUEUE:+$QUEUE }tickets In Progress, $(grep -c . <<<"$stale" || true) older than ${STALE_HOURS}h${DRY_RUN:+ (dry run)}"
  [ -n "$stale" ] || return 0
  while read -r t; do
    local id hours body
    id=$(jq -r .identifier <<<"$t"); hours=$(jq -r .hours <<<"$t")
    echo "$id back to Todo (In Progress for ${hours}h)"
    [ -z "$DRY_RUN" ] || continue
    body="Back to Todo: In Progress for ${hours}h with no hand-off, so the implementation pass that took it most likely died. The next pass picks it up again."
    { "$TRACKER" comment "$id" --body "$body" && "$TRACKER" move "$id" todo; } >/dev/null \
      || echo "$id FAILED to move"
  done <<<"$stale"
}

if [ -n "${INTERVAL:-}" ]; then
  while true; do
    run_pass || true
    echo "$(now) pass done, next in ${INTERVAL}s"
    sleep "$INTERVAL"
  done
else
  run_pass
fi
