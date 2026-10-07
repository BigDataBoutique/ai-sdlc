#!/usr/bin/env bash
# Keep my PRs moving: every open PR in the tracked repos that I authored and that has a merge
# conflict with its base branch or a reviewer requesting changes gets one fix session. PRs by
# anyone else, and PRs with neither problem, are left alone. The repo's checkout is resolved
# from its git remote by scripts/repo-path.sh (folder names need not match); the PR's head is
# checked out in a fresh worktree under ../.worktrees/update-prs/, then a `claude -p` session
# fixes or answers every review point first, merges origin/<base> and resolves the conflicts,
# runs the tests and commits. It never pushes: the script squashes the result into one commit
# on origin/<base> (message = PR title and body), force-pushes it with a lease on the head it
# saw, and re-requests review. GitHub is the source of PRs; the linked ticket is read through
# scripts/tracker.py.
#
# Skip rules, in order:
#   - not authored by me, in a repo that is not tracked in projects.yaml, opened more than CREATED_WITHIN days ago (default 30), draft, or the repo has no local clone to worktree from
#   - neither conflicting nor changes requested. "Changes requested" = a reviewer's latest
#     review is REQUEST_CHANGES and no commit landed after it (a pushed fix awaits re-review)
#   - a session already ran on this exact head (cache: fixed = it pushed, so the head moved;
#     replied = it only answered threads; failed = don't burn tokens on the same failure). Delete the cache to retry.
#
# With a PR URL argument (./update-prs.sh <PR url>) only that PR is
# handled, whoever authored it: skipped if missing, closed, draft, approved, or with no conflict,
# requested changes or unresolved threads. Otherwise it gets the same worktree, session, squash
# and force-push, with the session told to verify each reviewer claim before acting on it.
#
# Cron (one pass per invocation):
#   5,35 * * * * /path/to/ai-sdlc/update-prs.sh >> /path/to/ai-sdlc/logs/update-prs.log 2>&1
# Or long-running: INTERVAL=1800 ./update-prs.sh    (sdlc.py does this for you)
#
# Env:
#   CLAUDE_FLAGS    permission policy (shell word list, eval-split). Default below allows
#                   edits, Bash (the tracker CLI included), the Linear MCP (Linear projects) and web research; blocks merges and every push (the script pushes).
#   REPOS           space-separated repo allowlist (default: the repos in projects.yaml)
#   MODEL           default opus (an alias: the latest Opus). A review point that needs deeper
#                   reasoning goes to a Fable subagent; an overloaded or unavailable model
#                   falls back along opus,fable.
#   INTERVAL        seconds between passes; unset = single pass (cron mode)
#   CREATED_WITHIN  only PRs opened in the last N days (default 30): older ones are left alone
#   DRY_RUN=1       list decisions, don't touch worktrees, sessions or the cache
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

now() { date +%Y-%m-%dT%H:%M:%S%z; }   # `date -Is` is GNU-only

ROOT=$(cd "$(dirname "$0")" && pwd)          # this folder
. "$ROOT/scripts/env.sh"                     # TRACKER, REPOS_ROOT, ORG, HOST, CLONE_BASE ... from projects.yaml
CACHE=$(state_file update-prs)           # { "<pr url>": {head, status: fixed|replied|failed} }
LOGDIR=${LOGDIR:-$ROOT/logs/$PROJECT/update-prs}
WORKTREES="$REPOS_ROOT/.worktrees/update-prs"
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
# Array, not a string: unquoted $CLAUDE_FLAGS would split rules containing spaces.
if [ -n "${CLAUDE_FLAGS:-}" ]; then eval "claude_flags=($CLAUDE_FLAGS)"; else
  # Deny rules are command-prefix matches (`git -C x push` slips past); the prompt is the real guard
  claude_flags=(--permission-mode acceptEdits
    --allowedTools "Bash,Read,Edit,Write,MultiEdit,NotebookEdit,Grep,Glob,Agent,${MCP_TOOLS}WebSearch,WebFetch"
    --disallowedTools "Bash(gh pr merge:*),Bash(git push:*)")
