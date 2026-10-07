---
name: review-prs
description: Review the open PRs in the tracked repos that need your review — hunts hidden logic flaws, verifies the linked ticket is actually satisfied, checks the cross-repo chain and CI, then approves or requests changes on GitHub. Never merges. Use for "review my PRs", "what's awaiting my review", "review PR N in <repo>".
user-invocable: true
---

# Review PRs

Review the open PRs that need a review from you, then **land a verdict on GitHub**: approve
when clean, request changes when not. Never merge, never push to the branch, never change a
ticket's stage.

Setup, once: run from this folder (`ai-sdlc`) so the skill loads. When
`./scripts/tracker.py config projects` lists several and `SDLC_PROJECT` is unset, take the one the
user named (else each in turn) and `export SDLC_PROJECT=<key>` before the calls below.
`ORG=$(./scripts/tracker.py config org)`, `CONTEXT=$(./scripts/tracker.py config context)`; when
`./scripts/tracker.py config host` is not github.com, `export GH_HOST=<host>`. The tracked repos
are `./scripts/tracker.py config repos`. Resolve a repo's local clone with
`./scripts/repo-path.sh <repo>`; never hardcode a home path. The interactive shell may be zsh,
which does not word-split an unquoted `$var`: quote the split explicitly or run the snippet
through `bash -c`.

**Light review means** what a careful reviewer would actually block or ask about: high-confidence
findings only, roughly five per PR at most, ranked. Don't restate the diff, don't list nits lint
or CI already catches, don't redesign the author's approach.

## 0. Read-only in the working copies

- **Never `gh pr checkout`, `git checkout`, `git switch`, `git stash` or merge** in a clone. It
  may hold uncommitted work or untracked credential files. Anything that writes code belongs in
  a worktree (`docs/conventions.md`); review writes none.
- Get the code with `git fetch origin pull/<N>/head` and diff against the merge base; step 2 does
  this.
- **Never read a repo file by its working-copy path**: the clone sits on whatever branch its owner
  left, so content and line numbers come from the wrong commit. Read only the extracted PR head
  under `$OUT/tree/` (step 2), or `git show <head-sha>:<path>`.
- Don't run builds, tests, formatters or migrations. CI already ran; read its result.

## 1. Find the PRs

```bash
./scripts/awaiting-review.sh          # requested from you, re-reviews you owe, changes-requested PRs pushed since
./scripts/awaiting-review.sh --all    # + every open non-draft PR in the tracked repos you haven't reviewed
```

Use `--all` for a project where reviews are not requested per person (an open-source repo, say),
or for a wider net. Both modes only look at the tracked repos. A PR the user names in another
repo is in scope: review it, and say in the report that it fell outside the tracked set, since
the chain docs may not cover it.

- **Re-requests.** Submitting a review clears you from `reviewRequests`, so a PR back in the list
  was re-requested (`awaiting-review.sh` marks it `[re-review requested after your <VERDICT>]`).
  Compare your last review's `commit_id` with the head. New commits: check your earlier points
  were addressed and read the new code in full. Unchanged head: someone says the last pass missed
  something; re-derive findings from the diff and read every thread posted since. Re-approving an
  unchanged diff needs a reason: say what you re-checked.
- GitHub does not re-request after you request changes and the author pushes; the "pushed since
  your review" section catches those. They are re-reviews.
- A ticket-driven queue: `./scripts/tracker.py list --state in_review --json`. The tracker lags:
  an open PR is reviewable whatever stage its ticket is in, and the GitHub sweep is the authority.

**Skip** (one line at the end) a PR that is a draft, closed or merged, authored by you or a bot,
or already carries your approval and was not re-requested. If the user names a PR, go to step 2.

## 2. Gather context per PR

```bash
./scripts/pr-context.sh <repo> <number>
```

It writes metadata, the check rollup, ticket ids, stats and the full diff to a scratch folder;
run the `export OUT=...` on its last line. It extracts the PR head into `$OUT/tree/`; **every
`grep`, `sed` and `cat`, and every line number you cite, comes from there.** With no local clone
there is no tree: read single files with
`gh api -H 'Accept: application/vnd.github.raw' "repos/$ORG/<repo>/contents/<path>?ref=<head-sha>"`.

Read the diff **and the code around it**: a hidden flaw is usually outside the diff, in the
caller that now passes null, the sibling branch nobody updated, the cache that is now stale.

**Read the repo's own `CLAUDE.md` and `docs/`** (paths printed by `pr-context.sh`) before judging
anything repo-specific: they are the convention source of truth and they change. Also skim
`$CONTEXT` for the project's design and change-chain docs.

**Read what other reviewers said before writing anything:**

```bash
gh api repos/$ORG/<repo>/pulls/<N>/reviews  --jq '.[] | "\(.user.login) \(.state): \(.body)"'
gh api repos/$ORG/<repo>/pulls/<N>/comments --jq '.[] | "\(.user.login) \(.path):\(.line): \(.body)"'
```

