---
name: implement-tickets
description: Autonomous pass over the queued tickets in Todo (Linear or GitHub Issues, per projects.yaml) — triage each spec in a Sonnet subagent against the actual code, move ones an open PR already implements to In Review, send already-implemented tickets to QA, vague or contradictory ones back to Design, ones needing a human decision to Requires Discussion, mark blocked ones with the tracker's dependency, implement the rest (Sonnet for fully specified tickets, Opus for designed ones, across repos if needed) with the fewest PRs: patch an open PR of yours, batch small related tickets, else one PR per ticket
argument-hint: "[TICKET-ID ...]"
user-invocable: true
---

# Implement tickets

Runs unattended (`implement-tickets.sh`, driven by `sdlc.py`). **Never stop to ask the user.**
Anything that needs a human decision goes on the ticket as a comment, and the ticket moves to
Requires Discussion.

**Setup, once.** Run from this folder (`ai-sdlc`). Read the project from the config, never from
memory (the runner sets `SDLC_PROJECT`, and every `tracker.py` call follows it):
`ORG=$(./scripts/tracker.py config org)`, `CONTEXT=$(./scripts/tracker.py config context)`
(the project's own docs: design, change chains, local dev, environments; subagents read what
applies), `QUEUE=$(./scripts/tracker.py config queue-label)` (empty when the stage alone is the
queue), `KIND=$(./scripts/tracker.py config kind)`, `ME=$(gh api user --jq .login)` (the PR
author; `./scripts/tracker.py me --json` gives the tracker user, whose `id` is a ticket's
`creator.id` when you filed it). When `config host` is not github.com, `export GH_HOST=<host>`.
`REPOS_ROOT` (env, else the parent of this folder) holds the repos as siblings; resolve a clone
with `./scripts/repo-path.sh <repo>`. Hand subagents absolute paths, never a hardcoded home path.

You are the orchestrator, so keep your own context thin. Every review, research, implementation
and verification step runs in a fresh `general-purpose` subagent; you hold only ticket IDs,
verdicts and PR links. Subagents fetch ticket details themselves: pass the identifier, not the text.

**Never end your turn while a subagent is running.** A headless session that stops talking is
done: whatever is still running is orphaned and its ticket stays In Progress. Run subagents in the
foreground (several `Agent` calls in one message for parallel work), and finish only once every
ticket in your list has moved out of In Progress.

**Models.** Always pass `model` on the Agent call, as an alias so the latest version runs. Triage
is `"sonnet"`: a cheap reading task. Implementation is `"sonnet"` for a SPECIFIC verdict and
`"opus"` for DESIGNED. Verification is always `"opus"`. A subagent that fails because its model is
overloaded, unavailable or out of usage reruns once a step up: `"sonnet"` on `"opus"`, `"opus"` on
`"fable"`.

**The tracker is driven through `./scripts/tracker.py`** (`--help`, `howto`). Every state move,
label change, assignment and ticket creation goes through it, identically on Linear and GitHub
Issues. Read a ticket with `get <ID> --comments`, comment with `comment <ID> --body-file FILE`.
Stages: `design`, `todo`, `in_progress`, `in_review`, `qa`, `discussion`.

## 1. Queue

- The queue is the ticket IDs in `$ARGUMENTS`, in order: the runner already sorted them by
  priority (Urgent first), then oldest created, and already moved each to In Progress (its claim:
  no other pass touches an In Progress ticket). A single ID is the queue as-is, whatever its state
  or labels. With no arguments, fetch it: `./scripts/tracker.py list --state todo --unblocked
  ${QUEUE:+--label "$QUEUE"} --json`, plus `--cycle current` on Linear (drop it only when the
  user asks for every cycle), and `move <ID> in_progress` each one before anything else. This is
  the implementation order in step 3.
- Every ticket in the queue leaves In Progress before you finish: to In Review, QA, Design,
  Requires Discussion, or back to Todo when blocked. The runner puts back whatever is still
  there, with a comment, so a forgotten hand-off costs a pass.
- Fetch every tracked repo once, up front: for each `<repo> <path>` in
  `./scripts/repo-path.sh --list`, `git -C <path> fetch -q origin`.
- **Candidate repos** are the `repos` field of `tracker.py get <ID> --json` (labels mapped through
  `projects.yaml`; on GitHub Issues the repo the issue lives in). A label naming a sibling folder
  also maps to it. Labels are hints: triage decides from the code which repos are touched.

## 2. Triage: one Sonnet subagent per ticket, all in parallel