fi
DRY_RUN=${DRY_RUN:-}
CREATED_WITHIN=${CREATED_WITHIN:-30}
# The tracked repos only, like review-prs.sh: an org can hold many repos this project does not drive.
REPOS=${REPOS:-$("$TRACKER" config repos | tr '\n' ' ')}
since=$(date -u -d "-$CREATED_WITHIN days" +%F 2>/dev/null || date -u -v-"$CREATED_WITHIN"d +%F)   # GNU, then BSD

mkdir -p "$LOGDIR"
[ -s "$CACHE" ] || echo '{}' > "$CACHE"
# mkdir lock, not flock: macOS has no flock (see review-prs.sh). Stale after 6h.
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

# Follow-up tickets are filed the way the tracker wants, as in review-prs.sh: Linear takes the
# current cycle and the affected repo's label, GitHub Issues take the repo.
if [ "$KIND" = linear ]; then
  FOLLOWUP='--cycle current --label "<affected repo label: '"$TRACKER"' config label-for-repo <repo>>"'
else
  FOLLOWUP='--repo <affected repo>'
fi

cache_head() { jq -r --arg pr "$1" '.[$pr].head // ""' "$CACHE"; }
cache_set() { # pr status head
  [ -n "$DRY_RUN" ] && return
  jq --arg pr "$1" --arg status "$2" --arg head "$3" '.[$pr] = {status:$status, head:$head}' "$CACHE" > "$CACHE.tmp" && mv "$CACHE.tmp" "$CACHE"
}