A point already made is not yours to make again; add only what it misses, in one line. On a
**re-review**, check each of your earlier points was addressed **and re-derive whether it was
right**: a faithful implementation of bad advice is worse than the original code. Withdraw a wrong
point and say what it broke. When another reviewer already requested changes at the current head,
post a `COMMENT`, not a verdict (a blocker of your own that they missed gets a `REQUEST_CHANGES`
with only your findings).

## 3. Check the ticket

Take the ticket id from the PR title, branch or body (`pr-context.sh` extracts them; the id shape
is `./scripts/tracker.py config ticket-example`), then `./scripts/tracker.py get <ID> --comments`.

- **Does the diff do what the ticket asked?** Verify each hard requirement clause against the
  code, not against the PR description.
- **Scope drift**: work for another ticket, or requirements silently deferred. A deliberate,
  argued departure is not drift.
- Linear `attachments` list every PR of the ticket across repos: the authoritative map for step 4.
  On GitHub use `gh search prs <ID> --owner $ORG`. A related ticket for the same defect class is
  one line of context, not a redesign.
- A PR with no ticket is a **minor** finding: mention it in the body of a PR you were already
  blocking, or in the report. Never grounds to request changes.

## 4. Chained changes

Read the project's change chains in `$CONTEXT` (its cross-repo doc, if it has one) whenever the PR
touches a contract another repo consumes: an API, a schema, a message, a config field, a shared
type. Review a chained set in data-flow order. Those docs are a starting point: a PR can create a
consumer the docs don't list (infra that must provision a new topic, a config repo that must
reference a new field). Ask who reads or provisions for this change.

**Check the other leg exists**, for each repo the change should reach:

- `gh search prs --owner $ORG --repo <consumer-repo> <ticket-id-or-keyword>` for a companion PR,
  open or merged; the ticket's attachments and relations.
- Nothing found **is** the finding: name the repo and what it must do as
  `Follow-up ticket: <proposed title>`.

**Has the project already solved this?** Grep sibling repos for the same construct (a workflow
step, a build shape, a retry policy) and read the comment beside it before accepting a novel one.

Raise: a missing leg, an untracked consumer, a pin pointing at an unmerged commit,
**merge-order hazards** (a dependency bump merging before what it depends on), shape mismatches
(nullable vs required, float read as int). **A new enum-like value is a chain change even when no
schema moves**: grep every consumer for the value's siblings; the `switch` lacking the new case
is the finding. Do this unprompted.

## 5. What to look for

Hidden logic flaws first, then conventions, then design. Repo-specific traps live in each repo's
docs (step 2), not here.

### Correctness

- Error paths and partial failure: swallowed exceptions, a `try` that turns a hard failure into
  an empty result, retries without bounds, unbounded caches or queries.
- **Parity claims.** When a PR says a path works "like" an existing one, diff the two on their
  failure branches — a 4xx, a timeout mid-sequence — not the happy path. Typical miss: the new
  poster stops at the first rejected call where its sibling logged and continued.
- **A narrower `except` than its siblings.** Read the library's exception tree for what escapes,
  and check whether the calling loop has its own `try` — if it doesn't, one bad item aborts the
  whole fan-out.
- **Nullability narrowed on a hot path**: a `.get()` or `or` fallback replaced by direct
  indexing or attribute access on a value that was allowed to be null. Find what handled the
  null before, and whether the new exception lands outside the per-item `try`.
- Off-by-one and boundary conditions on time windows, pagination, watermarks.
- Concurrency: shared mutable state across tasks, non-atomic read-modify-write, parallel
  branches writing the same key without a merge rule.
- Secrets and PII in logs, events, fixtures, and exception strings that reach a log or a client.

### Verification

- **Green CI is not verification.** Know which suites a repo's default job excludes, and check
  whether the changed path is covered by one of them. A test that only hit a connectivity
  `skip` did not run, and a live test must never be committed with its skip marker inactive.
- A PR that claims verification but ran only the fast suite, when the change lives in the path
  the slow suite covers.
- Tests that assert the mock rather than the behaviour; a bug fix with no regression test.
- Non-trivial new or changed logic with no tests, or only happy-path ones: name the edge case
  that's missing (empty or null input, a boundary, partial failure).
- A missing test for a non-obvious contract is worth more than a tautological one is worth
  removing.
- Comments narrating the PR's own draft history ("an earlier version…") — not allowed.
- Anything else `docs/code-style.md` rules out: long comments, a PR body that no longer matches
  the head commit, and compatibility code between repos that ship together (the project's
  context docs say which).

### Complexity

What the diff could drop, against `docs/code-style.md` ("Writing code"). One line each, inside
the ≤5 cap, with what replaces it:

- `delete:` dead code, unused flexibility, a speculative feature. Nothing replaces it.
- `reuse:` re-implements a helper the repo already has. Name it.
- `stdlib:` / `native:` hand-rolls what the standard library or platform ships, or adds a
  dependency for it. Name the function or feature.
- `yagni:` an interface with one implementation, config nobody sets, a layer with one caller.
- `shrink:` same logic, fewer lines. Show the shorter form.

