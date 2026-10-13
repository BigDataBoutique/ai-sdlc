#!/usr/bin/env bash
# Make every ticket in the Design stage (in the current cycle on Linear) that carries the
# work-queue label, when the project has one, ready for implementation: one `claude -p`
# session per ticket researches the codebase (and the web if needed) and appends a design
# section to the ticket. The script then checks the design landed and the original description
# survived, and finishes whatever bookkeeping the session left behind (assign to me, hand the
# ticket off) through scripts/tracker.py.
# The hand-off is what takes the ticket out of the queue, so no "done" bookkeeping is needed
# beyond a small failure cache: with a queue label it drops the label and leaves the stage to
# a human; a project with no queue label (`tracker.queue_label` unset) moves the ticket to Todo.
#
# The design section ends in an "Implementation instructions" block: imperative lines per
# PR naming the files, the interfaces to preserve, the tests to add, the commands to run
# and an acceptance check. implement-tickets.sh triages every Todo
# ticket before implementing it and sends a vague one here (state Design, queue label kept,
# a comment listing what is missing); a ticket carrying that block is what its triage
# rates specific enough to hand to a cheaper model. With a queue label, moving the designed
# ticket back to Todo and re-adding the label stays a human call.
#
# The session runs on Opus and writes the design; the codebase research, where the deep
# reasoning is, runs in a Fable subagent and the adversarial check in an Opus one. Models are
# aliases, so each run gets the latest of its family. An overloaded or unavailable model falls
# back along opus,fable; a session that fails anyway (refusal, its own rate limit) is retried
# once on FALLBACK_MODEL before the ticket is cached as failed.
#
# The tracker (Linear or GitHub Issues, per projects.yaml) is reached through
# scripts/tracker.py: the queue, the ticket text handed to the session, and the label/assignee
# writes are plain API calls, no Claude turn involved. Linear needs LINEAR_API_KEY in ./.env
# (see .env.example); GitHub Issues use the gh login.
#
# Skip rules:
#   - a session already ran (and failed, or exited 0 without a design landing) on this exact
#     ticket revision (identifier + updatedAt, cached) - delete the cache to retry.
#
# Cron (one pass per invocation):
#   7,37 * * * * /path/to/ai-sdlc/design-tickets.sh >> /path/to/ai-sdlc/logs/design-tickets.log 2>&1
# Or long-running (a pass every 30 min, e.g. under tmux/systemd):
#   INTERVAL=1800 ./design-tickets.sh
# Only tickets in the team's current cycle (Linear); `--all-cycles` (or ALL_CYCLES=1) takes
# the whole queue:
#   ./design-tickets.sh --all-cycles
# Or design one ticket, regardless of state, label, cycle, or cached revision (runs once):
#   ./design-tickets.sh <ticket id>      (Linear: ABC-123, GitHub Issues: repo#123)
#
# Env:
#   CLAUDE_FLAGS    permission policy for the sessions (cron can't answer prompts), as a
#                   shell word list (eval-split, so quote args containing spaces). Default
#                   below allows the tracker CLI, the Linear MCP (linear trackers), gh,
#                   read-only git/repo access, web research and subagents; blocks edits, git
#                   writes, merges and pushes.
#   MODEL           session model, default opus
#   FALLBACK_MODEL  default fable, tried once per issue when the MODEL session
#                   fails; set to the empty string to disable the retry
#   INTERVAL        seconds between passes; unset = single pass (cron mode)
#   DRY_RUN=1       list decisions, don't spawn sessions or touch the cache or the tracker
#   ALL_CYCLES=1    same as --all-cycles
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
now() { date +%Y-%m-%dT%H:%M:%S%z; }   # `date -Is` is GNU-only; BSD date rejects it outright

usage() { echo "Usage: $0 [--all-cycles | TICKET-ID]"; }
if [ "$#" -eq 1 ] && { [ "$1" = --help ] || [ "$1" = -h ]; }; then
  usage; exit 0
