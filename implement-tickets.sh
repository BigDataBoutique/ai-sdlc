#!/usr/bin/env bash
# Implement every ticket in Todo (in the current cycle on Linear) that carries the work-queue
# label, when the project has one: one `claude -p` session runs the implement-tickets skill
# from this folder (see .claude/skills/implement-tickets/SKILL.md). The skill triages each ticket
# in a Sonnet subagent first, checked against the actual code on origin, not just the
# ticket text: one with an open PR already implementing it is skipped before any code is
# read; one already implemented on the default branch (no open PR or branch
# needed) moves straight to QA; a vague ticket (no code area, no expected behaviour, a
# "figure it out" over a whole repo) or one that turns out to contradict itself or the
# code goes back to Design with the label kept and a comment on what's missing or
# contradictory, so design-tickets.sh picks it up; one that needs an actual human call
# (product, scope, legal) goes to Requires Discussion instead. The rest are implemented
# in worktrees under ../.worktrees and shipped as PRs: on Sonnet when the ticket already
# names files, behaviour and tests, else on Opus. Models are aliases, so each run gets the
# latest of its family; an overloaded or unavailable model falls back along opus,fable.
#
# The queue comes from scripts/tracker.py (Linear via LINEAR_API_KEY in ./.env, or GitHub
# Issues via gh, per projects.yaml; on GitHub the Todo stage can simply be "no stage label"),
# minus tickets with an open blocker (`list --unblocked`: the tracker's own dependency, set
# by the skill with `tracker.py block`), and is handed to the skill as its arguments, already
# in priority order.
#
# Every queued ticket is moved to In Progress here, before the session starts, and no other
# queue lists an In Progress ticket: that is the claim that keeps a second pass (or a triage
# or design pass) off the same ticket. The skill moves each ticket on to its outcome stage
# (In Review, QA, Design, Requires Discussion, or back to Todo when blocked); whatever is
# still In Progress when the session ends, however it ends, goes back to Todo here. So there
# is no cache: a ticket is retried next pass.
#
# The lock directory holds this pass's pid; a lock whose pid is dead is taken over, one whose
# pid lives is honoured however old. reset-stale-tickets.sh checks the same pid and leaves a
# running pass's tickets alone. SIGTERM/SIGINT (sdlc.py's kill, or Ctrl-C) stop the session
# (scripts/claude_run.py forwards the signal to the session's process group) and release the
# tickets before exiting.
#
# Only tickets in the team's current cycle (Linear); `--all-cycles` (or ALL_CYCLES=1) takes
# the whole queue.
#
# Pass a ticket ID (`./implement-tickets.sh <ticket id>`) to implement just that ticket,
# whatever its state or labels; with no argument the Todo queue is processed.
#
# Cron (one pass per invocation):
#   0 */4 * * * /path/to/ai-sdlc/implement-tickets.sh >> /path/to/ai-sdlc/logs/implement-tickets.log 2>&1
# Or long-running: INTERVAL=14400 ./implement-tickets.sh    (sdlc.py does this for you)
#
# Env:
#   CLAUDE_FLAGS    permission policy (shell word list, eval-split). Default below allows
#                   edits, Bash (the tracker CLI included), Linear MCP, web research and
#                   subagents; blocks merges and pushes to a default branch.
#   MODEL           orchestrator model, default opus (an alias: the latest Opus). Triage and
#                   fully specified tickets run on Sonnet, designed ones and verification on
#                   Opus, whatever this is set to.
#   STALL_MIN       minutes of a silent event stream before a session is killed and resumed
#                   once with a nudge (default 30); a second stall ends the pass
#   INTERVAL        seconds between passes; unset = single pass (cron mode)
#   DRY_RUN=1       list the queue, don't spawn a session
#   ALL_CYCLES=1    same as --all-cycles
set -euo pipefail
usage() { echo "Usage: $0 [--all-cycles]"; }
ALL_CYCLES=${ALL_CYCLES:-}
case "$#:${1:-}" in
  0:) ;;
  1:--all-cycles) ALL_CYCLES=1 ;;
  1:--help|1:-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
now() { date +%Y-%m-%dT%H:%M:%S%z; }   # `date -Is` is GNU-only; BSD date rejects it outright

