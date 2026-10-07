#!/usr/bin/env bash
# Review every open, non-draft PR in the tracked repos that still needs a review
# from me. GitHub is the only source of PRs: no running Claude session; the session reads a
# linked ticket through scripts/tracker.py.
# Spawns one `claude -p` per PR in its own detached worktree at the PR head, under
# ../.worktrees/review-prs/<repo>. The clone it branches from is resolved from its git remote
# by scripts/repo-path.sh (folder names need not match repo names).
#
# The worktree is the point: reviews used to run with cwd = the repo checkout itself, so a
# review and a session implementing in that same checkout fought over one working tree. One
# worktree per repo, re-pointed per PR rather than recreated, keeps the checkout free and
# keeps the cost to the files that actually differ between two PR heads.
#
# Skip rules, in order (all decided from `gh pr view` JSON, before any session is spawned):
#   - authored by me or by a bot (CI image bumps, dependabot)
#   - a verdict (APPROVED or CHANGES_REQUESTED, not COMMENTED/DISMISSED/PENDING) by any
#     human reviewer - me or anyone else - already sits on the current head commit. A review
#     is a review of a commit; if that commit is still the head, nothing has changed since
#     and there is nothing new to review. Commits pushed after the last verdict make the PR
#     eligible again; a re-requested review on an unchanged head does not.
#   - a review session already failed on this exact head (cache) - avoids
#     burning tokens every cycle on the same failure; delete the cache to retry.
#
# Cron (one pass per invocation):
#   3,18,33,48 * * * * /path/to/ai-sdlc/review-prs.sh >> /path/to/ai-sdlc/logs/review-prs.log 2>&1
# Or long-running (a pass every 15 min, e.g. under tmux/systemd):
#   INTERVAL=900 ./review-prs.sh
#
# Env:
#   CLAUDE_FLAGS    permission policy for the review sessions (cron can't answer prompts),
#                   as a shell word list (it is eval-split, so quote args containing spaces).
#                   Default below allows gh/git/read tools, the tracker CLI (ticket reads,
#                   follow-up tickets) and web lookups, and blocks merge/push.
#   MODEL           default opus (an alias: the latest Opus)
#   INTERVAL        seconds between passes; unset = single pass (cron mode)
#   REPOS           space-separated repo allowlist. Default: the repos in projects.yaml, the
#                   single list this, pull-all.sh and scripts/awaiting-review.sh all use.
#                   "all" = whole org.
#   UPDATED_WITHIN  only PRs updated in the last N days (default 30); 0 = no limit
#   DRY_RUN=1       list decisions, don't spawn sessions or touch the cache
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# GNU/BSD portability: `date -Is` and `date -d` are GNU-only, macOS has neither.
now()      { date +%Y-%m-%dT%H:%M:%S%z; }
days_ago() { date -v-"$1"d +%F 2>/dev/null || date -d "-$1 days" +%F; }

ROOT=$(cd "$(dirname "$0")" && pwd)          # this folder (CLAUDE.md lives here)
. "$ROOT/scripts/env.sh"                     # TRACKER, REPOS_ROOT, ORG, HOST, QUEUE ... from projects.yaml
WORKTREES=${WORKTREES:-$REPOS_ROOT/.worktrees/review-prs}   # one per repo, reused across PRs
CACHE=$(state_file review-prs)           # { "<pr url>": {head, status: reviewed|failed} }
LOGDIR=${LOGDIR:-$ROOT/logs/$PROJECT/review-prs}   # gitignored; /tmp would lose the logs the cache points at
MODEL=${MODEL:-opus}
# Sessions know only what this folder and the repo encode: no auto-memory written by earlier
# sessions (here or anywhere), no ~/.claude/CLAUDE.md, no user settings.json. Dropping the user
# source also drops the MCP servers registered in ~/.claude.json, so MCP servers (Linear, optional) come from
# .mcp.json here when it exists (its OAuth login is reused). --bare would go further but refuses OAuth altogether. A
# function, not more entries in claude_flags, so a CLAUDE_FLAGS override is covered too. Effort
# was the one thing worth keeping from the user settings. The cwd is a worktree, so this
# folder's CLAUDE.md only loads through --add-dir plus the env that turns that on.
mcp_flags=(); [ -f "$ROOT/.mcp.json" ] && mcp_flags=(--mcp-config "$ROOT/.mcp.json")
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1
claude() { command claude --setting-sources project,local --settings '{"effortLevel":"high"}' ${mcp_flags[@]+"${mcp_flags[@]}"} --add-dir "$ROOT" "$@"; }
# Array, not a string: unquoted $CLAUDE_FLAGS would split "Bash(gh pr merge:*)" on its spaces
# and claude would silently ignore the resulting garbage deny rules.
if [ -n "${CLAUDE_FLAGS:-}" ]; then eval "claude_flags=($CLAUDE_FLAGS)"; else
  claude_flags=(--permission-mode acceptEdits
    --allowedTools "Bash(gh:*),Bash(git:*),Bash($TRACKER:*),Read,Grep,Glob,Agent,WebSearch,WebFetch"
    --disallowedTools "Bash(gh pr merge:*),Bash(git push:*)")
