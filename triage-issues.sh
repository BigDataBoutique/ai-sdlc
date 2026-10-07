#!/usr/bin/env bash
# Triage every issue in the triage stage: one `claude -p` session per issue reads the issue and
# all its comments, applies the context's policies.md (close when it falls under a close rule),
# validates the report against the code (and a failing test in a worktree when that settles it),
# comments, labels it with one outcome group and moves it to the next stage. The project's
# triage.md says what each outcome means and who the session writes as.
#
# Needs, per project (else the pass skips it): a triage stage in projects.yaml, `tracker.triage`
# with `outcomes` ([[labels]...]), optional `labels` (others it may add) and `next` (default todo),
# and `<context>/triage.md`. `./scripts/tracker.py config triage` prints what was resolved.
#
# Issues the tracker user opened are never triaged here (`list --not-mine`, and the single-issue
# form refuses them): a maintainer commenting on their own report is a conversation with
# themselves. They wait for a human. The session also never reviews a PR the same user authored.
#
# A session that dies or leaves the issue without an outcome is cached as failed for that issue
# revision (identifier + updatedAt); delete the cache to retry. Cron or sdlc.py (task `triage`).
#
# Env: MODEL (default opus), FALLBACK_MODEL (default fable, empty disables), CLAUDE_FLAGS,
#      BATCH (issues per pass, default 10), DRY_RUN=1, INTERVAL; or pass one issue id.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
now() { date +%Y-%m-%dT%H:%M:%S%z; }
[ "${1:-}" != --help ] && [ "${1:-}" != -h ] || { echo "Usage: $0 [repo#N]"; exit 0; }
TICKET_ID=${1:-}
ROOT=$(cd "$(dirname "$0")" && pwd)
. "$ROOT/scripts/env.sh"
need_task triage                             # triage stage, tracker.triage and triage.md, or skip
LOGDIR=${LOGDIR:-$ROOT/logs/$PROJECT/triage-issues}
CACHE=$(state_file triage-issues)
MODEL=${MODEL:-opus}
FALLBACK_MODEL=${FALLBACK_MODEL-fable}
BATCH=${BATCH:-10}
DRY_RUN=${DRY_RUN:-}
TRIAGE_CFG=$("$TRACKER" --json config triage)
OUTCOMES=$(jq -c .outcomes <<<"$TRIAGE_CFG")       # [[labels]...]: a triaged issue has all labels of one
NEXT=$(jq -r .next <<<"$TRIAGE_CFG")
outcomes_text=$(jq -r 'map(map("`\(.)`") | join(" + ")) | join(" | ")' <<<"$OUTCOMES")
labels_text=$(jq -r '.labels | map("`\(.)`") | join(", ") | if . == "" then "none" else . end' <<<"$TRIAGE_CFG")
ME=$(gh api user --jq .login)                  # PRs live on the forge whatever the tracker is
ME_ID=$("$TRACKER" me --json | jq -r .id)      # the tracker user, to refuse issues they opened

mcp_flags=(); [ -f "$ROOT/.mcp.json" ] && mcp_flags=(--mcp-config "$ROOT/.mcp.json")
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
if [ -n "${CLAUDE_FLAGS:-}" ]; then eval "claude_flags=($CLAUDE_FLAGS)"; else
  # Edits are for the throwaway test in a worktree (the prompt says so); no pushes, PRs or merges.
  claude_flags=(--permission-mode acceptEdits
    --allowedTools "Bash,Read,Edit,Write,Grep,Glob,Agent,WebSearch,WebFetch"
    --disallowedTools "Bash(gh pr:*),Bash(gh issue delete:*),Bash(git push:*),Bash(gh api -X DELETE:*)")
fi

mkdir -p "$LOGDIR"
[ -s "$CACHE" ] || echo '{}' > "$CACHE"
LOCKDIR="$LOGDIR/.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then echo "$(now) stale lock older than 6h, taking over"
  else echo "$(now) previous run still going, exiting"; exit 0; fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

cache_get() { jq -r --arg k "$1" '.[$k] // {} | "\(.status // "") \(.updatedAt // "")"' "$CACHE"; }
cache_set() { [ -n "$DRY_RUN" ] && return; jq --arg k "$1" --arg s "$2" --arg u "$3" '.[$k] = {status:$s, updatedAt:$u}' "$CACHE" > "$CACHE.tmp" && mv "$CACHE.tmp" "$CACHE"; }