Triage is read-only: launch every subagent in one message with `model: "sonnet"`. Give each the
identifier, its candidate repos, the whole queue's IDs, `$ME` and this brief:

> Triage ticket `<ID>` for readiness. Read it with `<ai-sdlc>/scripts/tracker.py get <ID>
> --comments`.
>
> **First check whether an open PR already implements it, before reading any code.** It does if:
> a PR link or attachment on the ticket or its comments is open; `gh search prs <ID> --owner
> <ORG> --state open` finds one; an origin branch is named like the ticket's `branchName`; or an
> open PR in a candidate repo fixes the same thing without naming the ticket (`gh pr list -R
> <ORG>/<repo> --state open --json number,title,body,headRefName,url,author`, then `gh pr diff` on
> any whose title or body matches). If so reply `SKIP <ID>` with the PR URL and change nothing.
>
> Keep the other open PRs you found, with their authors. Read linked tickets and docs, the project
> docs in `<CONTEXT>`, and the relevant code in the candidate repos. Local checkouts may be dirty
> or behind: read `origin/<default branch>` with `git show` / `git grep` (already fetched; don't
> fetch). Use web research where the ticket depends on external APIs, services or libraries.
>
> **Blocked?** The ticket is blocked when the change needs something that is not on the default
> branch yet and that another ticket delivers: an unmerged PR (its ticket is the one named in its
> `Fixes`/`Closes` line or linked to it), or an open ticket it depends on, including one in this
> queue (`<queue IDs>`). Reply `BLOCKED BY <ticket id> <ID>` with one reason line, unless the
> blocker is in this queue and both changes are small enough to ship as one PR (then `BATCH WITH`,
> below). An unmerged PR with no ticket is `UNCLEAR`, naming the PR.
>
> **Fewest PRs.** Note in your plan when an open PR authored by `<ME>` in the same repo already
> changes the code this ticket touches and the ticket is a small addition that belongs in the
> same review: `PATCH <PR url> (<head branch>)`. And when another SPECIFIC/DESIGNED ticket in this
> queue touches the same files or area and both fit one reviewable PR: `BATCH WITH <IDs>`.
>
> A ticket is specific enough when it says what should change, where, and how to tell it worked:
> behaviour before and after (or the bug and the expected behaviour), the repo and code area, and
> a check an engineer can run. "Investigate", "improve", "figure out", a bare title, or a goal
> with no location is not a spec, however big the repo.
>
> Decide, in this order:
> - **IMPLEMENTED**: `origin/<default branch>` already contains the behaviour (the open-PR check
>   ruled out SKIP). Confirm with `git show` / `git grep`. Post one comment naming where it is
>   done (files, functions, the commit or PR that shipped it), then `move <ID> qa`.
> - **UNCLEAR**: needs a decision only a person can make (product tradeoff, scope call, legal or
>   compliance) where the answer turns on judgement, not more reading. Research until the
>   questions are concrete, post one comment (what needs deciding, findings with paths and links,
>   the specific questions, a recommended direction if any), then `move <ID> discussion`.
> - **VAGUE**: the goal is understandable but implementation can't proceed as specified: no code
>   area, expected behaviour or acceptance check, or research found a contradiction (the ticket
>   disagrees with itself or the code, or its premise is wrong) that a design pass could resolve
>   without a product call. Post one comment: the questions the design pass must pin down, or the
>   contradiction with findings and a recommended direction. Then `move <ID> design`; keep the
>   queue label if there is one. `design-tickets.sh` picks it up. A ticket that already has a
>   "Design (ready for implementation)" section is never VAGUE for lacking detail, but is VAGUE if
>   its instructions do not suffice or contradict the code; say which instruction.
> - **DESIGNED**: an engineer could implement it without asking anyone but would still work out
>   the approach or find the files. Resolve small ambiguities with a sensible default following
>   existing patterns; don't bounce a ticket over details.
> - **SPECIFIC**: DESIGNED, and the ticket or its Design section names the files or functions,
>   the behaviour of each change and the tests, so a newcomer could start editing in minutes.
>
> Comments are concise and never mention AI, Claude or Anthropic. When the ticket's `creator.id`
> is the tracker user's (you filed it), a comment carries only what a reader needs to act on: where
> it is already done, what must be decided; one or two lines, no discussion with yourself.
>
> First line of the reply: `SPECIFIC|DESIGNED|VAGUE|UNCLEAR|SKIP|IMPLEMENTED|BLOCKED BY <id> <ID>`;
> one reason line for the last five. For SPECIFIC/DESIGNED add a brief plan: repos and default
> branches, key files, approach, defaults chosen, the PR plan (count, order, cross-repo
> dependencies from the project's change-chain docs), `PATCH` or `BATCH WITH` when they apply, and
> any step split as `STACK` only when this one ticket is too big for one PR.
>
> Read `docs/stacked-prs.md` in `<ai-sdlc>` before planning a stack. A stack is the steps of one
> ticket that build on each other, nothing else: never stack on another ticket's unmerged PR,
> that is a blocker.

## 3. Hand off the non-implementable, then implement

First the verdicts that need no code, right after triage:

- **SKIP**: `move <ID> in_review`; comment the PR link in one line unless the ticket already
  carries it. The PR is what gets reviewed; the ticket is out of the queue until it merges or
  someone moves it back.
- **BLOCKED BY `<other>`**: `./scripts/tracker.py block <ID> --by <other>` (the tracker's own
  dependency: a GitHub issue dependency, a Linear `blocks` relation), one comment line
  `Blocked by <other>: <why>` unless already said, then `move <ID> todo`. The queue
  (`list --unblocked`) hides it until `<other>` is closed. Never implement it, never stack on
  the blocker's branch. When the blocker is in this queue and is implemented now, the blocked
  ticket still waits for the next pass after that PR merges.
- IMPLEMENTED, VAGUE and UNCLEAR move as the brief says.

Then the SPECIFIC and DESIGNED tickets, grouped for the fewest PRs and review passes, in this
order of preference:

1. **PATCH** an open PR authored by `$ME`: the ticket's change lands as commits on that PR's head
   branch, the PR body gains its `Fixes` line and is re-described. No new PR.
2. **BATCH** tickets whose plans name each other: one implementation, one PR, one `Fixes` line
   per ticket. Each batched ticket is handled as one unit below (moves together, one comment each).
3. One PR per ticket, off the default branch.

A ticket (or batch) holds every repo it touches from implementation until it ships or is blocked.
Walk the units in priority order, start each whose repos are all free, and walk the list again
whenever one finishes: units in different repos run in parallel, units sharing a repo run one
after another. Tell each subagent other tickets run concurrently in other repos: never stop or
remove containers or services it didn't start, use a free port when a default is taken. Waiting
tickets stay In Progress (the runner claimed them). When a unit starts:

1. `./scripts/tracker.py move <ID> in_progress` for each of its tickets (a no-op when the runner
   already did it; needed when you fetched the queue yourself).
2. **Implementation subagent** (`model: "sonnet"` for SPECIFIC, `"opus"` for DESIGNED). Give
   it the identifiers, the triage plans, `branchName`, the PR it patches (url, head branch and
   head repository) if any, and the **Implementation rules**. It returns, per PR: repo, worktree
   path, branch, base, title, body, lint/test results, or `BLOCKED: <reason>`.
3. **Verification subagent** (fresh, `model: "opus"`). Give it the identifiers, the PR list, the
   **Review rules** section of `.claude/skills/review-prs/SKILL.md` (read and apply) and the
   **Ship rules**. It reviews each `git diff <base>...<branch>` against the tickets, reads changed
   files in full, re-runs lint/tests, fixes every real problem with new commits in the same
   worktree, ships, and returns PR URLs or `BLOCKED: <reason>`.
4. Shipped: comment the PR links (one line each) on every ticket of the unit, `move <ID>
   in_review` for each, remove the worktrees (`git -C <clone> worktree remove <path>`). BLOCKED:
   comment the reason, `move <ID> discussion`, keep the worktree for inspection.

### Implementation rules

- Work only in git worktrees, never in the clones (they hold the owner's uncommitted work):
  `git -C <clone> worktree add $REPOS_ROOT/.worktrees/<ticket-id>/<repo> -b <branch> <base>`. Base
  is `origin/<default branch>` (look it up: `git symbolic-ref refs/remotes/origin/HEAD`; never
  assume master or main), or the parent step's branch inside one ticket's stack.
- Branch: the ticket's `branchName` for the first PR in each repo (the first ticket's for a
  batch); extra stacked PRs get `<branchName>-<short-suffix>`.
- **Patching an open PR**: re-check it is open and authored by `$ME` (`gh pr view <url> --json
  state,author,headRefName,headRepository,headRepositoryOwner`); if merged, work off the default
  branch instead; if closed or someone else's, `BLOCKED: <reason>`. Fetch its head branch from its
  head repository, work in a worktree on that branch, commit on top; never rewrite its history.
- Stacking follows `docs/stacked-prs.md`: only the steps of one ticket. A single cohesive change
  is one PR; never split for its own sake, and never build on another ticket's unmerged PR.
- Follow the repo's `CLAUDE.md` for setup, conventions and commands, and the project docs in
  `$CONTEXT` for local-dev traps. Install dependencies in the worktree; run lint, typecheck and
  tests before committing. Add tests for new or changed logic, edge cases included, each one failing
  without the change (`docs/code-style.md` also sets comment and PR-body rules). Check a green run really ran: a silently missing
  dependency shows as zero collected tests, not an error.
- Keep the diff minimal and in the surrounding style: no unrelated churn, no speculative
  abstractions. Minimal means after tracing the flow: grep every caller of a function you change
  and fix the shared function once, rather than only the path the ticket names.
  `docs/code-style.md` ("Writing code") has the order to reach for reuse, stdlib and existing
  dependencies before new code.
- Commit, don't push. Messages as a human would write them, no co-author or tool trailers.
- If the spec is contradictory mid-implementation, or the work needs a secret, infra access or a
  human decision, stop and return `BLOCKED: <reason>` instead of guessing.

### Ship rules (for the verification subagent)

- Right before pushing, `git -C <worktree> fetch origin`. If the base moved, merge it into the
  branch (`git merge origin/<base>`, never rebase) and re-run the tests.
- `git -C <worktree> push origin <branch>`; a patched PR is pushed to its head repository and
  branch. If GitHub rejects a `.github/workflows/` change for a missing `workflow` scope, return
  `BLOCKED: needs gh auth refresh -h <host> -s workflow`.
- Open PRs bottom of the stack first: `gh pr create -R $ORG/<repo> --head <branch> --base <base
  branch> --title … --body …`. If a parent step merged meanwhile, base on the default branch
  (`docs/stacked-prs.md`). Never push to a branch you didn't create, except a PR you are patching.
- A patched PR gets no new PR: `gh pr edit <url> --body …` with the added `Fixes` line and a
  description of the whole change as it now stands, and the title updated when the scope grew.
- **Assign and request review.** The owner is the ticket's `assignee`, else its `creator` (both in
  `tracker.py get <ID> --json`; a `login` on GitHub, an email on Linear). For an email, find the
  login from their commits (`gh api "repos/$ORG/<repo>/commits?author=<email>&per_page=1" --jq
  '.[0].author.login'`), else match their name against `gh api orgs/$ORG/members --jq '.[].login'`
  and `gh api users/<login> --jq .name`. Then `gh pr edit <url> --add-assignee <login>` and request
  review from the owner (unless the owner authored the PR; then the ticket's creator, unless they
  authored it too). Also request the first suggested reviewer who is neither author nor owner:
  `gh api graphql -f query='{repository(owner:"<ORG>",name:"<repo>"){pullRequest(number:<n>){suggestedReviewers{reviewer{login}}}}}'`,
  then `gh pr edit <url> --add-reviewer <login>`. If a login can't be resolved, skip that step and
  say so in your return.
