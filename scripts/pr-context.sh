#!/usr/bin/env bash
# Collect everything needed to review one PR, without touching the working
# copy (fetch only — never checkout/switch/merge; the checkouts hold uncommitted work).
# Extracts the PR head into <out>/tree so files can be read at the right commit: the clone
# sits on whatever branch its owner left it on, so reading it gives the wrong content and
# the wrong line numbers.
#
#   pr-context.sh <repo> <number> [outdir]
#
# The clone is found by remote URL (scripts/repo-path.sh), so folder names don't matter.
# With no clone, the diff comes from `gh` and there is no tree.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
. "$ROOT/scripts/env.sh"                    # ORG, and GH_HOST for a non-default forge
# What a ticket reference looks like in this project's tracker: \bABC-[0-9]+\b for Linear,
# <repo>#N for GitHub Issues. Falls back to the generic Linear shape when the CLI can't answer.
TICKET_RE=$("$HERE/tracker.py" config ticket-regex 2>/dev/null || echo '\b[A-Z][A-Z0-9]+-[0-9]+\b')
REPO=${1:?usage: pr-context.sh <repo> <number> [outdir]}
NUM=${2:?usage: pr-context.sh <repo> <number> [outdir]}
OUT=${3:-${TMPDIR:-/tmp}/pr-review}/$REPO-$NUM
mkdir -p "$OUT"

CLONE=$("$HERE/repo-path.sh" "$REPO" 2>/dev/null || true)

gh pr view "$NUM" --repo "$ORG/$REPO" \
    --json number,title,url,author,isDraft,state,mergeable,mergeStateStatus,baseRefName,headRefName,body,labels,additions,deletions,changedFiles,reviewDecision,commits,statusCheckRollup \
    >"$OUT/pr.json"

jq -r '
    "PR        \(.url)
title     \(.title)
author    \(.author.login)   draft=\(.isDraft)   base=\(.baseRefName)   head=\(.headRefName)
size      +\(.additions)/-\(.deletions) across \(.changedFiles) files
mergeable \(.mergeable) / \(.mergeStateStatus)   reviewDecision=\(.reviewDecision // "NONE")
labels    \([.labels[].name] | join(", "))
"' "$OUT/pr.json"

echo "--- commits ---"
jq -r '.commits[] | "\(.oid[0:8]) \(.messageHeadline)"' "$OUT/pr.json"

echo
echo "--- checks ---"
jq -r '(.statusCheckRollup // [])[]
    | "\(.conclusion // .state // "PENDING")\t\(.name // .context)\t\(.detailsUrl // "")"' "$OUT/pr.json" |
    sort -u | awk -F'\t' '{printf "%-12s %-34s %s\n", $1, $2, $3}'

echo
echo "--- ticket ---"
# `|| true`: no match makes grep exit 1, and under pipefail that would end the run here —
# on exactly the PR whose missing ticket is itself worth reporting.
jq -r '[.title, .headRefName, .body] | join(" ")' "$OUT/pr.json" |
    grep -oiE "$TICKET_RE" | { case "$TICKET_RE" in *'#'*) cat;; *) tr 'a-z' 'A-Z';; esac; } | sort -u |
    tee "$OUT/tickets.txt" | sed 's/^/  /' || true
[[ -s "$OUT/tickets.txt" ]] || echo "  (none found — every PR should carry a ticket)"

echo
echo "--- body ---"
jq -r '.body // ""' "$OUT/pr.json" | sed 's/^/  /'

echo
if [[ -n "$CLONE" && -d "$CLONE/.git" ]]; then
    git -C "$CLONE" fetch -q origin "pull/$NUM/head"
    HEAD_SHA=$(git -C "$CLONE" rev-parse FETCH_HEAD)
    BASE=$(jq -r .baseRefName "$OUT/pr.json")
    git -C "$CLONE" fetch -q origin "$BASE"
    MB=$(git -C "$CLONE" merge-base "$HEAD_SHA" "origin/$BASE")
    git -C "$CLONE" diff "$MB..$HEAD_SHA" >"$OUT/diff.patch"
    git -C "$CLONE" diff --stat "$MB..$HEAD_SHA" >"$OUT/diffstat.txt"
    # Read-only copy of the PR head (tracked files only). A root config.yml can hold real
    # credentials (a repo may track one); drop it rather than hand it to a reviewer.
    rm -rf "$OUT/tree" && mkdir -p "$OUT/tree"
    git -C "$CLONE" archive --format=tar "$HEAD_SHA" | tar -x -C "$OUT/tree"
    rm -f "$OUT/tree/config.yml"
    echo "clone     $CLONE   head=$HEAD_SHA   merge-base=$MB"
    echo "tree      $OUT/tree   <- grep/sed/cat files HERE, never in $CLONE"
    # The repo's own docs are the convention source of truth — read them before judging
    # conventions or naming a repo-specific trap.
    [[ -f "$OUT/tree/CLAUDE.md" ]] && echo "conventions $OUT/tree/CLAUDE.md"
    if [[ -d "$OUT/tree/docs" ]]; then
        echo "docs      $OUT/tree/docs: $(ls "$OUT/tree/docs" | tr '\n' ' ')"
    fi
    if [[ ! -s "$OUT/diff.patch" ]]; then
        # already merged into base (or nothing to compare) — fall back to GitHub's diff
        gh pr diff "$NUM" --repo "$ORG/$REPO" >"$OUT/diff.patch"
        gh pr diff "$NUM" --repo "$ORG/$REPO" --name-only >"$OUT/diffstat.txt"
        echo "note      local range was empty (PR merged?) — diff taken from GitHub"
    fi
else
    gh pr diff "$NUM" --repo "$ORG/$REPO" >"$OUT/diff.patch"
    gh pr diff "$NUM" --repo "$ORG/$REPO" --name-only >"$OUT/diffstat.txt"
    echo "clone     (none found for $ORG/$REPO) — diff fetched via gh; no tree, read files with:"
    echo "          gh api -H 'Accept: application/vnd.github.raw' \"repos/$ORG/$REPO/contents/<path>?ref=<head-sha>\""
fi

echo
echo "--- changed files ---"
cat "$OUT/diffstat.txt"

echo
echo "diff:     $OUT/diff.patch"
echo "metadata: $OUT/pr.json"
[[ -d "$OUT/tree" ]] && echo "tree:     $OUT/tree" || true

# Last line, copy-pasteable: the skill's $OUT paths only work if this is actually set.
echo
echo "export OUT=$(printf '%q' "$OUT")"