e.g. `repo.py:88: yagni: AbstractRepository with one implementation. Inline it.` Never flag
validation, error handling or a regression test as bloat.

## 6. CI

`pr-context.sh` prints the rollup. For every non-`SUCCESS` check say which job and why:

```bash
gh pr checks <N> --repo $ORG/<repo>
gh run view <run-id> --repo $ORG/<repo> --log-failed | tail -60
```

**First establish the job ran.** A job that never reached a runner reports as failed with
`log not found`; the tell is `steps: []` and a 2-3 second duration, and the reason is in the
check-run annotation (`gh api repos/$ORG/<repo>/check-runs/<job-id>/annotations --jq '.[].message'`).
Report that as infrastructure, not the PR's fault, and hold no verdict on it. Diagnosing a failure
the job never had sends the author after something that isn't broken. Also report
`mergeable: CONFLICTING`, a stale base and someone else's `CHANGES_REQUESTED`. CI is
informational: never a reason by itself to request changes; never re-run or "fix" it.

## 7. Review rules, report, verdict

Write the findings out in full **before** composing the payload: per PR one verdict line, then
ranked findings as `repo/path.ext:line` + what breaks + why, the ticket check, chain status and
follow-up tickets. A finding that won't survive being written plainly isn't ready to post. Then
post; invoking this skill is the authorization.

```bash
gh pr review <N> --repo $ORG/<repo> --approve --body "<short note, or omit>"    # clean

cat > review.json <<'JSON'                                                     # findings
{"event":"REQUEST_CHANGES","commit_id":"<head sha>","body":"<1-3 sentences>","comments":[
  {"path":"src/foo.py","line":42,"side":"RIGHT","body":"..."}]}
JSON
gh api repos/$ORG/<repo>/pulls/<N>/reviews --input review.json
```

- Pin to the head SHA with `commit_id`; re-check `gh pr view <N> --json headRefOid` right before
  submitting and stop if it moved. `line` must be a diff line on that side or the API 422s
  (otherwise comment on the nearest changed line). Verify every cited `path:line` against
  `$OUT/tree` first.
- **The verdict is decisive**; nobody looks again unless you request changes and the author
  pushes. `REQUEST_CHANGES` for what must be fixed before merge: a confirmed bug or logic flaw,
  data loss or a security hole, a break of another repo or an existing contract, a mismatch with
  the ticket or PR description, a claim the code rests on that research shows is wrong, or a
  missed change that materially affects correctness, cost or performance. Put every point in that
  one review. Otherwise `APPROVE`, with take-or-leave feedback as inline comments.
- A correctness finding needing an unlikely trigger goes on the approval as an inline comment
  naming the trigger.
- `COMMENT` is not a verdict: use it for a genuinely blocking question, and when another
  reviewer's changes-requested is open at this head.
- Cosmetics never count (style, naming, formatting, "I'd have done it differently"); drop them.
- **Verify claims** the PR relies on (library behaviour, vendor limit, API contract) with a source
  before accepting or disputing them. Another reviewer's verdict doesn't replace yours: if you
  reach the same blocking conclusion and it's unfixed, request changes in one line pointing at
  their finding.
- **Default to this PR.** A related finding small enough to fold in (same repo, hours not days;
  the docs page or test this change should have touched) is a `REQUEST_CHANGES` point, not a
  ticket. Only work that is **big or unrelated** (a leg in another repo, a bug it exposes but
  didn't introduce, something needing its own design) is a follow-up ticket: one line in the
  body, `Follow-up ticket: <title>`, collected in the report. Suggest, don't create; when asked, file it as `review-prs.sh` does:
  `./scripts/tracker.py create --state todo --title ... --body-file F` plus `--repo <repo>` on
  GitHub or `--cycle current --label "<repo label>"` on Linear (`--state triage` when unsure it is
  real), plus `--label <blocker>` from `tracker.py config blocker-label` when it is non-empty and
  the follow-up must land before the next release.
- One review per PR. Say in chat which PRs you approved and which you blocked, with URLs.

### Writing every comment

The reader is the author, skimming between other things. Correct but unreadable comments don't
get acted on.

- **Three parts, blank line between**: what breaks (one short sentence); why (one or two
  sentences); the fix (a ```` ```suggestion ```` block when it sits on the commented lines,
  matching indentation, else a short snippet or pointer).
- Short sentences, one idea each. Plain words first, identifier after; backtick two names per
  sentence at most. One finding per inline comment; a point about another repo goes in the body.
- Name what breaks, not what you checked; consumers that turned out fine go in the chat report.
  An approval body is one or two sentences carrying what inline comments can't (a merge-order
  note, a withdrawn point), or nothing; it never summarises the PR.
- Budget: inline at most six short lines before the fix; body three sentences for changes
  requested, two for an approval.
- No "forever", "never", "always" unless you read the path that bounds it. No bold headers inside
  a comment. Direct, specific, no preamble or praise; ask a question when unsure.
- **Never mention AI, Claude or Anthropic** in any review text or ticket.
