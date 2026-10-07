# Scripts

The framework's tooling, one level below the runners in the `ai-sdlc` root, plus the helpers
behind PR review. Everything project-specific (data-store queries, cloud login, evals) lives in
the project's context folder, `contexts/<project>/scripts/`, and is documented there.

| | scripts | what they do |
|---|---|---|
| **config** | `env.sh` | Sourced by every runner: reads `projects.yaml` once and sets the paths and forge settings they share. With several projects and no `SDLC_PROJECT`, reruns the runner once per project. |
| **logs** | `prune-logs.sh` | Run by `env.sh`, at most once a day: deletes per-run logs older than `LOG_RETENTION_DAYS` (14) and trims `logs/*.log` to `LOG_KEEP_LINES` (20000). `--force` skips the daily check. |
| **tracker** | `tracker.py` (+ `test_tracker.py`) | The ticket tracker CLI behind every runner and skill: list a queue, read, move, label, assign, comment, create, on Linear (API key) or GitHub Issues (`gh`), per `../projects.yaml`. |
| **review** | `repo-path.sh`, `awaiting-review.sh`, `pr-context.sh` | The checkout resolver, the review queue, and the per-PR context gatherer. They back the `review-prs` skill and `../review-prs.sh`. The repo list they sweep is `../projects.yaml`. |

The headless runners (`../review-prs.sh`, `../update-prs.sh`, `../design-tickets.sh`,
`../implement-tickets.sh`, `../reset-stale-tickets.sh`, `../pull-all.sh`) live in the root, next
to `CLAUDE.md`: each derives `ROOT` from its own location and sources `scripts/env.sh`.

## `env.sh`

Sourced after `ROOT` is set. It runs `tracker.py config env` once and exports what a runner needs, so
no script names a project, an org or a host:

