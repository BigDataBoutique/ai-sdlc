#!/usr/bin/env bash
# Print the local clone path for a tracked repo (org and host from projects.yaml), resolved by its remote URL.
#
#   repo-path.sh api            -> /Users/you/code/acme-api
#   repo-path.sh worker         -> /Users/you/code/worker
#   repo-path.sh not-cloned     -> (nothing, exit 1)
#
# Folder names do not have to match repo names, and on some machines they don't. A table
# mapping one to the other would be wrong on the next machine, so this reads `git remote`
# instead. It also stops a same-named clone of a *different* forge or org standing in for
# the real repo.
#
# Exit 1 with no output when no clone matches: callers fall back to reading the PR through
# `gh`, which works without a checkout.
#
#   --list   print every "<repo> <path>" pair found, for debugging a machine's layout
#
# Env: REPOS_ROOT overrides where the repos are looked for (default: the parent of ai-sdlc).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ORG=${ORG:-$("$HERE/tracker.py" config org)}
HOST=${HOST:-$("$HERE/tracker.py" config host)}
# Parent of ai-sdlc: where the repos live. Overridable, and pull-all.sh exports it, so the
# two agree about which root they are resolving against.
REPOS_ROOT=${REPOS_ROOT:-$(cd "$HERE/../.." && pwd)}

# git@github.com:org/name.git and https://github.com/org/name(.git) both -> org/name
remote_slug() {
    local url
    url=$(git -C "$1" remote get-url origin 2>/dev/null) || return 1
    # Only a remote on the project's forge host can be ours; another host is a different repo
    # that happens to share a folder name.
    case "$url" in *"$HOST"[:/]*) ;; *) return 1 ;; esac
    url=${url%.git}
    printf '%s\n' "${url##*"$HOST"[:/]}"
}

if [[ "${1:-}" == "--list" ]]; then
    for d in "$REPOS_ROOT"/*/; do
        [[ -d "$d/.git" ]] || continue
        slug=$(remote_slug "$d") || continue
        [[ "$slug" == "$ORG"/* ]] || continue
        printf '%-28s %s\n' "${slug#"$ORG"/}" "${d%/}"
    done
    exit 0
fi

REPO=${1:?usage: repo-path.sh <github-repo-name> | --list}

for d in "$REPOS_ROOT"/*/; do
    [[ -d "$d/.git" ]] || continue
    [[ "$(remote_slug "$d" 2>/dev/null)" == "$ORG/$REPO" ]] || continue
    printf '%s\n' "${d%/}"
    exit 0
done
exit 1