fi
DRY_RUN=${DRY_RUN:-}
# One list, projects.yaml, shared with pull-all.sh and scripts/awaiting-review.sh, so adding
# a repo is a one-line edit in one place.
REPOS=${REPOS:-$("$TRACKER" config repos | tr '\n' ' ')}
UPDATED_WITHIN=${UPDATED_WITHIN:-30}

mkdir -p "$LOGDIR"
[ -s "$CACHE" ] || echo '{}' > "$CACHE"
# macOS has no flock (util-linux), so the original `flock -n 9 ||` branch fired on every Mac
# and the script exited as "already running" before it looked at a single PR.
#
# mkdir is atomic everywhere. A run killed mid-pass leaves the directory behind, so a lock
# older than 6h is treated as abandoned and taken over.
LOCKDIR="$LOGDIR/.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
    echo "$(now) stale lock older than 6h, taking over"
  else
    echo "$(now) previous run still going, exiting"; exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

ME=$(gh api user --jq .login)

cache_get() { jq -r --arg pr "$1" '.[$pr] // {} | "\(.status // "") \(.head // "")"' "$CACHE"; }
cache_set() { # pr status head
  [ -n "$DRY_RUN" ] && return
  jq --arg pr "$1" --arg status "$2" --arg head "$3" '.[$pr] = {status:$status, head:$head}' "$CACHE" > "$CACHE.tmp" && mv "$CACHE.tmp" "$CACHE"
}

# How a follow-up ticket is filed depends on the tracker: Linear takes the current cycle and the
# affected repo's label, GitHub Issues take the repo.
if [ "$KIND" = linear ]; then
  FOLLOWUP='--cycle current --label "<affected repo label: '"$TRACKER"' config label-for-repo <repo>>"'; CYCLE_NOTE=' without `--cycle`'
else
  FOLLOWUP='--repo <affected repo>'; CYCLE_NOTE=''
fi
BLOCKER_NOTE=; [ -z "$BLOCKER" ] || BLOCKER_NOTE=" Add \`--label $BLOCKER\` when the follow-up must land before the next release: data loss, a security hole, or a break that ships with this PR."

