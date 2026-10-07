# Stacked PRs

A stacked PR is branched off another PR's branch and opened with that branch as its base,
so it shows only its own diff. GitHub has no stack command: a stack is just branches plus PR
bases.

## When to stack

Stack only for **one ticket in several steps**: the change is too big to review as one PR and
splits into layers that build on each other (schema, then API, then UI). Each PR must stand on
its own in review.

Otherwise, don't stack. Same repo or same area is not enough: work that can merge in any
order gets its own PR off the default branch. A PR stacked on an unrelated one gets blocked
by a review it has nothing to do with, and a PR stacked on another ticket's unmerged PR goes
stale with it.

## Needing an open PR is not a stack

- **The open PR is ours** and the ticket is a small addition to the same change: patch that PR
  (commits on its head branch, a `Fixes` line added to its body) instead of opening another.
  One PR, one review.
- **Anything else** (someone else's PR, a fork, a change of ours too big to fold in): the ticket
  is *blocked* by the PR's ticket. Record it with `./scripts/tracker.py block <ID> --by <other>`
  (a GitHub issue dependency or a Linear `blocks` relation), leave the ticket in Todo, and let the
  implement pass pick it up once the blocker is closed (`list --unblocked` hides it until then).
  Never implement it against the unmerged branch.

A cross-repo chain (`contexts/<project>/cross-repo-changes.md`) is not a stack either: each PR
is off its own repo's default branch and links the others with `Depends on <url>`.

## Build a stack

Bottom first. Each branch is cut from the one below, and each PR's base is its parent's
branch, never a fork's:

```bash
git -C ../<repo> worktree add ../.worktrees/<ticket>/<repo> -b <branch> origin/<default>
# commit, push
gh pr create -R <owner>/<repo> --head <branch> --base <default> ...

git -C ../<repo> worktree add ../.worktrees/<ticket>/<repo>-2 -b <branch>-api <branch>
# commit, push
gh pr create -R <owner>/<repo> --head <branch>-api --base <branch> ...
```

Each stacked PR's body says `Depends on <parent PR url>`, so the merge order is visible.

## Keep it up to date

- **Parent got new commits:** merge the parent branch into the child (`git merge
  origin/<parent>`) and push. Merge, not rebase: a rebase needs a force-push, and a force-push
  is only allowed on a branch you created in that same run.
- **Parent merged:** when the parent's branch is deleted after the merge, GitHub retargets
  its children to the parent's base on its own
  ([docs](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/merging-a-pull-request)).
  If the branch was kept, retarget by hand: `gh pr edit <n> --base <default>`. Then merge
  `origin/<default>` into the child, so a squash-merged parent doesn't show up again in its
  diff.
- **Parent closed without merging:** the child is based on work that won't land. Retarget it
  to the default branch and bring in what it needs, or close it.