ROOT=$(cd "$(dirname "$0")" && pwd)          # this folder (CLAUDE.md + skills live here)
. "$ROOT/scripts/env.sh"                     # TRACKER, REPOS_ROOT, QUEUE ... from projects.yaml
need_task implement                          # needs a todo stage in projects.yaml
CYCLE=(); [ "$KIND" = linear ] && [ -z "$ALL_CYCLES" ] && CYCLE=(--cycle current)
LOGDIR=${LOGDIR:-$ROOT/logs/$PROJECT/implement-tickets}
MODEL=${MODEL:-opus}
# Sessions know only what this folder and the repo encode: no auto-memory written by earlier
# sessions (here or anywhere), no ~/.claude/CLAUDE.md, no user settings.json. Dropping the user
# source also drops the MCP servers registered in ~/.claude.json, so MCP servers (Linear, optional) come from
# .mcp.json here when it exists (its OAuth login is reused). --bare would go further but refuses OAuth altogether. A
# function, not more entries in claude_flags, so the helper calls below and a CLAUDE_FLAGS
# override are covered too. Effort was the one thing worth keeping from the user settings.
mcp_flags=(); [ -f "$ROOT/.mcp.json" ] && mcp_flags=(--mcp-config "$ROOT/.mcp.json")
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
claude() { command claude --setting-sources project,local --settings '{"effortLevel":"high"}' ${mcp_flags[@]+"${mcp_flags[@]}"} "$@"; }
# Array, not a string: unquoted $CLAUDE_FLAGS would split rules containing spaces.
if [ -n "${CLAUDE_FLAGS:-}" ]; then eval "claude_flags=($CLAUDE_FLAGS)"; else
  # Deny rules are command-prefix matches; the skill text is the real guard against pushing to a default branch.
  claude_flags=(--permission-mode acceptEdits
    --allowedTools "Bash,Read,Edit,Write,MultiEdit,NotebookEdit,Grep,Glob,Agent,${MCP_TOOLS}WebSearch,WebFetch"
    --disallowedTools "Bash(gh pr merge:*),Bash(git push origin master:*),Bash(git push origin main:*),Bash(git push --force:*),Bash(git push -f:*)")
fi
DRY_RUN=${DRY_RUN:-}
TICKET=${1:-}                                # optional: implement only this ticket
# Extra guidance for the implementing session, on top of the skill's own stacking and docs rules.
STACKING="A ticket that already has an open PR implementing it moves to In Review before any code is read. Fewest PRs and review passes: add a small related fix to an open PR you authored rather than opening another; put several small tickets touching the same code in one PR; stack only the steps of one ticket too big for one PR (docs/stacked-prs.md). A ticket that needs another ticket's unmerged work is blocked (tracker.py block), left in Todo and never implemented or stacked until that ticket is closed. Independent work gets its own PR off the default branch."
DOCS="Update the repo's CLAUDE.md and docs/ in the same PR whenever the change calls for it: a significant addition (a feature area, module, endpoint group, store, command or config key), a behavior change, or anything that makes a doc wrong (moved, renamed, removed). Follow the Repo docs rules in $ROOT/docs/code-style.md: describe the shape, not the detail, and delete what's no longer true. What else must land with it (a docs-site page, another repo's leg) is in $CONTEXT/cross-repo-changes.md when the project has one."
# Project-specific rules for the implementing session, when the project's context has any.
[ ! -s "$CONTEXT/implement-notes.md" ] || DOCS="$DOCS $(cat "$CONTEXT/implement-notes.md")"

mkdir -p "$LOGDIR"
# mkdir lock, not flock: macOS has no flock (util-linux), so `flock -n 9 ||` fired its
# failure branch on every Mac and the pass exited as "already running" without doing
# anything. mkdir is atomic everywhere. The directory holds this pass's pid (lock_live in
# scripts/env.sh): a pass that died leaves a dead pid and is taken over at once, a long pass
# keeps its lock however long it runs. Age alone used to decide, and a 6h pass lost its lock
# to the next one while still implementing.
LOCKDIR="$LOGDIR/.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if lock_live "$LOCKDIR"; then echo "$(now) previous run still going, exiting"; exit 0; fi
  echo "$(now) lock left by a dead pass, taking over"
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT

PASS_IDS=      # the tickets this pass claimed, for the signal handler
SESSION_PID=   # the running claude_run.py, if any
# A signal (sdlc.py's kill, Ctrl-C) stops the session and puts its tickets back. The session
# runs in the background and the script waits on it, because a trap only fires during `wait`,
# never while a foreground child runs.
on_signal() {
  echo "$(now) interrupted: stopping the session and releasing its tickets"
  if [ -n "$SESSION_PID" ]; then kill -TERM "$SESSION_PID" 2>/dev/null || true; wait "$SESSION_PID" 2>/dev/null || true; fi
  release_in_progress "$PASS_IDS"
  exit 143
}
trap on_signal TERM INT

