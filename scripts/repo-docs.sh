#!/usr/bin/env bash
# Print a repo's own agent docs: its root CLAUDE.md in full, then every other CLAUDE.md and
# docs/ file path, read from origin's default branch (not the working tree, which may be dirty
# or behind). The runners put this in the prompt of sessions that start in this folder, where
# a repo's CLAUDE.md would otherwise never load.
#
#   repo-docs.sh api [worker ...]     -> one section per repo
#   repo-docs.sh --path DIR           -> a worktree or checkout as it stands on disk
#
# A repo with no local clone is read through `gh`. Never fails on a missing doc: the section
# says there is none, so a prompt can include the output unconditionally.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ORG=${ORG:-$("$HERE/tracker.py" config org)}
MAX_DOCS=${MAX_DOCS:-80}

section() { # label tree-listing(stdin) root-claude-md-file
    local label=$1 listing others
    listing=$(cat)
    echo "### $label: CLAUDE.md and docs"
    if [[ -s "$2" ]]; then echo '```markdown'; cat "$2"; echo '```'; else echo "(no root CLAUDE.md)"; fi
    others=$(grep -E '(^|/)CLAUDE\.md$|^docs?/' <<<"$listing" | grep -vx 'CLAUDE.md' | head -n "$MAX_DOCS")
    if [[ -n "$others" ]]; then echo "Also in the repo (read what applies before changing it):"; sed 's/^/- /' <<<"$others"; fi
    echo
}

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT

if [[ "${1:-}" == "--path" ]]; then
    dir=${2:?usage: repo-docs.sh --path DIR}
    cp "$dir/CLAUDE.md" "$tmp" 2>/dev/null || : >"$tmp"
    git -C "$dir" ls-files | section "$dir" "$tmp"
    exit 0
fi

[[ $# -gt 0 ]] || { echo "usage: repo-docs.sh <repo>... | --path DIR" >&2; exit 2; }
for repo in "$@"; do
    repo=${repo#./}
    if clone=$("$HERE/repo-path.sh" "$repo" 2>/dev/null); then
        git -C "$clone" fetch -q origin 2>/dev/null || true
        ref=$(git -C "$clone" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null) || ref=
        if [[ -z "$ref" ]]; then
            for b in main master; do git -C "$clone" rev-parse -q --verify "origin/$b" >/dev/null && ref=origin/$b && break; done
        fi
        if [[ -n "$ref" ]]; then
            git -C "$clone" show "$ref:CLAUDE.md" >"$tmp" 2>/dev/null || : >"$tmp"
            git -C "$clone" ls-tree -r --name-only "$ref" | section "$repo ($ref)" "$tmp"
            continue
        fi
    fi
    # no clone (or no default branch known): the forge has it
    gh api -H 'Accept: application/vnd.github.raw' "repos/$ORG/$repo/contents/CLAUDE.md" >"$tmp" 2>/dev/null || : >"$tmp"
    gh api "repos/$ORG/$repo/git/trees/HEAD?recursive=1" --jq '.tree[].path' 2>/dev/null | section "$repo (default branch, via gh)" "$tmp"
done