triage_prompt() { # identifier url repo dump
  local id=$1 url=$2 repo=$3 dump=$4
  cat <<PROMPT
You are triaging issue $id ($url) of $NAME. Work unattended; never ask the user anything.

$("$TRACKER" howto)

Repo clones live in $REPOS_ROOT; resolve one with \`$ROOT/scripts/repo-path.sh <repo>\` (this issue: ${repo:-no repo named, pick from $("$TRACKER" config repos | paste -sd, -)}). The project docs are in $CONTEXT. Local checkouts may be dirty or behind: read origin's default branch (\`git -C <clone> fetch -q origin\`, then \`git show\` / \`git grep\` on origin/<default>), and run anything that executes code in a worktree: \`git -C <clone> worktree add $REPOS_ROOT/.worktrees/$id-triage/<repo> -b triage-${id//[#]/-} origin/<default>\`. Never edit a clone, never push, never open a PR. Remove the worktree when done.

Steps:
1. Read $CONTEXT/README.md and $CONTEXT/triage.md (this project's triage policy: who you act as, what each outcome means, how to write), and any file it tells you to read. Where triage.md and these steps disagree, triage.md wins. The issue and all its comments are at the end of this prompt; follow any linked issue or PR.
2. Close policy. If $CONTEXT/policies.md exists and the issue falls under one of its close rules, do exactly what that file says (label, comment, close) and stop. When unsure, do not close.
3. Open PR. Search for open PRs that reference or fix this issue (\`gh pr list -R $ORG/<repo> --state open --search "<number>"\`, plus linked PRs on the issue). A PR authored by $ME is yours: never review it, approve it or comment on it; if the issue does not link it, add one line naming it, pick the outcome triage.md gives an issue with an open PR, and go on to step 8. For anyone else's PR, review the issue and the PR together (read the diff, check it against the code and the policies) and make one decision for both: the PR is right (say so in the PR review and on the issue), needs changes (say exactly which), or the issue needs an answer first. Never pick an outcome that queues the issue for implementation while a PR covers it. When the decision waits on the opener (issue author, or the PR author if different), ask in a comment and assign them with \`$TRACKER assign $id --user <login>\`.
4. Research freely: the codebase, linked issues and PRs, and the web (WebSearch/WebFetch) for upstream docs, release notes and known issues. Decide what it is: a bug report, a feature request, or a question.
5. Feature request: reason about it against the code and the policies (does it fit the scope, where would it live, what would it touch, risks, a cheaper alternative). Post ONE comment with that reasoning, stating whether it is valid, its priority and whether it will be built.
6. Bug report: validate the complaint.
   a. Trace the reported flow in the code on origin's default branch. Check it was not already fixed there or by an open PR, and that the reported version/config can actually hit the code path.
   b. If a test can settle it (in the worktree, per the repo's CLAUDE.md), write one that fails because of the reported behaviour and run it. A test you could not run is not evidence; say so.
   c. Post ONE short comment: confirmed (root cause with file paths, the failing test if any, a suggested fix direction), not a bug (why, with pointers), or the exact details still missing from the reporter.
7. A support question: answer it in a comment.
8. Outcome. Unless you closed it, leave the issue carrying every label of exactly one of these groups, picked per triage.md: $outcomes_text. Other labels you may add as they fit: $labels_text. Add no other labels; set them in one \`$TRACKER label $id --add ...\` call.
9. Hand off: \`$TRACKER move $id $NEXT\`, unless triage.md names another stage for this outcome. Then verify with \`$TRACKER get $id\` that the issue left the triage stage and the labels and comment are in place.

Comments: short, specific, human, in the voice triage.md asks for, in the issue author's language; no AI or Claude mention, no signature. Follow $ROOT/docs/code-style.md for comments written to others.
Finish with one line: the identifier, the labels now on it, and a five-word verdict.

--- The issue as it stands now ---
$dump
PROMPT
}

session() { # model log id url repo dump
  (cd "$ROOT" && triage_prompt "$3" "$4" "$5" "$6" | "$ROOT/scripts/claude_run.py" "$2" -- "$(command -v claude)" \
    --setting-sources project,local --settings '{"effortLevel":"high"}' ${mcp_flags[@]+"${mcp_flags[@]}"} \
    --model "$1" --fallback-model opus,fable --add-dir "$REPOS_ROOT" "${claude_flags[@]}") 2>&1
}

# Printed last: done | failed. The session decides; this checks the outcome is complete.
verify() { # id comments-before
  local id=$1 before=$2 after
  after=$("$TRACKER" get "$id" --comments --json) || { echo "  tracker lookup failed"; echo failed; return; }
  if [ "$(jq -r .stateType <<<"$after")" = closed ]; then echo done; return; fi
  local has_outcome new_comments
  has_outcome=$(jq -r --argjson o "$OUTCOMES" '(.labels | map(ascii_downcase)) as $l
    | any($o[]; all(.[]; ascii_downcase as $x | $l | index($x) != null))' <<<"$after")
  new_comments=$(( $(jq '.comments | length' <<<"$after") - before ))
  if [ "$has_outcome" != true ]; then echo "  no outcome labels ($outcomes_text)"; echo failed; return; fi
  if [ "$new_comments" -lt 1 ]; then echo "  no comment posted"; echo failed; return; fi
  if [ "$(jq -r .stage <<<"$after")" = triage ]; then
    echo "  session left the issue in triage, moving it to $NEXT here"
    "$TRACKER" move "$id" "$NEXT" >/dev/null || { echo "  move failed"; echo failed; return; }
  fi
  echo done
}

run_pass() {
  if [ -n "$TICKET_ID" ]; then
    issues=$("$TRACKER" get "$TICKET_ID" --json | jq -c '[.]') || { echo "$(now) tracker lookup failed for $TICKET_ID"; return 1; }
  else
    issues=$("$TRACKER" list --state triage --not-mine --json | jq -c --argjson n "$BATCH" '.[:$n]') || { echo "$(now) tracker query failed"; return 1; }
  fi
  echo "$(now) $(jq length <<<"$issues") issues to triage${DRY_RUN:+ (dry run)}"
  while read -r issue; do
    id=$(jq -r .identifier <<<"$issue"); url=$(jq -r .url <<<"$issue"); updated=$(jq -r .updatedAt <<<"$issue"); repo=$(jq -r '.repo // .repos[0] // empty' <<<"$issue")
    if [ "$(jq -r '.creator.id // empty' <<<"$issue")" = "$ME_ID" ]; then echo "$id skipped: you opened it, a human triages it"; continue; fi
    read -r status cached <<<"$(cache_get "$id")"
    if [ -z "$TICKET_ID" ] && [ -n "$status" ] && [ "$cached" = "$updated" ]; then echo "$id skipped: session $status on this revision before"; continue; fi
    echo "$id triaging $url"
    [ -z "$DRY_RUN" ] || continue
    dump=$("$TRACKER" get "$id" --comments) || dump="(dump unavailable: read the issue with the tracker yourself)"
    before=$("$TRACKER" get "$id" --comments --json | jq '.comments | length')
    log="$LOGDIR/$id.$(date +%s).log"
    ok=
    session "$MODEL" "$log" "$id" "$url" "$repo" "$dump" && ok=1
    if [ -z "$ok" ] && [ -n "$FALLBACK_MODEL" ] && [ "$FALLBACK_MODEL" != "$MODEL" ]; then
      echo "$id $MODEL failed ($(tail -n1 "$log" | cut -c1-120)), retrying on $FALLBACK_MODEL"
      log="$LOGDIR/$id.$(date +%s).fallback.log"
      session "$FALLBACK_MODEL" "$log" "$id" "$url" "$repo" "$dump" && ok=1
    fi
    if [ -z "$ok" ]; then
      if grep -qiE 'usage credits|usage limit|rate limit' "$log"; then echo "$id pass aborted: out of credits / rate limited, see $log"; return 1; fi
      cache_set "$id" failed "$updated"; echo "$id claude FAILED, see $log"; continue
    fi
    result=$(verify "$id" "$before" | tee -a "$log" | tail -n1)
    cache_set "$id" "$result" "$updated"; echo "$id $result $url: $(tail -n1 "$log")"
  done < <(jq -c 'sort_by(.updatedAt)[]' <<<"$issues")
}

if [ -z "$TICKET_ID" ] && [ -n "${INTERVAL:-}" ]; then
  while true; do run_pass || true; echo "$(now) pass done, next in ${INTERVAL}s"; sleep "$INTERVAL"; done
else
  run_pass
fi