run_pass() {
  if [ -n "$TICKET" ]; then
    n=1; ids=$TICKET; urls=$("$TRACKER" get "$TICKET" --json | jq -r .url) || urls=$TICKET
    echo "$(now) single ticket $urls${DRY_RUN:+ (dry run)}"
  else
    # Already sorted: priority first (Urgent .. Low, none last), then oldest created first.
    # --unblocked: a ticket whose blocker is still open is not implemented, however ready it looks.
    issues=$("$TRACKER" list --state todo --unblocked ${QUEUE_ARGS[@]+"${QUEUE_ARGS[@]}"} ${CYCLE[@]+"${CYCLE[@]}"} --json) || { echo "$(now) tracker query failed"; return 1; }
    n=$(jq 'length' <<<"$issues")
    ids=$(jq -r '[.[].identifier] | join(" ")' <<<"$issues")
    urls=$(jq -r '[.[] | .url // .identifier] | join(" ")' <<<"$issues")
    echo "$(now) $n Todo tickets${QUEUE:+ with $QUEUE}${CYCLE[*]:+ in the current cycle}${DRY_RUN:+ (dry run)}: $urls"
  fi
  [ "$n" -gt 0 ] || return 0

  # Claim every ticket before the session starts: In Progress is what keeps every other pass
  # (a second implement pass included) off it. One the tracker refuses is left out of the run.
  ids=$(claim "$ids"); PASS_IDS=$ids
  [ -n "$ids" ] && [ -z "$DRY_RUN" ] || return 0

  "$ROOT/pull-all.sh" || true                # worktrees branch off a fresh master; failures are logged there
  log="$LOGDIR/pass.$(date +%s).log"
  echo "$(now) implementing $ids, see $log"
  # claude_run.py waits for background subagents (no 600s print-mode ceiling) and kills and
  # resumes a session whose stream goes quiet for STALL_MIN minutes; exit 124 = stalled twice.
  rc=0
  (cd "$ROOT" && echo "/implement-tickets $ids" | "$ROOT/scripts/claude_run.py" "$log" -- "$(command -v claude)" \
    --setting-sources project,local --settings '{"effortLevel":"high"}' ${mcp_flags[@]+"${mcp_flags[@]}"} \
    --model "$MODEL" --fallback-model opus,fable --add-dir "$REPOS_ROOT" --append-system-prompt "$STACKING $DOCS" "${claude_flags[@]}") 2>&1 &
  SESSION_PID=$!
  wait "$SESSION_PID" || rc=$?
  SESSION_PID=
  release_in_progress "$ids"; PASS_IDS=
  if [ "$rc" -eq 0 ]; then
    echo "$(now) done:"; tail -n "$n" "$log"
  elif grep -qiE 'usage credits|usage limit|rate limit' "$log"; then
    echo "$(now) pass aborted: out of credits / rate limited, see $log"
  else
    echo "$(now) claude FAILED (exit $rc), see $log"; return 1
  fi
}

claim() { # ids -> the ids now In Progress, space-separated (progress on stderr: stdout is the result)
  local id out= msg
  for id in $1; do
    if msg=$("$TRACKER" move "$id" in_progress 2>&1); then
      out="$out $id"
      if [ -n "$DRY_RUN" ]; then echo "$id $msg" >&2; else echo "$id claimed: In Progress" >&2; fi
    else echo "$id claim failed ($msg), left out of this pass" >&2; fi
  done
  echo "${out# }"
}

# Whatever the session left In Progress goes back to Todo now: a session that died, stalled or
# simply forgot the hand-off. The next pass triages the ticket again, and one with an open PR
# moves to In Review there, so nothing is implemented twice.
release_in_progress() { # ids
  local id
  [ -n "$1" ] || return 0
  for id in $("$TRACKER" list --state in_progress --json | jq -r --arg ids "$1" '.[] | select(.identifier as $i | $ids | split(" ") | index($i)) | .identifier'); do
    echo "$id back to Todo (left In Progress by the session)"
    { "$TRACKER" comment "$id" --body "Back to Todo: the implementation session ended without handing this off. The next pass picks it up again." \
      && "$TRACKER" move "$id" todo; } >/dev/null || echo "$id FAILED to move"
  done
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