- Title follows the repo's `CLAUDE.md` convention and includes the ticket ID. Body: 150 words or
  fewer on what changed and why, then one `Fixes <ID>` line per ticket it closes (GitHub Issues:
  `Fixes #<n>`, or `Fixes <ORG>/<repo>#<n>` across repos); stacked or cross-repo PRs add
  `Depends on <PR url>`. Update the body when a later commit changes what it says. **No test
  plan section.**
- **Never mention AI, Claude or Anthropic** in commits, PR titles or bodies: no "Generated with"
  line, no co-author trailer.
- Never merge, never push to a default branch, never force-push a branch you didn't create in this
  run.

## 4. Wrap up

**Follow-up tickets.** For each real problem outside a ticket's scope (a bug, a data gap in another
repo, a missing piece another change depends on) create one: `./scripts/tracker.py create --state
todo --title "..." --body-file FILE`, plus `--repo <repo>` on GitHub or `--cycle current --label
"<repo label>"` on Linear (`tracker.py config label-for-repo <repo>`), plus `--label "$QUEUE"` when
the project has a queue label. Body: what was found (file paths), why it matters, a concrete fix,
and a line naming the source ticket. No follow-ups for nits; product or legal questions go in the
source ticket's comment.

Print one line per ticket, nothing else: `<ID> — <PR urls>`, `<ID> — patched <PR url>`, `<ID> —
QA: <where it's already done>`, `<ID> — Design: <what is missing or contradictory>`, `<ID> —
Requires Discussion: <why>`, `<ID> — In Review: <the open PR>`, `<ID> — blocked by <other>: <why>`
or `<ID> — blocked: <why>`.
