#!/usr/bin/env bash
# List open PRs in the tracked repos awaiting the current user's review.
#   awaiting-review.sh          review-requested:@me, plus re-reviews the user owes
#   awaiting-review.sh --all    + every open non-draft PR in the swept repos that the
#                               user didn't author and hasn't reviewed
#
# Repos come from projects.yaml (the same list review-prs.sh sweeps). Bot-authored PRs
# (dependabot, CI image bumps) are skipped everywhere.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
. "$ROOT/scripts/env.sh"                    # ORG, and GH_HOST for a non-default forge
# Not mapfile: macOS ships bash 3.2, which has neither mapfile nor readarray.
REPOS=$("$HERE/tracker.py" config repos)
ME=$(gh api user -q .login)
# gh search takes the repos as flags: only the tracked ones, not everything else in the org.
REPO_FLAGS=(); for r in $REPOS; do REPO_FLAGS+=(--repo "$ORG/$r"); done

row() { # repo number title author
    printf '%-24s #%-6s %-12s %s\n' "$1" "$2" "$4" "$3"
}

echo "== review requested from $ME =="
gh search prs --owner "$ORG" "${REPO_FLAGS[@]}" --state open --review-requested @me \
    --json repository,number,title,author,isDraft \
    -q '.[] | select(.isDraft|not) | select(.author.is_bot|not)
        | [.repository.name, (.number|tostring), .title, .author.login] | @tsv' \
    -- -author:@me |
    while IFS=$'\t' read -r repo num title author; do
        # Submitting a review clears you from reviewRequests, so a PR you have already
        # reviewed is only back in this list because someone re-requested it. Mark it:
        # an approval here is a request for another pass, not a duplicate to skip.
        # PENDING is your own unsubmitted draft - it never cleared the request, so it
        # is not a prior review and must not be reported as one.
        prior=$(gh api "repos/$ORG/$repo/pulls/$num/reviews" \
            -q "[.[] | select(.user.login == \"$ME\" and .state != \"PENDING\")] | last | .state // empty" 2>/dev/null || true)
        [[ -z "$prior" ]] || title="$title [re-review requested after your $prior]"
        row "$repo" "$num" "$title" "$author"
    done

echo
echo "== reviewed by $ME without approval, author pushed since =="
# GitHub does not re-request a review when the author pushes after a changes-requested
# verdict, so review-requested:@me never shows these. A PR you approved comes back only
# when the author re-requests it, which the section above catches. Compare the head to
# the commit the user's last review was submitted on.
for repo in $REPOS; do
    gh pr list --repo "$ORG/$repo" --state open --limit 50 \
        --json number,title,author,isDraft,headRefOid,latestReviews \
        -q "
            .[]
            | select(.isDraft | not)
            | select(.author.login != \"$ME\")
            | select([.latestReviews[]? | select(.author.login == \"$ME\") | .state]
                     | length > 0 and (index(\"APPROVED\") | not))
            | [(.number|tostring), .title, .author.login] | @tsv" 2>/dev/null |
        while IFS=$'\t' read -r num title author; do
            head=$(gh pr view "$num" --repo "$ORG/$repo" -q .headRefOid --json headRefOid 2>/dev/null)
            reviewed=$(gh api "repos/$ORG/$repo/pulls/$num/reviews" \
                -q "[.[] | select(.user.login == \"$ME\")] | last | .commit_id // empty" 2>/dev/null)
            [[ -n "$reviewed" && "$reviewed" != "$head" ]] || continue
            row "$repo" "$num" "$title [pushed since your review]" "$author"
        done
done

[[ "${1:-}" == "--all" ]] || exit 0

echo
echo "== other open PRs in the swept repos with no review from $ME =="
for repo in $REPOS; do
    gh pr list --repo "$ORG/$repo" --state open --limit 50 \
        --json number,title,author,isDraft,reviewDecision,latestReviews \
        -q "
            .[]
            | select(.isDraft | not)
            | select(.author.login != \"$ME\")
            | select(.author.is_bot | not)
            | select([.latestReviews[]?.author.login] | index(\"$ME\") | not)
            | [(.number|tostring), .title, .author.login, (.reviewDecision // \"NONE\")] | @tsv" 2>/dev/null |
        while IFS=$'\t' read -r num title author decision; do
            row "$repo" "$num" "$title [$decision]" "$author"
        done
done
