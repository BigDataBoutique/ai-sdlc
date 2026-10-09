---
name: qa-cycle
description: Work through the tickets sitting in the QA stage (current cycle on Linear), 10 at a time — pull a batch, check each fix is merged and deployed, give the user concrete steps (or run the data check) on the right environment, and close on their say-so. Also runs the project's prod-rollout watchlist after a release, when its context docs define one. Use for "next QA batch", "QA tickets", "validate in staging", "check the watchlist", "did the release land".
user-invocable: true
argument-hint: "[watchlist [release]]"
---

# QA cycle

Two modes:

- **Batch** (default): validate and close QA tickets.
- **Watchlist** (`/qa-cycle watchlist [release]`, or after a production release): only when the
  project's QA doc defines one (see below); re-check what was waiting on that release.

The tracker is the only state. A ticket still in QA hasn't been validated. Nothing is tracked
locally.

## Project knowledge

When `./scripts/tracker.py config projects` lists several and `SDLC_PROJECT` is unset, ask which
one and `export SDLC_PROJECT=<key>` first. Everything project-specific comes from the context folder, `CONTEXT=$(./scripts/tracker.py config
context)`: read `$CONTEXT/qa.md` first. It should say where each service's deployed version is
read (manifests, registry, release workflow), which environment suits which kind of change, how
to reach it and what test data or users exist, plus the rollout watchlist if any. **If there is
no `qa.md`, or it lacks something you need, ask the user; don't guess.** Also read `$CONTEXT`'s
other docs when a check needs them. `ORG=$(./scripts/tracker.py config org)`; when `config host` is
not github.com, `export GH_HOST=<host>`. Clones: `./scripts/repo-path.sh <repo>`.

## Rules

- **Never close, reopen or move a ticket without the user saying so.** Propose; they decide.
- Read and change tickets through `./scripts/tracker.py` (`howto`). It can't edit a line in place
  or create a sub-ticket; on Linear use the MCP `save_issue` for those.
- Read-only everywhere: data stores and dashboards through the project's data skills, `gh` for
  PRs, `git fetch` + `git show origin/...` in the clones. No checkouts.
- Tracker text is written as the user would write it. No mention of Claude or AI.
- Anything new you find that isn't the ticket's bug gets its own ticket (ask who owns it), not a
  reason to hold the current one: `tracker.py create` (see `howto`; `--state triage` when unsure).

## 1. Pull a batch

1. `./scripts/tracker.py list --state qa --json`, plus `--cycle current` on Linear. Add
   `${QUEUE:+--label "$QUEUE"}` only if the user wants just the automated queue.
2. It sorts by priority, then `createdAt`; re-sort ties by oldest `updatedAt`. Take 10.
3. Per ticket: `tracker.py get <ID> --json --comments` (description, PR links). For
   screenshot-only descriptions on Linear, call the MCP `extract_images` with URLs from a
   **fresh** `get_issue`: signed URLs expire within minutes.
4. PR state: `gh pr view N -R $ORG/<repo> --json state,mergedAt,mergeCommit`. Closed-unmerged PRs
   linked alongside are usually superseded: say so and ignore them.

Present one table: ticket, what to check, where, what "pass" looks like. Flag the ones that aren't
closeable (unmerged PR, unticked tasks in the description, pending customer steps).

## 2. Is it deployed?

Find the deployed version the way `qa.md` says, always from fresh remote state (`git fetch`, then
`origin/HEAD`, never a working tree). Then:

```bash
gh api repos/$ORG/<repo>/compare/<merge_sha>...<deployed_sha> -q .status
# "ahead" or "identical" = the fix is in the deployed build; "behind" or "diverged" = it is not
```

- A merged fix can still be missing: a later squash from a stale branch can revert it. Before
  closing, check the fix's added lines are still on the default branch, not only that the merge
  commit is an ancestor of the deployed build.
- **When did it reach the environment?** Don't assume the current build was the first to carry
  the fix: walk the deploy history to the first deploy that includes it. That date is the split
  point for any before/after data check; the wrong one puts "after" data in the "before" bucket.
- Read a deployed tag at a given commit with `git show <commit>:<file>`; commit subjects don't
  always carry the sha, and not every commit touching the file is a deploy.

## 3. Where and how to test

Pick the environment from `qa.md`. Rule of thumb: a change that only reads data goes where the
realistic data is; anything with side effects (emails, tickets, billed calls) goes where a test
artifact is harmless. Before handing out a URL, confirm the environment actually has the data the
feature reads, and use real values from it in the "valid input" case, so a pass shows rows rather
than an empty list.

- **UI:** numbered steps plus a pass line. For URL-parameter bugs pre-encode the payloads; for
  storage bugs give a console snippet that backs up, corrupts and restores. Warn before anything
  that fires `alert()`.
- **Security (XSS/SSRF):** payloads call back to a webhook URL; pass is zero requests, including
  when a second user opens the item.
- **API:** run the request yourself and report status and body for both the bad and the valid
  input. Confirm the reason for a generic validation error in the service logs. Use a non-admin
  user for permission checks (403s, cross-org, admin-only fields) as `qa.md` describes; if it
  needs a credential, ask the user for a short-lived one.
- **Data checks:** show before/after counts, not "looks fine". Say what the check can't see.

Shell traps: an interactive zsh does not word-split `set -- $pair`, so a looped `curl` sends one
mangled field: wrap the loop in `bash -c`. A `grep`/`xargs` over an empty file list reads stdin
and hangs; give `curl` a `-m` timeout.

## 4. Close

On the user's OK: Linear, `tracker.py move <ID> Done`; GitHub Issues, `gh issue close <n> -R
$ORG/<repo>`. Then:

- **Fix not yet in production** → *before* closing, record it where `qa.md` says (the watchlist,
  else a comment on the ticket): the ticket, the commits that must ship (and which must ship
  **together**), the current production build ids, and the exact check to run including baseline
  numbers. Append with `tracker.py append <ID> --body-file F`, never a full description rewrite.
- Fix in production → nothing else.

Keep a running tally in chat: closed / skipped / added to the watchlist / new tickets filed.

## 5. Watchlist mode

Only when `qa.md` defines a watchlist: its trigger table says which item to re-check after which
release.

1. Pick the item(s) for what shipped: the one the user names, or, for "did the release land",
   each whose trigger fired since its "as of" date. Items whose Ship line needs several repos stay
   pending until all are in production.
2. For each unticked item, compare its commits against the current production build (section 2).
3. Not deployed → report "still waiting" with the production build id.
4. Deployed → find the rollout date, run the check written in the item against production and
   report the numbers. On the user's OK, tick the item (Linear MCP `save_issue` with `patch`,
   `- [ ]` → `- [x]` on that line) and add the rollout id and result to the line.
5. Every item in a group ticked → propose closing it and updating the index; every group closed →
   propose closing the index.

A production release is whatever `qa.md` says it is. Run this mode after one lands, or when asked.