fix_prompt() { # pr repo n branch base conflicted(yes|no) reviewers
  local pr=$1 repo=$2 n=$3 branch=$4 base=$5 conflicted=$6 reviewers=$7
  cat <<EOF
You are in a git worktree of $ORG/$repo checked out (detached HEAD) at the head of pull request $pr, branch \`$branch\`, base \`$base\`. Bring the PR to a review-clean state, up to date with origin/$base. Never merge the PR and never push: when you finish, the runner squashes everything into one commit on top of origin/$base, with the PR title and description as its message, and force-pushes it.

Current state:
- origin/$base is not merged in yet. $([ "$conflicted" = yes ] && echo "GitHub reports the branch CONFLICTS with it." || echo "GitHub reports no conflict (or has not computed one yet).")
- Reviewers whose latest review requests changes: ${reviewers:-none}.

Do all of the following, in order. Steps 1 and 4 are mandatory whatever the review state is.

1. Read the entire discussion before touching anything:
   - the PR itself: \`gh pr view $pr --json title,body,comments\`;
   - every review, including approvals and plain comments, not just the ones requesting changes: \`gh api repos/$ORG/$repo/pulls/$n/reviews\`;
   - every inline review comment and its thread, resolved or not: \`gh api repos/$ORG/$repo/pulls/$n/comments --paginate\`;
   - every conversation comment on the PR: \`gh api repos/$ORG/$repo/issues/$n/comments --paginate\`;
   - the ticket the PR is for, referenced in its title, branch or body (IDs look like $EXAMPLE): \`$TRACKER get <ID> --comments\`.
   Build the full list of open points: everything a reviewer asked for, questioned or flagged, plus anything on the issue that the PR was supposed to cover and doesn't. Ignore only points that already have a clear reply from me or a later commit that addressed them.

2. Resolve every open point first, one at a time, before merging $base. Research first: read this repo's own \`CLAUDE.md\` and \`docs/\` (the convention source of truth), the surrounding code and its callers; when the PR touches a contract another repo consumes, check the change chains in $CONTEXT; use WebSearch/WebFetch when the point is about a library, API, protocol or best practice you are not certain about. Then either
   - fix it: change the code as the reviewer asked, or in a better way if the research shows one; or
   - respond: if the reviewer is mistaken or there is a real trade-off, reply on that exact thread with the reasoning and the evidence (\`gh api repos/$ORG/$repo/pulls/$n/comments/<comment id>/replies -f body='...'\` for inline comments, \`gh pr comment $pr --body '...'\` for review-level or conversation points). Plain text, brief, no hedging; no AI, Claude or Anthropic mentions.
   Scope: default to fixing it in this PR. A valid point that is related to the change and small enough to fold in (same repo, hours of work, not days) is fixed here, even when it reaches past the diff. Only a point that is big (needs its own design, or more than this PR can carry) or unrelated to the change (a leg in another repo, a pre-existing bug the PR only exposes) gets a follow-up ticket instead: first \`$TRACKER list --open --since 90\` and reuse a ticket for the same problem, else \`$TRACKER create $FOLLOWUP --state todo --title "<what to change>" --body-file <file>\` (body: what was found with file paths and the PR url, why it matters, the concrete fix, and the PR's ticket id). Reply on the thread with the ticket id. Never decline a point as out of scope without a ticket.
   A question gets an answer on its thread even when you also change code. Do not silently skip a point. Commit the fixes.
   A point you can't settle with confidence (a disputed design or correctness call, a subtle concurrency or data bug) goes to one subagent with \`model: "fable"\`, given the point, the thread and this worktree; it returns the fix (committed) or the reply. One at a time, never two working in the worktree at once.

3. Keep the diff minimal and in the style of the surrounding code: no unrelated churn, no speculative abstractions.

4. Merge the latest base: \`git merge --no-edit origin/$base\`. Resolve every conflicted file so that both the branch's intent (including the fixes from step 2) and $base's changes survive: read the surrounding code and \`git log -p origin/$base -- <file>\` to understand what changed on each side; never guess and never drop either side. Then \`git add\` the files and commit.

5. Run the repo's lint, typecheck and tests for what the branch and the merge touched (its CLAUDE.md / docs say how). Fix what breaks, and commit.

6. Leave the worktree clean (\`git status\` shows nothing, untracked files included) with origin/$base merged in; otherwise the runner pushes nothing. Intermediate commit messages are discarded by the squash, but still never mention AI, Claude or Anthropic.

7. The PR title and description become the squashed commit's message, so make them describe the whole change as it now stands (\`gh pr edit $pr --title '...' --body '...'\`), the body 150 words or fewer per docs/code-style.md. Post one short PR comment (\`gh pr comment $pr --body '...'\`) summarizing what was fixed and what was answered. Don't re-request review: the runner does that after pushing.

8. Finish with one line: PR, what was fixed, what was answered, follow-up ticket ids if any.

If you cannot finish safely (a conflict whose right resolution needs a human decision, a point that needs product input, a secret or infra access), finish with one line: \`BLOCKED: <reason>\`, and nothing is pushed.
EOF
}

# Squash the worktree's HEAD into one commit on origin/<base> and force-push it over <head>,
# then re-request review. Returns 1 (nothing pushed) when the session left the tree unfit.
squash_push() { # wt pr owner repo branch base head reviewers
  local wt=$1 pr=$2 owner=$3 repo=$4 branch=$5 base=$6 head=$7 reviewers=$8 r
  case $branch in master|main|"$base") echo "$pr refusing to force-push $branch"; return 1 ;; esac
  [ -z "$(git -C "$wt" status --porcelain)" ] || { echo "$pr worktree not clean, nothing pushed"; return 1; }
  git -C "$wt" merge-base --is-ancestor "origin/$base" HEAD || { echo "$pr origin/$base not merged in, nothing pushed"; return 1; }
  # already one commit on the latest base and the session changed nothing: leave the head alone
  if [ "$(git -C "$wt" rev-list --count "origin/$base..$head")" = 1 ] \
     && [ "$(git -C "$wt" rev-parse "$head^")" = "$(git -C "$wt" rev-parse "origin/$base")" ] \
     && git -C "$wt" diff --quiet "$head" HEAD; then return 0; fi
  git -C "$wt" reset -q --soft "origin/$base"
  git -C "$wt" diff --cached --quiet && { echo "$pr no changes left against origin/$base, nothing pushed"; return 1; }
  # whitespace cleanup: the default one strips markdown headings as comments
  # explicit returns: errexit is off inside the caller's `if`
  gh pr view "$pr" --json title,body --jq '.title + "\n\n" + .body' | git -C "$wt" commit -q --cleanup=whitespace -F - || return 1
  git -C "$wt" -c credential.helper= -c 'credential.helper=!gh auth git-credential' push -q \
    --force-with-lease="refs/heads/$branch:$head" "https://$HOST/$owner/$repo.git" "HEAD:refs/heads/$branch" || return 1
  for r in $reviewers; do gh pr edit "$pr" --add-reviewer "$r" >/dev/null; done
}

run_pass() {
  filter=(--owner "$ORG" --state open --author @me --created ">$since" --limit 200)
  if [ "$REPOS" != all ]; then for r in $REPOS; do filter+=(--repo "$ORG/$r"); done; fi
  prs=$(gh search prs "${filter[@]}" --json url --jq '.[].url' | sort)
  echo "$(now) $(grep -c . <<<"$prs") open PRs authored by $ME since $since${DRY_RUN:+ (dry run)}"

  for pr in $prs; do
    info=$(gh pr view "$pr" --json isDraft,state,mergeable,headRefOid,headRefName,baseRefName,reviews,commits 2>/dev/null) || { echo "$pr gh pr view failed"; continue; }
    [ "$(jq -r .state <<<"$info")" = OPEN ] && [ "$(jq -r .isDraft <<<"$info")" = false ] || { echo "$pr skipped: not open or draft"; continue; }
    head=$(jq -r .headRefOid <<<"$info"); branch=$(jq -r .headRefName <<<"$info"); base=$(jq -r .baseRefName <<<"$info")
    # mergeable is UNKNOWN while GitHub recomputes; the next pass catches it
    conflicted=$([ "$(jq -r .mergeable <<<"$info")" = CONFLICTING ] && echo yes || echo no)
    # reviewers (not me) whose latest review is REQUEST_CHANGES, unless a commit landed after it
    reviewers=$(jq -r --arg me "$ME" '
      (.commits | map(.committedDate) | max) as $last
      | [.reviews[] | select(.author.login != $me)] | group_by(.author.login)
      | map(sort_by(.submittedAt) | last | select(.state == "CHANGES_REQUESTED" and .submittedAt > $last) | .author.login)
      | join(" ")' <<<"$info")
    [ "$conflicted" = yes ] || [ -n "$reviewers" ] || { echo "$pr skipped: nothing to fix"; continue; }
    [ "$(cache_head "$pr")" != "$head" ] || { echo "$pr skipped: already attempted on this head, see $LOGDIR"; continue; }

    repo=$(sed -E 's#.*/([^/]+)/pull/.*#\1#' <<<"$pr"); n=$(sed -E 's#.*/pull/([0-9]+)$#\1#' <<<"$pr")
    # by remote URL, not folder name: a clone may sit in a differently named folder
    clone=$("$ROOT/scripts/repo-path.sh" "$repo" 2>/dev/null) || { echo "$pr skipped: no local clone of $ORG/$repo"; continue; }
    echo "$pr fixing @ $head: conflicted=$conflicted changes requested by ${reviewers:-nobody}"
    [ -z "$DRY_RUN" ] || continue

    # fresh detached worktree at the PR head; detached so a leftover implement worktree on the same branch can't block it
    wt="$WORKTREES/$repo-$n"
    fetch_worktree "$clone" "$ORG" "$repo" "$wt" "$branch"

    log="$LOGDIR/$repo-$n.$(date +%s).log"
    if (cd "$wt" && fix_prompt "$pr" "$repo" "$n" "$branch" "$base" "$conflicted" "$reviewers" | claude -p --model "$MODEL" --fallback-model opus,fable "${claude_flags[@]}") > "$log" 2>&1 \
       && ! grep -q '^BLOCKED:' "$log" && squash_push "$wt" "$pr" "$ORG" "$repo" "$branch" "$base" "$head" "$reviewers" >> "$log" 2>&1; then
      st=$([ "$(gh pr view "$pr" --json headRefOid --jq .headRefOid)" != "$head" ] && echo fixed || echo replied)
      cache_set "$pr" "$st" "$head"; echo "$pr $st: $(tail -n1 "$log")"
      git -C "$clone" worktree remove --force "$wt"
    elif grep -qiE 'usage credits|usage limit|rate limit' "$log"; then
      echo "$pr pass aborted: out of credits / rate limited, see $log"; return
    else
      cache_set "$pr" failed "$head"; echo "$pr FAILED, worktree kept at $wt, see $log: $(tail -n1 "$log")"
    fi
  done
}

fetch_worktree() { # clone owner repo wt branch
  git -C "$1" -c credential.helper= -c 'credential.helper=!gh auth git-credential' \
    fetch -q "https://$HOST/$2/$3.git" '+refs/heads/*:refs/remotes/origin/*'
  [ -d "$4" ] && git -C "$1" worktree remove --force "$4"
  git -C "$1" worktree add -q --detach "$4" "origin/$5"
}

url_prompt() { # pr owner repo n branch base
  local pr=$1 owner=$2 repo=$3 n=$4 branch=$5 base=$6
  cat <<EOF
Fix PR $pr ($owner/$repo, \`$branch\` -> \`$base\`). You are in a detached worktree at its head. Never merge the PR and never push: when you finish, the runner squashes everything into one commit on top of origin/$base, with the PR title and description as its message, and force-pushes it.

1. First read every review, inline comment, thread and PR comment: \`gh pr view $pr --json body,comments\`, \`gh api repos/$owner/$repo/pulls/$n/{reviews,comments}\`, \`gh api repos/$owner/$repo/issues/$n/comments\`. Fix each point that is valid and that you can verify independently from the code and/or web research, trusting only authoritative sources (official docs, specs, upstream repos); a reviewer's claim alone is no proof. A point you can't settle with confidence goes to one subagent with \`model: "fable"\`, given the point, the thread and this worktree, one at a time. Default to fixing in this PR: a valid point that is related and small enough to fold in (same repo, hours not days) is fixed here even past the diff. Only a big point (its own design, more than this PR can carry) or an unrelated one (another repo, a pre-existing bug the PR only exposes) gets a follow-up ticket instead: \`$TRACKER list --open --since 90\` to reuse one, else \`$TRACKER create $FOLLOWUP --state todo --title "<what to change>" --body-file <file>\`, and a reply naming it. Reply with evidence on the thread of any point you don't fix; skip none silently. Commit.
2. Then always merge the latest base: \`git merge --no-edit origin/$base\`. Resolve conflicts keeping the intent of both sides, your fixes included (\`git log -p origin/$base -- <file>\`); commit.
3. Run the repo's lint and tests (see its CLAUDE.md); fix what breaks and commit. Leave \`git status\` empty, untracked files included, or nothing gets pushed.
4. Make the PR title and description describe the whole change as it now stands (\`gh pr edit $pr --title '...' --body '...'\`, body 150 words or fewer): they become the squashed commit message.
5. Never mention Claude, AI or Anthropic in commits, replies, comments or the description, and add no co-author trailers. Write as a human.

Finish with one line: what was fixed/answered, or \`BLOCKED: <reason>\` if it can't be done safely (nothing is pushed then).
EOF
}

# one PR by URL: run the session only if the PR is open, not draft, not approved, and has
# a conflict, changes requested, or unresolved review threads. Ignores author, age and cache.
run_url() {
  local pr=${1%/} owner repo n info clone wt log threads conflicted changes approved branch base head reviewers
  [[ $pr =~ ^https://[^/]+/([^/]+)/([^/]+)/pull/([0-9]+)$ ]] || { echo "not a PR URL: $pr" >&2; exit 2; }
  owner=${BASH_REMATCH[1]}; repo=${BASH_REMATCH[2]}; n=${BASH_REMATCH[3]}
  info=$(gh pr view "$pr" --json isDraft,state,mergeable,headRefOid,headRefName,baseRefName,reviews 2>/dev/null) || { echo "$pr skipped: PR not found"; return; }
  [ "$(jq -r .state <<<"$info")" = OPEN ] && [ "$(jq -r .isDraft <<<"$info")" = false ] || { echo "$pr skipped: not open or draft"; return; }
  # latest review per reviewer decides
  changes=$(jq -r '[.reviews | group_by(.author.login)[] | sort_by(.submittedAt) | last | .state] | any(. == "CHANGES_REQUESTED")' <<<"$info")
  approved=$(jq -r '[.reviews | group_by(.author.login)[] | sort_by(.submittedAt) | last | .state] | any(. == "APPROVED")' <<<"$info")
  reviewers=$(jq -r '[.reviews | group_by(.author.login)[] | sort_by(.submittedAt) | last | select(.state == "CHANGES_REQUESTED") | .author.login] | join(" ")' <<<"$info")
  conflicted=$([ "$(jq -r .mergeable <<<"$info")" = CONFLICTING ] && echo yes || echo no)
  threads=$(gh api graphql -F o="$owner" -F r="$repo" -F n="$n" -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved}}}}}' \
    --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length')
  if [ "$conflicted" = no ] && [ "$changes" = false ] && { [ "$approved" = true ] || [ "$threads" = 0 ]; }; then
    echo "$pr skipped: nothing to fix (approved=$approved, unresolved threads=$threads, no conflicts)"; return
  fi
  echo "$pr needs work: conflicted=$conflicted changes_requested=$changes unresolved_threads=$threads"
  [ -z "$DRY_RUN" ] || return 0
  clone=$("$ROOT/scripts/repo-path.sh" "$repo" 2>/dev/null) || { echo "$pr skipped: no local clone of $owner/$repo"; return; }
  branch=$(jq -r .headRefName <<<"$info"); base=$(jq -r .baseRefName <<<"$info"); head=$(jq -r .headRefOid <<<"$info")
  wt="$WORKTREES/$repo-$n"
  fetch_worktree "$clone" "$owner" "$repo" "$wt" "$branch"
  log="$LOGDIR/$repo-$n.$(date +%s).log"
  (cd "$wt" && url_prompt "$pr" "$owner" "$repo" "$n" "$branch" "$base" \
    | claude -p --model "$MODEL" --fallback-model opus,fable "${claude_flags[@]}") 2>&1 | tee "$log"
  grep -q '^BLOCKED:' "$log" && { echo "$pr blocked, nothing pushed, worktree kept at $wt"; return; }
  if squash_push "$wt" "$pr" "$owner" "$repo" "$branch" "$base" "$head" "$reviewers" 2>&1 | tee -a "$log"; then
    git -C "$clone" worktree remove --force "$wt"
  else
    echo "$pr nothing pushed, worktree kept at $wt"
  fi
}

if [ -n "${1:-}" ]; then
  run_url "$1"
elif [ -n "${INTERVAL:-}" ]; then
  while true; do
    run_pass
    echo "$(now) pass done, next in ${INTERVAL}s"
    sleep "$INTERVAL"
  done
else
  run_pass
fi