fi
ALL_CYCLES=${ALL_CYCLES:-}
if [ "$#" -eq 1 ] && [ "$1" = --all-cycles ]; then ALL_CYCLES=1; shift; fi
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [[ ! "$1" =~ ^[[:alnum:]][[:alnum:]#._-]*$ ]]; }; then
  usage >&2; exit 2
fi
TICKET_ID=${1:-}

ROOT=$(cd "$(dirname "$0")" && pwd)          # this folder (CLAUDE.md lives here)
. "$ROOT/scripts/env.sh"                     # TRACKER, REPOS_ROOT, ORG, HOST, QUEUE ... from projects.yaml
[ -n "$TICKET_ID" ] || need_task design      # the queue pass needs a design stage in projects.yaml
CYCLE=(); [ "$KIND" = linear ] && [ -z "$ALL_CYCLES" ] && CYCLE=(--cycle current)
CACHE=$(state_file design-tickets)      # { "<identifier>": {updatedAt, status: done|failed} }
LOGDIR=${LOGDIR:-$ROOT/logs/$PROJECT/design-tickets}   # gitignored; /tmp would lose the logs the cache points at
MODEL=${MODEL:-opus}
FALLBACK_MODEL=${FALLBACK_MODEL-fable}   # `-` not `:-`: an empty value disables the retry
# Sessions know only what this folder and the repo encode: no auto-memory written by earlier
# sessions (here or anywhere), no ~/.claude/CLAUDE.md, no user settings.json. Dropping the user
# source also drops the MCP servers registered in ~/.claude.json, so MCP servers (Linear, optional) come from
# .mcp.json here when it exists (its OAuth login is reused). --bare would go further but refuses OAuth altogether. A
# function, not more entries in claude_flags, so a CLAUDE_FLAGS override is covered too.
# Effort was the one thing worth keeping from the user settings.
mcp_flags=(); [ -f "$ROOT/.mcp.json" ] && mcp_flags=(--mcp-config "$ROOT/.mcp.json")
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
claude() { command claude --setting-sources project,local --settings '{"effortLevel":"high"}' ${mcp_flags[@]+"${mcp_flags[@]}"} "$@"; }
# Array, not a string: unquoted $CLAUDE_FLAGS would split rules containing spaces.
if [ -n "${CLAUDE_FLAGS:-}" ]; then eval "claude_flags=($CLAUDE_FLAGS)"; else
  claude_flags=(--permission-mode default
    --allowedTools "${MCP_TOOLS}Bash($TRACKER:*),Bash(gh:*),Bash(git:*),Read,Grep,Glob,Agent,WebSearch,WebFetch,Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(wc:*),Bash(find:*),Bash(grep:*),Bash(rg:*)"
    --disallowedTools "Edit,Write,MultiEdit,NotebookEdit,Bash(gh pr merge:*),Bash(gh issue delete:*),Bash(git push:*),Bash(git commit:*),Bash(git checkout:*),Bash(git pull:*),Bash(git fetch:*)")
fi
DRY_RUN=${DRY_RUN:-}

mkdir -p "$LOGDIR"
[ -s "$CACHE" ] || echo '{}' > "$CACHE"
# mkdir lock, not flock: macOS has no flock (util-linux), so `flock -n 9 ||` fired its
# failure branch on every Mac and the pass exited as "already running" without doing
# anything. mkdir is atomic everywhere; a run killed mid-pass leaves the directory
# behind, so a lock older than 6h is treated as abandoned and taken over.
LOCKDIR="$LOGDIR/.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
    echo "$(now) stale lock older than 6h, taking over"
  else
    echo "$(now) previous run still going, exiting"; exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

cache_get() { jq -r --arg k "$1" '.[$k] // {} | "\(.status // "") \(.updatedAt // "")"' "$CACHE"; }
cache_set() { # identifier status updatedAt
  [ -n "$DRY_RUN" ] && return
  jq --arg k "$1" --arg status "$2" --arg u "$3" '.[$k] = {status:$status, updatedAt:$u}' "$CACHE" > "$CACHE.tmp" && mv "$CACHE.tmp" "$CACHE"
}

# How the design section gets onto the ticket. Linear: the MCP's patch/append keeps the
# original description byte for byte; GitHub Issues: the CLI does the same PATCH.
append_howto() { # identifier
  if [ "$KIND" = linear ]; then
    echo "Update the issue with save_issue using the patch parameter, op \"append\", so the ORIGINAL DESCRIPTION IS PRESERVED VERBATIM (or run \`$TRACKER append $1 --body \"<the section>\"\`, which does the same)."
  else
    echo "Run \`$TRACKER append $1 --body \"<the section>\"\`: it appends to the issue body and keeps the original text verbatim. Never \`gh issue edit --body\`, which replaces it."
  fi
}

# What the session does to hand a designed ticket over. The labels it may add come from
# `tracker.design_labels` in projects.yaml (none configured: the repo labels alone).
handoff_labels() {
  local extra; extra=$("$TRACKER" config design-labels | awk -F': ' 'NF > 1 {printf "%s%s (%s)", sep, $1, $2; sep="; "}')
  if [ -n "$extra" ]; then echo "only labels from this set as they fit: the repo labels listed above, plus one or more of $extra"
  else echo "only labels that already exist on the repo and fit the ticket (never invent new ones)"; fi
}
handoff_step() { # identifier
  if [ -n "$QUEUE" ]; then
    echo "and one \`$TRACKER label $1 --remove \"$QUEUE\" --add ...\` call adding $(handoff_labels)"
  else
    echo "then \`$TRACKER move $1 todo\` to hand the ticket to the implementation pass, and, if labels help, one \`$TRACKER label $1 --add ...\` call with $(handoff_labels)"
  fi
}

# The target repos'"'"' CLAUDE.md and docs listing, for the prompt: this session starts in $ROOT, so
# a repo'"'"'s own CLAUDE.md would never load. $1 is the ticket'"'"'s "./repo ./repo" list; none: say so.
repo_docs() { # repos
  local r; r=${1//.\//}
  if [ -n "$r" ]; then "$ROOT/scripts/repo-docs.sh" $r
  else echo "(no repo matched: once you know which repos the ticket touches, run \`$ROOT/scripts/repo-docs.sh <repo>...\` and read it before designing)"; fi
}

design_prompt() { # identifier url repos ticket-dump
  local id=$1 url=$2 repos=$3 dump=$4
  cat <<PROMPT
You are designing the solution for ticket $id ($url). Goal: find the best design / architecture / solution for the problem stated in the ticket, then update the ticket so it is complete and ready for implementation. Do not write code, and change the ticket's stage only as step 5 says.

Context: the $NAME repos live in $REPOS_ROOT (one git repo per folder, listed in $ROOT/projects.yaml). Target repo(s) for this ticket: ${repos:-none matched - infer them from the ticket}. The project's own docs are in $CONTEXT (design, change chains between repos, local dev): read what applies before designing. A change can span several repos, so look across them when the ticket implies it.

The target repos' own CLAUDE.md and docs/ are the convention source of truth, read from origin's default branch (they are not loaded into this session on their own). Read the docs listed under them that apply, and put both the CLAUDE.md text and the list into the brief of every subagent you spawn:

$(repo_docs "$repos")

$("$TRACKER" howto)

Steps:
0. If $CONTEXT/policies.md exists, read it first. A ticket that falls under one of its close or scope rules is handled exactly as that file says (comment, labels, close), and then you stop: no design, no hand-over.
1. Read the ticket and ALL its comments (a dump follows at the end of this prompt). Follow parent/child/related tickets if referenced. If the implementation pass sent the ticket back to Design, its comment lists what was too vague: every point on that list must be answered by the implementation instructions in step 4.
2. Spawn one fresh general-purpose subagent with \`model: "fable"\` to research: trace the real flow end to end in the codebase (models, endpoints, components, existing patterns to reuse), and use web research only when the ticket depends on an external API/library/behavior. It must return a written verification: is the ticket's premise correct, is any proposed approach sound, what is missing, and its recommended design with repo-relative file paths.
3. Spawn a second, independent general-purpose subagent with \`model: "opus"\` to adversarially verify those findings against the code (re-check claims, don't rubber-stamp). Reconcile disagreements with one targeted check of your own. A subagent that fails because its model is overloaded, unavailable or out of usage reruns once on the other of the two.
4. $(append_howto "$id") Append "\n\n---\n\n## Design (ready for implementation)\n" followed by Markdown containing: the recommended solution (one decisive recommendation, not a survey; the smallest change that fully solves the problem), API / data model changes with file paths, frontend changes with file paths, edge cases, open questions only if truly blocking, and then a "### Implementation instructions" subsection. That subsection is the contract with the implementation pass, which turns a vague ticket away and hands a specific one to a cheaper model, so write it as short imperative lines an engineer follows top to bottom, one block per PR in merge order (repo and base branch in the block's first line), in this shape:

  Implement <the thing> according to the design above.
  Relevant files are <repo-relative paths; mark (new) ones>.
  Preserve <interface / API / behaviour that must not change>.
  Add tests <names or paths, and what each asserts; each must fail without the change>.
  Run <the exact lint / typecheck / test commands from the repo's CLAUDE.md>.
  If tests fail, diagnose and repair without changing <the preserved API>.
  Acceptance: <one check an engineer can run: request and expected response, UI state, query result>.

State the default chosen for every ambiguity and name the repo traps that apply (from its CLAUDE.md and docs/); no line may need further research to act on. Use real newlines, not escape sequences. If the description already carries a Design section from an earlier pass, append a "### Revision <date>" under it with what changed and a full instructions block, never a second Design heading.
5. Then the bookkeeping, through the tracker CLI: \`$TRACKER assign $id --me\`, $(handoff_step "$id"). If the tracker rejects a label it does not have, retry without it.
6. If a related ticket overlaps or contradicts this one, name it in the design section; never change other tickets.
7. Verify with \`$TRACKER get $id\` that the description still starts with the original text$([ -z "$QUEUE" ] || echo " and \"$QUEUE\" is gone").

Never mention AI, Claude, or Anthropic in anything written to the tracker. Write as an engineer would.
Finish with one line: the ticket identifier, the labels now on it, and a five-word summary of the recommendation.

--- The ticket as it stands now ---
$dump
PROMPT
}

# After a session: did the design land, and did the bookkeeping? The session decides the
# design; label and assignee are mechanical, so the script completes them when the session
# left them behind. Last line printed is the status: done | failed.
verify_and_finish() { # identifier original-description
  local id=$1 original=$2 after labels has_design intact assignee
  after=$("$TRACKER" get "$id" --json) || { echo "  tracker lookup failed after the session"; echo failed; return; }
  intact=$(jq -r --arg o "$original" '.description | startswith($o | rtrimstr("\n") | rtrimstr(" "))' <<<"$after")
  has_design=$(jq -r '.description | test("## Design")' <<<"$after")
  labels=$(jq -r '.labels | join(", ")' <<<"$after")
  assignee=$(jq -r '.assignee | if . == null then "" else (.email // .login // .name // "") end' <<<"$after")
  if [ "$has_design" != true ]; then echo "  no design section on the ticket (labels: $labels)"; echo failed; return; fi
  if [ "$intact" != true ]; then echo "  WARNING: the description no longer starts with the original text (labels: $labels)"; echo failed; return; fi
  if [ -z "$QUEUE" ]; then
    "$TRACKER" move "$id" todo >/dev/null || { echo "  move to Todo failed"; echo failed; return; }
  elif jq -e --arg q "$QUEUE" '.labels | index($q)' <<<"$after" >/dev/null; then
    echo "  session left \"$QUEUE\" on the ticket, removing it here"
    "$TRACKER" label "$id" --remove "$QUEUE" >/dev/null || { echo "  label removal failed"; echo failed; return; }
  fi
  if [ -z "$assignee" ]; then
    echo "  session left the ticket unassigned, assigning to me"
    "$TRACKER" assign "$id" --me >/dev/null || echo "  assign failed (left unassigned)"
  fi
  echo done
}

design_session() { # model log identifier url repos ticket-dump -> exit status of the session
  (cd "$ROOT" && design_prompt "$3" "$4" "$5" "$6" | "$ROOT/scripts/claude_run.py" "$2" -- "$(command -v claude)" \
    --setting-sources project,local --settings '{"effortLevel":"high"}' ${mcp_flags[@]+"${mcp_flags[@]}"} \
    --model "$1" --fallback-model opus,fable --add-dir "$REPOS_ROOT" "${claude_flags[@]}") 2>&1
}

# A session exited 0: check its work, cache the outcome, report. Returns 0 when done.
finish() { # identifier original-description updatedAt log
  local id=$1 last result
  last=$(tail -n1 "$4")
  result=$(verify_and_finish "$id" "$2" | tee -a "$4" | tail -n1)
  cache_set "$id" "$result" "$3"; echo "$id $result: $last"
  [ "$result" = failed ] && grep '^  ' "$4" | tail -n3
  [ "$result" = done ]
}

run_pass() {
  # origin fetch over HTTPS via gh credentials (no ssh agent under cron). The repo name comes
  # from the remote, not the folder, as in review-prs.sh: a clone may sit in a differently
  # named folder, and a URL built from the folder name 404s into the `|| true` below,
  # leaving origin stale.
  "$ROOT/scripts/repo-path.sh" --list | while read -r r d; do
    git -C "$d" -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
      fetch -q "$CLONE_BASE/$r.git" '+refs/heads/*:refs/remotes/origin/*' 2>/dev/null || true
  done

  if [ -n "$TICKET_ID" ]; then
    issues=$("$TRACKER" get "$TICKET_ID" --json | jq -c '[.]') || { echo "$(now) tracker lookup failed for $TICKET_ID"; return 1; }
    echo "$(now) designing requested ticket $TICKET_ID${DRY_RUN:+ (dry run)}"
  else
    issues=$("$TRACKER" list --state design ${QUEUE_ARGS[@]+"${QUEUE_ARGS[@]}"} ${CYCLE[@]+"${CYCLE[@]}"} --json) || { echo "$(now) tracker query failed"; return 1; }
    echo "$(now) $(jq 'length' <<<"$issues") Design tickets${QUEUE:+ with $QUEUE}${CYCLE[*]:+ in the current cycle}${DRY_RUN:+ (dry run)}"
  fi

  while read -r issue; do
    id=$(jq -r '.identifier' <<<"$issue"); url=$(jq -r .url <<<"$issue"); updated=$(jq -r .updatedAt <<<"$issue")
    original=$(jq -r '.description' <<<"$issue")
    repos=$(jq -r '[.repos[] | "./" + .] | join(" ")' <<<"$issue")

    read -r status cached <<<"$(cache_get "$id")"
    if [ -z "$TICKET_ID" ] && [ -n "$status" ] && [ "$cached" = "$updated" ]; then echo "$id skipped: session $status on this revision before, see $LOGDIR"; continue; fi

    echo "$id designing $url (repos: ${repos:-?})"
    [ -z "$DRY_RUN" ] || continue
    dump=$("$TRACKER" get "$id" --comments) || dump="(ticket dump unavailable: read it through the tracker yourself)"
    log="$LOGDIR/$id.$(date +%s).log"
    if design_session "$MODEL" "$log" "$id" "$url" "$repos" "$dump"; then
      finish "$id" "$original" "$updated" "$log" || [ -z "$TICKET_ID" ] || return 1
      continue
    fi
    # The session can refuse, be unavailable or hit its own rate limit: one retry on the
    # fallback model before giving up. Out of credits is account-wide, so the retry
    # fails the same way and the pass aborts below, uncached.
    if [ -n "$FALLBACK_MODEL" ] && [ "$FALLBACK_MODEL" != "$MODEL" ]; then
      echo "$id $MODEL failed ($(tail -n1 "$log" | cut -c1-120)), retrying on $FALLBACK_MODEL"
      log="$LOGDIR/$id.$(date +%s).fallback.log"
      if design_session "$FALLBACK_MODEL" "$log" "$id" "$url" "$repos" "$dump"; then
        finish "$id" "$original" "$updated" "$log" || [ -z "$TICKET_ID" ] || return 1
        continue
      fi
    fi
    if grep -qiE 'usage credits|usage limit|rate limit' "$log"; then
      # out of credits is transient: don't cache it, the whole pass is doomed anyway
      echo "$id pass aborted: out of credits / rate limited, see $log"; return 1
    else
      cache_set "$id" failed "$updated"; echo "$id claude FAILED, see $log"
      [ -z "$TICKET_ID" ] || return 1
    fi
  done < <(jq -c 'sort_by(.updatedAt)[]' <<<"$issues")
}

if [ -z "$TICKET_ID" ] && [ -n "${INTERVAL:-}" ]; then
  while true; do
    run_pass || true
    echo "$(now) pass done, next in ${INTERVAL}s"
    sleep "$INTERVAL"
  done
else
  run_pass
fi