| var | meaning |
|---|---|
| `TRACKER` | `$ROOT/scripts/tracker.py` |
| `PROJECT` | the project key, also exported as `SDLC_PROJECT` so sessions and helpers stay on it; `state_file <runner>` gives its cache path |
| `REPOS_ROOT` | where the clones live; default the parent of `ROOT`, exported. Set it when running from a worktree of ai-sdlc (the worktree's parent is `.worktrees/<id>/`) |
| `NAME`, `ORG`, `HOST` | project name, forge org, forge host (`github.com` unless `forge.host` says otherwise; for another host `GH_HOST` is exported so `gh` follows) |
| `CONTEXT` | the project's context folder (`context:` in `projects.yaml`, default `contexts/<key>`) |
| `KIND` | `linear` or `github` |
| `QUEUE`, `QUEUE_ARGS` | the queue label, empty when the project has none; `QUEUE_ARGS` is `--label $QUEUE` or nothing |
| `BLOCKER` | the release-blocker label for follow-up tickets, may be empty |
| `EXAMPLE` | a sample ticket id (`ABC-123`, `repo#123`) for prompts |
| `CLONE_BASE` | `https://<host>/<org>`, where `<repo>.git` is fetched from |
| `MCP_TOOLS` | `mcp__linear-server,` for a Linear project, else empty |

## `tracker.py`

One command line over the project's ticket tracker, so the runners and skills never need a
Claude turn (or a hardcoded UUID) to list a queue or move a ticket. The backend, team/repos,
queue label and stage names come from `../projects.yaml` (`projects.json` with the same shape
works too), or from the files in `--config` / `SDLC_CONFIG`, merged by project key (a key with two
different blocks, or a repo in two projects, is an error); names are resolved to IDs at runtime. Stdlib only; PyYAML is used when installed,
otherwise a built-in parser handles the block subset the file uses.

```bash
./tracker.py me                                        # who the tracker thinks you are
./tracker.py list --state todo --label "<queue label>" # a queue, priority first, oldest first
./tracker.py list --state todo --cycle current         # Linear: only the team's active cycle (ignored on GitHub)
./tracker.py list --open --since 30 --json             # every open ticket updated in 30 days
./tracker.py list --state in_progress --stage-since --json   # + stageSince: when each last entered its stage (reset-stale-tickets.sh)
./tracker.py list --state todo --unblocked --not-mine  # without tickets that have an open blocker, or that I created
./tracker.py get <ID> --comments                       # description, labels, repos, relations, blockers, comments
./tracker.py move <ID> in_review                       # a stage key (triage|design|todo|in_progress|in_review|qa|discussion) or a state name
./tracker.py block <ID> --by <OTHER>                   # OTHER must close first: a GitHub issue dependency, a Linear `blocks` relation
./tracker.py label <ID> --add "<label>" --remove "<label>"
./tracker.py assign <ID> --me
./tracker.py comment <ID> --body-file note.md
./tracker.py create --state todo --label "<label>" --title "..." --body-file body.md   # --repo R on GitHub
./tracker.py create --state todo --cycle current ...   # Linear: in the team's active cycle (ignored on GitHub)
./tracker.py append <ID> --body-file design.md         # appends to the description, verifies the original survived
./tracker.py setup-labels                              # GitHub: create the queue and stage labels in every repo
./tracker.py howto                                     # the paragraph the runners paste into prompts
```

`config <what>` prints one setting, for scripts (`--json` for the JSON form):

| `config …` | prints |
|---|---|
| `projects` | every project key in the config, one per line |
| `name` | display name (`name:`, default the project key) |
| `org`, `host` | forge org, forge host |
| `context` | absolute path of the context folder |
| `kind` | `linear` or `github` |
| `queue-label` | the queue label; empty when there is none |
| `stages` | `stage<TAB>state name` per line |
| `repos` | tracked repo names, one per line |
| `ticket-regex`, `ticket-example` | how a ticket reference looks (`\bABC-[0-9]+\b` / `ABC-123`; `((repo)#[0-9]+\|#[0-9]+)` / `repo#123`) |
| `design-labels` | `tracker.design_labels`, one `Group: a, b` per line |
| `blocker-label` | `tracker.blocker_label`, empty when unset |
| `clone-url R` | `https://<host>/<org>/R.git` |
| `repo-for-label L`, `label-for-repo R` | the repo↔label map; exit 1 when there is none |
| `env` | the `KEY=value` lines `env.sh` evals |

- **Linear**: GraphQL with a personal API key, `LINEAR_API_KEY` in `../.env` (see
  `.env.example`). Identifiers are `<KEY>-123`. `label` uses `addedLabelIds`/`removedLabelIds`,
  so other labels are untouched.
- **GitHub Issues**: `gh api`, no extra credential (`GH_HOST` for a non-default host). Issues live
  in each repo in `projects.yaml` (`issues: false` excludes one); identifiers are `<repo>#N` (or
  the issue URL); `create` needs `--repo` when there are several.
  - A stage is a label (`tracker.stage_labels`, defaults `stage:todo`, `stage:in-progress`, …). A
    stage mapped to `null` is the **unlabeled** one: an open issue carrying none of the stage
    labels (`todo: null` makes "no triage label" the ready queue). At most one stage may be
    unlabeled; two stages may share a label (`triage` and `design` both `triage`).
  - `tracker.exclude_labels` lists labels whose issues `list` never returns.
  - `queue_label` is optional; with none, the stage alone is the queue.
  - `setup-labels` creates the queue and stage labels the config names, in every repo, once.
- **Other `tracker:` keys**: `key` (ticket prefix), `states` (stage → Linear state name),
  `design_labels` (`{group: [labels]}` the design pass may add besides the repo label),
  `blocker_label` (put on follow-ups that must land before a release). Top level: `name`,
  `context`, `forge: {kind, org, host}`. See `../docs/new-project.md` for full examples.
- `--json` prints the normalized ticket (`identifier`, `title`, `state`, `stage`, `labels`,
  `repos`, `priority`, `branchName`, `assignee`, `creator`, `description`, …), the same shape
  on both trackers, which is what the runners consume. `get` (and `list --unblocked`) adds
  `blockedBy`: `[{identifier, title, open}]` from the tracker's dependencies; on GitHub the
  dependencies call is made only when the issue's dependency summary says there are any.
- On GitHub, no queue but `in_progress` lists an issue carrying the in-progress label, whatever
  other stage label it has: a running pass holds it.
- `DRY_RUN=1` (the same variable the runners honour) prints `would: …` for every write and
  writes nothing. Exit 2 = usage, 1 = the tracker call failed. Tests:
  `python3 -m unittest scripts/test_tracker.py`.

## Review helpers

- **`repo-path.sh <repo>`**: prints the local checkout for a repo, resolved by its git remote
  (org and host from `projects.yaml`). Folder names don't have to match repo names, and a
  same-named clone of another org or host won't be mistaken for it. Exit 1 and no output when
  there is no clone; callers fall back to reading the PR through `gh`. `--list` dumps every pair
  found, the quickest way to see how a machine is laid out. Looks in `$REPOS_ROOT`.
- **`awaiting-review.sh [--all]`**: the review queue: PRs requested from you, plus the
  re-reviews you owe (GitHub never re-requests those, so they appear nowhere else). `--all`
  widens it to every open PR in the swept repos you haven't reviewed.
- **`pr-context.sh <repo> <number> [outdir]`**: everything needed to review one PR: metadata,
  the check rollup, ticket ids, the diff against the merge base, and a read-only copy of the
  repo at the PR's head under `<outdir>/tree`. It only ever fetches, never checks out, so it is
  safe to run against a checkout holding someone's uncommitted work.

## Runners: shared behaviour

`review-prs.sh`, `update-prs.sh`, `design-tickets.sh`, `implement-tickets.sh`,
`reset-stale-tickets.sh` and `pull-all.sh`:

- `DRY_RUN=1` lists decisions without spawning sessions or touching caches or the tracker;
  `INTERVAL=<sec>` loops instead of one pass; `REPOS` overrides the tracked repo list
  (`review-prs.sh`, `update-prs.sh`, `pull-all.sh`).
- The session runners take `MODEL` and `CLAUDE_FLAGS` (a shell word list, eval-split, to override
  the permission policy: cron can't answer prompts); `design-tickets.sh` also `FALLBACK_MODEL`.
  Models are aliases (`sonnet`, `opus`, `fable`, `haiku`) so each run gets the latest of a family,
  and sessions pass `--fallback-model opus,fable` (covers an overloaded or unavailable model,
  subagents included; not a usage limit).
- `design-tickets.sh`, `implement-tickets.sh` and `triage-issues.sh` run their session through
  `scripts/claude_run.py`: it waits for background subagents (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`),
  streams events to `<log>.jsonl` as a heartbeat, and when that goes quiet for `STALL_MIN` minutes
  (30) kills the session and resumes it once with a nudge. A second stall exits 124. The session
  runs in its own process group, so `claude_run.py` forwards SIGTERM/SIGINT to it: killing a
  runner (from `sdlc.py`, or Ctrl-C) never leaves a session running on by itself.
- A mkdir lock (`logs/<runner>/.lock.d`) keeps passes from overlapping: stale after 6h, except
  that `implement-tickets.sh` writes its pid into the lock (`lock_live` in `env.sh`) and is only
  taken over once that pid is dead, however long the pass runs. macOS has no `flock`; the scripts
  also avoid GNU-only `date -Is`/`date -d`, so they run on Linux and macOS.
- Sessions load only project and local settings, with auto-memory off; the Linear MCP is declared
  in `.mcp.json`, passed with `--mcp-config` only when the file exists. See `../docs/conventions.md`.
- Caches (`.<runner>-cache.<project>.json`) stop a failed item burning tokens every pass; delete the file to retry.

What each one takes:

| runner | works on | notes |
|---|---|---|
| `design-tickets.sh` | Design-stage tickets (with the queue label when the project has one; current cycle on Linear, `--all-cycles` for all) | Appends a design section ending in implementation instructions. Hand-off: with a queue label it drops the label and leaves the stage to a human; with none it moves the ticket to Todo |
| `implement-tickets.sh` | Todo tickets with no open blocker (`list --unblocked`), in priority order (same queue rules) | Moves every queued ticket to In Progress first (the claim no other pass crosses), then one `claude -p` session running `/implement-tickets <ids>`; whatever is still In Progress when the session ends goes back to Todo with a comment. On GitHub Todo may be "no stage label" |
| `reset-stale-tickets.sh` | In Progress tickets (with the queue label if any) older than `STALE_HOURS` (6) in that stage | No session; comments and moves them back to Todo. Does nothing while an implement pass is running (its lock's pid is alive): that pass releases its own tickets |
| `review-prs.sh` | open non-draft PRs by others (not bots) in the tracked repos | Skips a PR that already has an approval or changes-requested on its current head |
| `update-prs.sh` | open PRs authored by me in the tracked repos with a merge conflict with the base branch or changes requested | Fixes or answers every point, merges the base into the head; the script squashes, force-pushes with a lease and re-requests review; `CREATED_WITHIN` (30 days) |
| `pull-all.sh` | fetches every tracked repo; fast-forwards ai-sdlc itself when clean and on the default branch | Never touches a checkout's working tree |

`triage-issues.sh` (Triage-stage issues opened by someone else: validate, comment, label one
`tracker.triage` outcome group, move on; issues the tracker user opened wait for a human, and a
PR the same user authored is never reviewed) completes the set. Each built-in skips a project whose config lacks its stage or
settings (`need_task` in `env.sh`, from `tracker.py config tasks`). `sdlc.py` gives each project a
row per task it turns on, plus its own `tasks:` from `projects.yaml`, each waiting `every` between passes.


## `review-prs.sh`

**Lives at the repo root (`../review-prs.sh`), not in this folder**: it derives `ROOT` from
its own location and `REPOS_ROOT` from its parent, so it must sit beside `CLAUDE.md` with the
project's repos as siblings. Run it as `./review-prs.sh` from the `ai-sdlc` root.

An unattended PR-review runner. One pass looks at every open, non-draft PR in the tracked
repos that still needs a review from you, and spawns one
headless `claude -p` session per PR in a detached worktree at that PR's head, under
`$REPOS_ROOT/.worktrees/review-prs/<repo>`.

The worktree is what keeps a review pass out of the way of whatever else you are doing. Reviews
used to run with `cwd` set to the repo checkout itself, so a review and a session implementing
in that same checkout shared one working tree. There is one worktree per repo, re-pointed at
each PR head rather than recreated, so the cost is the files that actually differ between two
heads and not a fresh checkout per PR. It is detached for the same reason `update-prs.sh` is:
a worktree holding a branch stops anyone else checking that branch out. Override the location
with `WORKTREES`. A PR from a fork is fetched through `refs/pull/<n>/head` first, since its
head sits on no `origin/*` branch.

GitHub is the only source of PRs, with no running Claude session; a ticket named by the PR is read
by the review session through `tracker.py`.

**Skip rules**, applied in order:
1. Authored by you, or by a bot (CI image bumps, dependabot).
2. A verdict (`APPROVED` or `CHANGES_REQUESTED`, by any human reviewer, you or anyone else)
   already sits on the current head commit: a review is of a commit, and nothing new has landed
   since. Commits pushed after the last verdict make the PR eligible again, as a re-review when
   the earlier verdict was yours; a re-request on an unchanged head does not. `COMMENTED`,
   `DISMISSED` and your own unsubmitted `PENDING` draft are not verdicts.
3. A session already failed on this exact head commit (per the cache) — so a broken PR doesn't
   burn tokens every cycle. Delete its entry in `.review-prs-cache.<project>.json` to retry.

Running out of credits mid-pass is treated as transient: the pass aborts without caching a
failure.

```bash
DRY_RUN=1 ./review-prs.sh        # print decisions, spawn nothing, don't touch the cache
./review-prs.sh                  # one pass (cron mode)
INTERVAL=900 ./review-prs.sh     # loop, a pass every 15 min (tmux/systemd)
```

Cron:

```cron
# adjust the path to wherever you cloned ai-sdlc
3,18,33,48 * * * * /path/to/ai-sdlc/review-prs.sh >> /tmp/review-prs.log 2>&1
```

**Env:**

| var | default | meaning |
|---|---|---|
| `REPOS` | the repos in `projects.yaml` | Repo allowlist; `all` = whole org. |
| `MODEL` | `opus` | Model for the review sessions. |
| `INTERVAL` | unset | Seconds between passes; unset = single pass. |
| `UPDATED_WITHIN` | `30` | Only PRs updated in the last N days; `0` = no limit. |
| `CLAUDE_FLAGS` | allows `gh`/`git`/read tools, denies `gh pr merge` and `git push` | Permission policy — cron can't answer prompts. Eval-split as a shell word list, so quote args containing spaces. |
| `LOGDIR` | `<ai-sdlc>/logs/<project>/review-prs` | Per-PR session logs, plus the `.lock.d` lock directory that prevents overlapping passes. Gitignored. |
| `DRY_RUN` | unset | `1` = list decisions only. |

The review prompt is built inline in `review_prompt()`; follow-up tickets are filed the way the tracker wants (`--cycle current` and the repo label on Linear, `--repo` on GitHub, the blocker label when the project sets one). Its rules are deliberately strict: the
verdict is decisive (approve, or request changes with *every* point), style nits are never
grounds for requesting changes, a related finding small enough to fold in is asked for on the PR
and only big or unrelated work gets a follow-up ticket, CI is informational, and the session re-checks `headRefOid`
before submitting so it never reviews a stale commit. It also refuses to duplicate work:
a re-requested pass on a head I have already reviewed submits nothing unless it finds
something new, and points another reviewer already made are never repeated — though their
verdict never stands in for mine, so an approvable PR still gets approved.

### Caveats before running this one

- **Runs on Linux and macOS.** Every script here avoids `flock` (not on stock macOS) in
  favour of an atomic `mkdir` lock, and avoids GNU-only `date -Is`/`date -d`. Date
  arithmetic that has no common spelling tries both, BSD first: see `days_ago()` in
  `review-prs.sh` and `iso_utc()` in a project's own task scripts. Keep new scripts to that rule,
  or a pass silently exits as "previous run still going" on one of the two platforms.
- **`ROOT` is the script's own directory** and `REPOS_ROOT` its parent, so the repo checkouts
  must be siblings of `ai-sdlc`. That is the layout `CLAUDE.md` describes, and nothing is
  hardcoded to a particular developer's home, but if you clone `ai-sdlc` somewhere away from the
  other repos, the fetch loop finds nothing and every session falls back to `cwd=$ROOT`. Running
  a runner from a *worktree* of `ai-sdlc` does the same, since the worktree's parent is
  `.worktrees/<id>/`: set `REPOS_ROOT` to the real repo root, which `repo-path.sh` honours too.