review_prompt() { # pr head
  local pr=$1 head=$2
  local repo n; repo=$(sed -E 's#.*/([^/]+)/pull/.*#\1#' <<<"$pr"); n=$(sed -E 's#.*/pull/([0-9]+)$#\1#' <<<"$pr")
  cat <<EOF
Review GitHub pull request $pr (head commit $head) and submit exactly one review. Never merge, never push.

How to work:
- If $CONTEXT/policies.md exists, read it before the diff. A PR that falls under one of its close or scope rules is handled exactly as that file says (comment and close, no review), and you finish with one line saying so.
- \`gh pr view $pr --json title,body,files\` for the description, and \`gh pr diff $pr\` for the diff. The PR body is the spec. If the title, branch or body names a ticket (IDs look like $EXAMPLE), read it with \`$TRACKER get <ID> --comments\` and check the diff does what the ticket asks, not only what the PR body claims.
- Commits landed after the last review of this PR; that is why you are here. If I (author $ME) reviewed an earlier commit (\`gh api repos/$ORG/$repo/pulls/$n/reviews\` and \`.../pulls/$n/comments\`), read my earlier comments, check whether each was addressed, re-check that each was right in the first place, and review the new commits in full. Withdraw a point you got wrong in one line.
- You are in a detached worktree of $repo checked out at this PR's head ($head), so the changed files and the code around them are on disk at the reviewed commit: read them directly. \`git diff <base>...$head\` and \`git show\` still work for comparing against the base. Do not run git fetch/pull/push and do not switch branches - this worktree is yours, but it is re-pointed for the next PR.
- Read this repo's own \`CLAUDE.md\` and \`docs/\` before judging anything repo-specific: they are the convention source of truth and they change. A convention asserted from memory is how a review ends up citing a rule the repo dropped months ago.
- When the PR touches a contract another repo consumes (an API, a schema, a message, a shared config), check the change chains in $CONTEXT (start with its cross-repo doc if it has one) — a column added upstream with no reader downstream lands silently. Those docs don't name every repo in $ROOT/projects.yaml: think about who actually reads or provisions for this change. For each consumer repo, run \`gh search prs --owner $ORG --repo <consumer-repo> <ticket-id-or-keyword>\` to see whether the companion change already landed, is open, or is missing entirely. A missing companion with no PR anywhere is a finding: name the repo and what it needs.
- When the PR relies on a factual claim - a library's behaviour, a vendor limit, an API contract, a benchmark - verify it (WebSearch/WebFetch, the library source, the repo's docs) before accepting or disputing it. A finding built on a wrong fact is worse than none.
- Read what other reviewers already left (\`gh api repos/$ORG/$repo/pulls/$n/reviews\` and \`.../pulls/$n/comments\`, plus \`gh pr view $pr --json comments\`) before writing anything. Never repeat a point already on the record; comment only on what they missed.
- Run \`gh pr checks $pr\` for CI status.
- Submit from this session:
  \`gh api repos/$ORG/$repo/pulls/$n/reviews --input review.json\`
  with review.json = {"event": "APPROVE|REQUEST_CHANGES", "body": "...", "comments": [{"path","line","side":"RIGHT","body"}], "commit_id": "$head"}.
  Re-check \`gh pr view $pr --json headRefOid\` right before submitting; if it no longer equals $head, stop without submitting and say so.
  An inline comment's line must be a new-file line that appears in the diff; otherwise put it in the body.
- Finish with one line: PR, decision, follow-up issue ids if any, and CI status if not passing.

Review rules:
- Verdict is binary. APPROVE, or REQUEST_CHANGES with every point the author must address. COMMENT only when a genuinely unclear point blocks the decision, phrased as the one question you need answered. This PR comes back only when new commits land.
- REQUEST_CHANGES only for what must be fixed before merge: a bug or logic flaw, data loss or a security hole, a break in compatibility with another repo or an existing contract, a mismatch with the ticket or the PR description, a claim the code rests on that research shows is wrong, a missed change that materially affects correctness, cost or performance, or a small gap that belongs to this change (a hardening the new code itself needs, the docs page or test this change should have touched). Each finding: one inline comment on the exact line, or the body when it cannot be anchored.
- APPROVE when nothing above applies. No issues means an empty body and no comments: no summary, no praise, no list of what you checked, no "looks good".
- Cosmetics never count: style, naming, formatting, wording, structure, "I'd have done it differently". Drop them entirely, on approvals too.
- Default to this PR. A real finding that is related to the change and small enough to fold in (same repo, hours of work, not days) is asked for here as a REQUEST_CHANGES point, never filed as a ticket. A ticket is only for work that is big (needs its own design, or more than this PR can carry) or unrelated to the change: a leg in another repo, a bug this PR exposes but did not introduce, a pre-existing gap next to the diff. Those get a ticket whatever the verdict. First \`$TRACKER list --open --since 90\` and look for the same problem, so an existing ticket is reused, not duplicated. Then \`$TRACKER create $FOLLOWUP --state todo --title "<what to change>" --body-file <file>\`. Use \`--state triage\`${CYCLE_NOTE} instead when unsure the problem is real, how big it is, or what the fix is.$BLOCKER_NOTE Body: what was found with file paths and the PR url, why it matters, the concrete fix, and the PR's ticket id when there is one. Reference the ticket id in one line of the review body. No ticket for cosmetics.
- Review the code, not the description. Read every changed file in full and the callers around it. Verify before claiming: a wrong "bug" costs more than a missed one.
- Another reviewer's verdict is not a substitute for mine. If you independently reach the same blocking conclusion and the fix has not landed, request changes in one line pointing at their finding.
- CI is informational, never blocking either way.
- Every review text is strict, direct and brief: state the problem and, if obvious, the fix, in one to three sentences. No prose, no preamble, no hedging, no restating the diff. Plain text, no HTML entities.
- Never mention AI, Claude, or Anthropic in any review text or ticket.
EOF
}

# A detached worktree per repo, re-pointed at each PR head. Reused rather than recreated:
# `app` is a big checkout and there are dozens of PRs a pass, so paying a full checkout per
# review would dominate the run. Detached for the same reason update-prs.sh is - a worktree
# holding a branch blocks anyone else checking it out, including the implement sessions this
# is meant to stay out of the way of.
review_worktree() { # clone repo pr-number head -> prints the worktree path
  # `$2` again, not `$repo`: bash 3.2 (what macOS ships) evaluates every right-hand side in
  # one `local` before assigning any of them, so `wt="$WORKTREES/$repo"` here dies on
  # `repo: unbound variable` under `set -u`.
  local clone=$1 repo=$2 n=$3 head=$4 wt="$WORKTREES/$2"
  # A PR from a fork is not on any origin/* branch, so the head may not be here yet.
  git -C "$clone" cat-file -e "$head^{commit}" 2>/dev/null || \
    git -C "$clone" -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
      fetch -q "$CLONE_BASE/$repo.git" "+refs/pull/$n/head:refs/remotes/origin/pr/$n" 2>/dev/null || true
  git -C "$clone" cat-file -e "$head^{commit}" 2>/dev/null || return 1

  git -C "$clone" worktree prune                      # a manually deleted dir still holds its registration
  if [ -d "$wt" ] && git -C "$wt" rev-parse --git-dir >/dev/null 2>&1; then
    # Reset first: a review should never inherit whatever the last one left behind.
    git -C "$wt" checkout -q --detach "$head" && git -C "$wt" reset -q --hard "$head" && \
      git -C "$wt" clean -qfd && { printf '%s' "$wt"; return 0; }
    git -C "$clone" worktree remove --force "$wt" 2>/dev/null
  fi
  mkdir -p "$WORKTREES"
  # Clear the path before adding, registered or not: `worktree add` refuses a path that
  # exists and is not empty, and the reuse branch above only catches a *working* worktree.
  # A clone that was re-cloned or moved leaves a directory whose `.git` points at a gitdir
  # that is gone - `rev-parse` fails, `worktree prune` drops the registration and leaves the
  # files - and without this the repo would then be skipped on every pass.
  rm -rf "$wt"; git -C "$clone" worktree prune
  git -C "$clone" worktree add -q --detach "$wt" "$head" || return 1
  printf '%s' "$wt"
}

# -> one decision per open non-draft PR in the org
run_pass() {
  # origin fetch over HTTPS via gh credentials (no ssh agent under cron). The repo name comes
  # from the remote, not the folder: a clone may sit in a differently named folder, and a
  # URL built from the folder name 404s into the `|| true` below, leaving origin stale.
  "$ROOT/scripts/repo-path.sh" --list | while read -r r d; do
    git -C "$d" -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
      fetch -q "$CLONE_BASE/$r.git" '+refs/heads/*:refs/remotes/origin/*' 2>/dev/null || true
  done

  # --limit 200: more open PRs than that across the swept repos need paging
  filter=(--owner "$ORG" --state open --draft=false --limit 200)
  [ "$UPDATED_WITHIN" = 0 ] || filter+=(--updated ">$(days_ago "$UPDATED_WITHIN")")
  if [ "$REPOS" != all ]; then for r in $REPOS; do filter+=(--repo "$ORG/$r"); done; fi
  # not mine, not a bot (manifests-ci image bumps, dependabot)
  prs=$(gh search prs "${filter[@]}" --json url,author --jq '.[] | select(.author.login != "'"$ME"'" and (.author.is_bot | not) and (.author.login | test("\\[bot\\]$") | not)) | .url' | sort)
  echo "$(now) $(grep -c . <<<"$prs") open PRs to consider${DRY_RUN:+ (dry run)}"

  for pr in $prs; do
    info=$(gh pr view "$pr" --json isDraft,state,headRefOid,reviews,commits 2>/dev/null) || { echo "$pr gh pr view failed"; continue; }
    head=$(jq -r .headRefOid <<<"$info")
    [ "$(jq -r .state <<<"$info")" = OPEN ] && [ "$(jq -r .isDraft <<<"$info")" = false ] || { echo "$pr skipped: not open or draft"; continue; }

    # Deterministic gate, no session involved: skip when any human's verdict already sits on
    # the current head. Each review carries the commit it was submitted against, so comparing
    # that to the head is exact - unlike committedDate vs submittedAt, which misses a commit
    # authored before the review and pushed after it, and a rebase that kept committer dates.
    # The date comparison stays only as the fallback for a review with no commit recorded.
    # A verdict on an older commit means code landed since: review it, as a re-review when the
    # earlier verdict was mine. Teams have .slug not .login and never match; PENDING is an
    # unsubmitted draft, COMMENTED is not a verdict, DISMISSED was withdrawn.
    verdict=$(jq -r --arg me "$ME" '
      .headRefOid as $head
      | (.commits | map(.committedDate) | max) as $last
      | [.reviews[]
         | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED")
         | select(.author.login | test("\\[bot\\]$") | not)] as $verdicts
      | ([$verdicts[] | select(.commit.oid == $head or (.commit.oid == null and .submittedAt > $last))] | sort_by(.submittedAt) | last) as $on_head
      | ([$verdicts[] | select(.author.login == $me)] | length > 0) as $mine
      | if $on_head != null then "done \($on_head.author.login) \($on_head.state)"
        elif $mine then "rereview"
        else "review" end' <<<"$info")
    case "$verdict" in
      done*) echo "$pr skipped: reviewed at this head (${verdict#done })"; continue;;
    esac

    # The cache is what stops a broken PR burning tokens every cycle. Deleting its entry is
    # the only way to retry the same head.
    read -r status cached_head <<<"$(cache_get "$pr")"
    if [ "$status" = failed ] && [ "$cached_head" = "$head" ]; then
      echo "$pr skipped: session failed on this head before, see $LOGDIR - delete its entry in $CACHE to retry"; continue
    fi

    repo=$(sed -E 's#.*/([^/]+)/pull/.*#\1#' <<<"$pr"); n=$(sed -E 's#.*/pull/([0-9]+)$#\1#' <<<"$pr")
    # Resolved by remote URL, not folder name, so a clone named differently still matches and a
    # same-named clone of another forge doesn't stand in for it. No clone -> review through gh.
    clone=$("$ROOT/scripts/repo-path.sh" "$repo" 2>/dev/null) || clone=""
    if [ -z "$clone" ]; then
      cwd="$ROOT"; no_clone=" (no local clone, reading via gh)"
    else
      no_clone=""
      # A dry run only reports the decision, so it must not build worktrees or fetch fork
      # refs into the clones: name the path the real run would use and touch nothing.
      if [ -n "$DRY_RUN" ]; then
        cwd="$WORKTREES/$repo"
      else
        cwd=$(review_worktree "$clone" "$repo" "$n" "$head") || {
          echo "$pr worktree failed, skipped"; continue
        }
      fi
    fi
    case "$verdict" in rereview) what="re-reviewing";; *) what="reviewing";; esac
    echo "$pr $what @ $head (cwd $cwd)$no_clone"
    [ -z "$DRY_RUN" ] || continue
    log="$LOGDIR/$repo-$n.$(date +%s).log"
    if (cd "$cwd" && review_prompt "$pr" "$head" | claude -p --model "$MODEL" "${claude_flags[@]}") > "$log" 2>&1; then
      cache_set "$pr" reviewed "$head"; echo "$pr done: $(tail -n1 "$log")"
    elif grep -qiE 'usage credits|usage limit|rate limit' "$log"; then
      # out of credits is transient: don't cache it as failed, the whole pass is doomed anyway
      echo "$pr pass aborted: out of credits / rate limited, see $log"; return
    else
      cache_set "$pr" failed "$head"; echo "$pr claude FAILED, see $log"
    fi
  done
}

if [ -n "${INTERVAL:-}" ]; then
  while true; do
    run_pass
    echo "$(now) pass done, next in ${INTERVAL}s"
    sleep "$INTERVAL"
  done
else
  run_pass
fi
