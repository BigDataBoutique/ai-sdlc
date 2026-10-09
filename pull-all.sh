#!/usr/bin/env bash
# Refresh the remote refs of every repo this workspace tracks, and self-update this folder.
#
# The tracked repos (projects.yaml) are fetch only: no working tree is ever touched.
# Worktrees branch off `origin/<default branch>` (docs/conventions.md, the implement skills,
# update-prs.sh), and a fetch in a clone updates `origin/*` for every worktree hanging off
# it - so a fetch is the whole refresh the runners need. This used to be `git pull --ff-only`
# in each checkout, which moved the owner's working tree to get there: it failed on a dirty
# repo ("local changes would be overwritten by merge"), skipped every repo on a feature
# branch, and left master behind whenever either happened. Fetch has none of those states.
#
# ai-sdlc itself (this folder) is the one exception: it's worked in directly, never through a
# worktree, so a fetch alone would leave its own docs and scripts stale on disk. It gets a
# real `git pull --ff-only`, but with the same safety as the rest of this script - skipped
# rather than forced when the tree is dirty, HEAD is detached, or it's on a feature branch.
#
# The list is the repos in projects.yaml - the tracked repos, the same list review-prs.sh sweeps -
# not whatever folders happen to sit beside this one. Each name is
# resolved to a clone by its git remote (scripts/repo-path.sh), so a folder named differently
# still matches and an unrelated clone can't stand in for it. A tracked repo with no clone on
# this machine is logged and skipped, not a failure: not every machine clones all of them.
# Fetch failures (network, no origin) are logged and make the exit code non-zero.
#
#   ./pull-all.sh            # log also goes to logs/pull-all.log
#
# Env:
#   REPOS_ROOT where the repos live (default: per project, see scripts/env.sh; with several
#              projects env.sh runs this once per project, each against its own root)
#   REPOS     space-separated repo allowlist (default: the repos in projects.yaml)
#   DRY_RUN=1 list what would be fetched, fetch nothing
set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
now() { date +%Y-%m-%dT%H:%M:%S%z; }   # `date -Is` is GNU-only; BSD date rejects it outright
ROOT=$(cd "$(dirname "$0")" && pwd)
. "$ROOT/scripts/env.sh"                          # REPOS_ROOT (exported: repo-path.sh resolves against it), ORG ...
mkdir -p "$ROOT/logs"; exec > >(tee -a "$ROOT/logs/pull-all.log") 2>&1

# One list, projects.yaml, shared with review-prs.sh and scripts/awaiting-review.sh.
REPOS=${REPOS:-$("$ROOT/scripts/tracker.py" config repos | tr '\n' ' ')}

# Two passes fetching one repo fight over its refs, and the errors that produced - "cannot
# lock ref ... is at X but expected Y", "Cannot fast-forward to multiple branches" (one pass
# reading .git/FETCH_HEAD another was rewriting) - read as broken repos when they were only
# overlap. sdlc.py starts a pass per interval and on a keypress, and orphans its children if
# it is interrupted rather than quit, so overlap is normal. mkdir is atomic everywhere; macOS
# has no flock. A pass killed mid-run leaves the directory, so an hour-old lock is taken over.
LOCKDIR="$ROOT/logs/.pull-all.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
    echo "$(now) stale lock older than 1h, taking over"
  else
    echo "$(now) previous run still going, exiting"; exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# Which ref to report movement on: origin's own HEAD, and for a clone that has never had it
# set, whichever of master/main exists. Not every repo here calls its default branch master.
default_ref() {
  git -C "$1" symbolic-ref -q --short refs/remotes/origin/HEAD && return
  for c in origin/master origin/main; do
    git -C "$1" rev-parse -q --verify "$c" >/dev/null && { echo "$c"; return; }
  done
  echo origin/master
}

# Pull this folder itself, ff-only, so a scheduled run keeps ai-sdlc's own docs and scripts
# current. Unlike the tracked repos below, this one is worked in directly - so it only
# fast-forwards when that's safe: a clean tree, HEAD on a branch, and that branch tracking
# origin's default. Anything else (dirty, detached, feature branch) is logged and left alone,
# the same way a dirty sibling repo is left alone by the fetch-only loop.
self_update() {
  local d="$ROOT" out before after def cur
  [ -n "$(git -C "$d" remote)" ] || { echo "ai-sdlc: no remote, skipped"; return; }
  if [ -n "${DRY_RUN:-}" ]; then echo "ai-sdlc: would pull ($d)"; return; fi
  if ! out=$(git -C "$d" fetch --prune --quiet 2>&1); then
    fail=$((fail+1))
    echo "ai-sdlc: FAILED: $(grep -m1 -E '^(error|fatal):' <<<"$out" || tail -n1 <<<"$out")"
    return
  fi
  def=$(git -C "$d" symbolic-ref -q --short refs/remotes/origin/HEAD) || def=origin/master
  cur=$(git -C "$d" symbolic-ref -q --short HEAD) || { echo "ai-sdlc: detached HEAD, skipped pull"; return; }
  if [ "origin/$cur" != "$def" ]; then
    echo "ai-sdlc: on $cur, not ${def#origin/}, skipped pull"; return
  fi
  if [ -n "$(git -C "$d" status --porcelain)" ]; then
    echo "ai-sdlc: dirty working tree, skipped pull"; return
  fi
  before=$(git -C "$d" rev-parse --short HEAD)
  if out=$(git -C "$d" merge --ff-only "$def" 2>&1); then
    after=$(git -C "$d" rev-parse --short HEAD)
    if [ "$before" = "$after" ]; then echo "ai-sdlc: up to date"
    else echo "ai-sdlc: $before..$after"; fi
  else
    fail=$((fail+1))
    echo "ai-sdlc: FAILED: $(grep -m1 -E '^(error|fatal):' <<<"$out" || tail -n1 <<<"$out")"
  fi
}

echo "$(now) fetch-all in $REPOS_ROOT${DRY_RUN:+ (dry run)}"

fail=0 missing=0
self_update
for r in $REPOS; do
  d=$("$ROOT/scripts/repo-path.sh" "$r" 2>/dev/null) || d=
  if [ -z "$d" ]; then missing=$((missing+1)); echo "$r: no local clone, skipped"; continue; fi
  if [ -n "${DRY_RUN:-}" ]; then echo "$r: would fetch ($d)"; continue; fi
  # `git fetch` in a repo with no remote is a silent no-op (exit 0), which would otherwise
  # be reported as a successful refresh.
  [ -n "$(git -C "$d" remote)" ] || { echo "$r: no remote, skipped"; continue; }
  ref=$(default_ref "$d")
  before=$(git -C "$d" rev-parse --short "$ref" 2>/dev/null) || before=
  # --prune drops remote-tracking refs the remote deleted; a stale one (origin/fix) also
  # blocks fetching a branch nested under it (origin/fix/contact-us-nav-link).
  if out=$(git -C "$d" fetch --prune --quiet 2>&1); then
    after=$(git -C "$d" rev-parse --short "$ref" 2>/dev/null) || after=
    if [ -z "$after" ]; then echo "$r: fetched (no $ref)"
    elif [ "$before" = "$after" ]; then echo "$r: up to date"
    else echo "$r: $before..$after"; fi
  else fail=$((fail+1)); echo "$r: FAILED: $(grep -m1 -E '^(error|fatal):' <<<"$out" || tail -n1 <<<"$out")"; fi
done
echo "$(now) done, $fail failed, $missing not cloned here"; exit $((fail > 0))
