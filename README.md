# ai-sdlc

> Headless [Claude Code](https://docs.claude.com/en/docs/claude-code/setup) runners that triage,
> design and implement tickets, review PRs and fix review feedback across your repos. They never
> merge and never push to a default branch.

ai-sdlc is a hub: scripts, skills and docs that drive an AI-assisted software lifecycle over a
project's repos. It works on any project whose code lives in one or more GitHub repos and whose
tickets live in Linear or GitHub Issues. Nothing in the scripts or generic skills names a
project: everything project-specific sits in three places, a config file (`projects.yaml`), a
`.env` and one context folder (`contexts/<key>/`).

**Contents:** [The problem](#the-problem) · [How it fits together](#how-it-fits-together) ·
[Ticket flow](#ticket-flow) · [Prerequisites](#prerequisites) · [Quickstart](#quickstart) ·
[Configuration](#configuration) · [Running it](#running-it) · [Safety](#safety) ·
[Conventions](#conventions) · [Design notes](#design-notes)

## The problem

An agent can write a decent PR for a well-specified ticket. Running that every day on a real
team, across several repos that depend on each other, is the hard part:

- tickets are vague, already done, already being done in an open PR, or need a human decision;
- the agent must not touch the checkouts people are working in, merge anything or push to a
  default branch;
- a PR in one repo often needs a matching change in another;
- reviews, review fixes and merge conflicts pile up on the PRs it opened;
- sessions die, stall or loop, and a scheduler has to notice.

ai-sdlc is the glue for that: a ticket tracker CLI, a set of single-purpose runners, a scheduler,
the skills the sessions run, and the written conventions they follow.

## How it fits together

```
ai-sdlc/                     this repo, the hub; all sessions start here
├── projects.yaml            your config, gitignored; copy projects.example.yaml to start
├── .env                     secrets, gitignored (LINEAR_API_KEY for Linear projects)
├── contexts/<project>/      what an agent should know about the product
├── sdlc.py                  terminal scheduler for the runners below
├── pull-all.sh              fetch every tracked repo, self-update this folder
├── design-tickets.sh        Design stage  -> design section + implementation instructions
├── implement-tickets.sh     Todo stage    -> triage, then PRs
├── reset-stale-tickets.sh   In Progress for too long -> back to Todo
├── triage-issues.sh         Triage stage  -> validated, commented, labeled, handed on
├── review-prs.sh            others' open PRs -> a review on GitHub
├── update-prs.sh            your PRs with conflicts or changes requested -> fixed, re-requested
├── scripts/                 tracker.py, env.sh, claude_run.py, review helpers
├── .claude/skills/          review-prs, implement-tickets (a project adds its own)
└── docs/                    conventions, code style, stacked PRs, setup, new project
../<repo>                    the project's repos, cloned as siblings of this folder
../.worktrees/               where every session that writes code works
```

- **`projects.yaml`** (or `.yml`/`.json`): the registry every script reads. See
  [`projects.example.yaml`](projects.example.yaml). Forge org and host,
  tracker kind (Linear team or GitHub Issues), stage names, an optional queue label, and the
  repos with the tracker label that targets each one. States, labels and teams are resolved by
  name at runtime; no IDs are stored.
- **`.env`**: `LINEAR_API_KEY` for a Linear project. GitHub needs only `gh auth login`.
  Template in [`.env.example`](.env.example).
- **`contexts/<project>/`**: the product knowledge sessions read: `README.md` (start here),
  `cross-repo-changes.md` (what must land together), `hld.md`, `local-dev.md`, `debugging.md`,
  `qa.md`, `code-style.md`, plus the project's own `skills/`, `commands/` and `scripts/`
  (symlinked into `.claude/`), and `triage.md` when the triage pass runs. All optional; see
  [`docs/new-project.md`](docs/new-project.md).
- **[`scripts/tracker.py`](scripts/tracker.py)**: one CLI over Linear (GraphQL) and GitHub Issues
  (`gh api`). Listing a queue, moving, labeling, assigning, commenting, creating and appending
  to a ticket are plain API calls, never a model turn. Stdlib Python; PyYAML is used if present.
- **Runners**: bash scripts, one job each, safe to run from cron. A project gets a built-in runner
  only when its config names what that runner needs (see [Configuration](#configuration)), and
  can add its own periodic scripts under `tasks:`. They read the queue through
  `tracker.py` and spawn `claude -p` sessions with a fixed permission policy.
- **[`sdlc.py`](sdlc.py)**: a curses TUI that runs the runners on intervals and shows their logs.
- **Skills** in [`.claude/skills/`](.claude/skills/): the same flows, runnable by hand from a
  `claude` session started in this folder.
- **[`CLAUDE.md`](CLAUDE.md)**: the agent-facing index and hard rules. This README is the human one.

## Ticket flow

Stage keys: `triage`, `design`, `todo`, `in_progress`, `in_review`, `qa`, `discussion`. Each maps
to a Linear state name, or a GitHub label (`stage:todo`, `stage:in-progress`, ...; one stage may be
"no stage label").

```
Triage --(human)--> Design --design-tickets.sh--> Todo --implement-tickets.sh--> In Progress --> In Review
                      ^                             |                                 |              |
                      |                             |                    stale > 6h   |       review-prs.sh (others' PRs)
                      +------ VAGUE ----------------+   <--- reset-stale-tickets.sh --+       update-prs.sh (your PRs)
                                                    |                                                |
                          Requires Discussion <- UNCLEAR                                   human merges
                          QA <------------------ IMPLEMENTED                                         |
                                                                                    QA --> closed on your OK
```

Triage is yours unless the project configures the triage pass (`tracker.triage`): then
`triage-issues.sh` validates each Triage issue, comments, labels it and moves it on.

The implement pass claims its whole queue first (every ticket to In Progress, which no other
pass touches), then triages each ticket on Sonnet against the code on `origin`:

| Verdict | Means | Goes to |
|---|---|---|
| SKIP | an open PR already implements it | In Review, with the PR linked |
| IMPLEMENTED | the default branch already does it | QA, with a comment naming where |
| BLOCKED BY | needs another ticket's unmerged work | Todo, marked blocked by that ticket in the tracker (a GitHub issue dependency, a Linear `blocks` relation); hidden from the queue until it closes |
| SPECIFIC | files, behaviour and tests are named | implemented on Sonnet |
| DESIGNED | goal, code area and acceptance are clear, no file-level plan | implemented on Opus |
| VAGUE | no location or expected behaviour, or a contradiction a design pass can settle | Design, with a comment on what's missing |
| UNCLEAR | needs a human call (product, scope, legal) | Requires Discussion |

Implementation opens as few PRs as it can: a small fix that belongs in an open PR of yours is
pushed to that PR, several small tickets on the same code share one PR, and a stack is only one
ticket split into steps. Whatever the session leaves In Progress goes back to Todo when it ends.
The triage pass never touches issues you opened yourself, and never reviews your own PRs.

The design pass answers that comment and appends a design section ending in an "Implementation
instructions" block (files, interfaces to keep, tests, commands, acceptance check), which the
next triage rates SPECIFIC. A ticket that carries instructions is never sent back to Design for
lacking detail, so the two passes can't ping-pong.

With a queue label (say `Ready for AI`), only labeled tickets are picked up, and the design pass
hands off by dropping the label: moving the ticket back to Todo is then your call. Without one,
the design pass moves the ticket to Todo itself. Moving In Review to QA after merge is up to you
or your tracker's automation.

## Prerequisites

- Linux or macOS (the scripts avoid `flock` and GNU-only `date` flags)
- [Claude Code](https://docs.claude.com/en/docs/claude-code/setup) (`claude`), logged in
- `git`, `bash`, `jq`, Python 3 (stdlib; `curses` for `sdlc.py`)
- [`gh`](https://cli.github.com/), logged in to the project's forge host with access to every tracked repo
- For Linear: a personal API key ([linear.app/settings/api](https://linear.app/settings/api)).
  The Linear MCP server in `.mcp.json` is optional (the runners skip the flag when the file is absent); sessions may use it to read and comment.

## Quickstart

```bash
mkdir work && cd work
git clone https://github.com/<you>/ai-sdlc.git
git clone <each repo of your project>        # siblings of ai-sdlc; folder names don't matter
cd ai-sdlc

cp projects.example.yaml projects.yaml
$EDITOR projects.json                        # keep your project's block, delete the other
mkdir -p contexts/<key> && $EDITOR contexts/<key>/README.md
cp .env.example .env                         # Linear: set LINEAR_API_KEY

gh auth status
./scripts/tracker.py me                      # the tracker answers
./scripts/tracker.py list --state todo       # the queue the implement pass would take
./scripts/tracker.py setup-labels            # GitHub Issues only: create stage and queue labels
./scripts/repo-path.sh --list                # which clone backs which repo

DRY_RUN=1 ./sdlc.py                          # every runner's decisions, no sessions, no tokens
```

Then run [`docs/workspace-setup.md`](docs/workspace-setup.md) against each clone (remote, git
identity, default branch). Repos are matched to clones by git remote, so a tracked repo you
haven't cloned is logged and skipped.

## Configuration

A GitHub Issues project:

```yaml
operator:
  name: Widget Operator
  forge: {kind: github, org: acme-inc}   # host: for GitHub Enterprise
  tracker:
    kind: github
    stage_labels: {triage: triage, design: triage, todo: null}   # null = "no stage label"
    exclude_labels: [question, wontfix]
  repos:
    widget-operator: {}
```

A Linear project:

```yaml
shop:
  name: Shop
  forge: {kind: github, org: acme-inc}
  tracker:
    kind: linear
    team: ABC                     # team name or key
    queue_label: Ready for AI     # optional
    states: {design: Design, todo: Todo, in_progress: In Progress, in_review: In Review, qa: QA}
  repos:
    api: {label: API}             # the tracker label that targets this repo
    web: {label: Web}
    infra: {}                     # reviewed, no ticket automation
```

Each built-in runner applies to a project only when the config names what it works on; the others
skip it with a log line. `./scripts/tracker.py config tasks` shows which apply and why not.

| Task | Needs |
|---|---|
| `pull`, `review`, `update` | the repos |
| `design` | a `design` stage (`tracker.states` on Linear, `tracker.stage_labels` on GitHub) |
| `implement`, `reset` | a `todo` stage |
| `triage` | a `triage` stage, `tracker.triage` and `<context>/triage.md` (the policy: what each outcome means, who it writes as) |

```yaml
  tracker:
    stage_labels: {triage: untriaged, todo: null}
    triage:
      outcomes: [[feature], [bug, approved], [invalid], [question]]  # a triaged issue carries one group
      labels: [help wanted, documentation]                            # others it may add
      next: todo                                                      # where it hands issues (default)
  tasks:
    monitor: {script: scripts/monitor-deploys.sh, every: 10m}         # the project's own script, relative to the context folder
    review: {every: 1h}                                               # a built-in's wait between passes
    implement: {every: 0}                                             # 0 = only when started by hand
```

`every` is the wait after a pass ends before the next one starts: seconds, or `30s`, `10m`, `4h`,
`1d`. Built-in defaults: pull 1h, review 15m, update 30m, design 30m, implement 4h, reset 30m,
triage 30m; a project task without `every` waits 1h. `<TASK>_EVERY` in the environment wins over
both (see [the scheduler](#the-scheduler)).

Other keys: `env` (an environment name, see [several projects](#several-projects-or-environments)),
`context` (default `contexts/<key>`), `repos_root` (where the clones live, default the parent of
the folder holding the config file), `tracker.key` (ticket prefix),
`tracker.design_labels` (`{group: [labels]}` the design pass may add), `tracker.ready_labels`
(GitHub: `[[a, b], [c]]`, a ticket is ready when it has all labels of one group), `tracker.blocker_label`
(put on follow-ups that must land before a release). Several projects can share one instance:
put several blocks in one file, name other files in a top-level `include:`, or list files in
`SDLC_CONFIG`; see
[several projects](docs/new-project.md#several-projects-in-one-instance).
`./scripts/tracker.py config <what>` prints any resolved setting.

Environment, shared by the runners:

| Variable | Meaning |
|---|---|
| `DRY_RUN=1` | print decisions; no sessions, no cache writes, no tracker writes |
| `INTERVAL=<sec>` | loop instead of a single pass |
| `REPOS_ROOT` | where the clones live, for every project (default: per project, `repos_root:` or beside the folder holding its config file) |
| `REPOS` | repo allowlist for `review-prs.sh`, `update-prs.sh`, `pull-all.sh` |
| `MODEL`, `CLAUDE_FLAGS` | session model alias and permission policy; `design-tickets.sh` also `FALLBACK_MODEL` |
| `ALL_CYCLES=1` | Linear: the whole queue, not just the active cycle |
| `SDLC_PROJECT`, `SDLC_CONFIG` | pick one project; `:`-separated config files to merge (`SDLC_CONFIG` may sit in `.env`) |
| `SDLC_ENV` | only the projects of that environment (`env:`) |
| `STALE_HOURS`, `STALL_MIN` | stale In Progress threshold (6h); stall timeout for a session (30 min) |
| `LOG_RETENTION_DAYS`, `LOG_KEEP_LINES` | log pruning (14 days, 20000 lines) |

Runtime state is gitignored: `.<runner>-cache.<project>.json` (delete one to retry failed items)
and `logs/<project>/<runner>/`.

## Running it

### The scheduler

```bash
./sdlc.py
```

Runs each task as one pass, then waits its interval (`every`, see [Configuration](#configuration))
before the next. Every project gets a row per task its config turns on, built-in or its own, run
with `SDLC_PROJECT` set; with several projects loaded rows are named `<task>-<key>`. `pull` covers
all projects and has one row. `<TASK>_EVERY` in seconds overrides a row's interval for this run
(`REVIEW_EVERY` for the row `review`, `REVIEW_SHOP_EVERY` for `review-shop`; `0` = manual only).
`SDLC_ENV=prod ./sdlc.py` schedules one environment only.

Keys: `1`-`9` run a visible row now, `a`/Tab switch the log view, ↑/↓/PgUp/PgDn/End scroll,
`p` pause everything, `d` enable/disable the viewed task, `k` kill it (or every visible task from
the combined log), `q` quit after running tasks finish (twice to kill them), `x` after `q` to quit
and leave them running.

With several workspaces loaded (a project's `env:`, else its key), a tab bar sits under the
header: `all`, then one tab per workspace with its running count (`●2`), red when one of its
tasks last failed and `⏸` when it is paused. ←/→ or `e` switch tabs; the rows, the combined log
and the `1`-`9` keys follow the tab. The `all` tab adds a workspace column to the rows and tags
each log line with its workspace, in the tab's colour (the `pull` row, shared by all, is `-`). `P` pauses the
workspace in view: its tasks stop starting on schedule, run now still works, and the pause
survives restarts. Schedule state survives restarts in `.sdlc-cache.json`; output also goes to
`logs/sdlc.log`.

### Runners on their own, or from cron

| Runner | Takes | Does |
|---|---|---|
| [`pull-all.sh`](pull-all.sh) | tracked repos | `git fetch` only, never touches a working tree; `pull --ff-only` on this folder when clean and on the default branch |
| [`design-tickets.sh`](design-tickets.sh) `[--all-cycles \| ID]` | Design tickets | one read-only session per ticket (Opus; research in a Fable subagent, adversarial check on Opus), appends the design, the script checks the original description survived and hands off |
| [`implement-tickets.sh`](implement-tickets.sh) `[--all-cycles]` | unblocked Todo tickets, priority order | claims them all (In Progress), then one session running `/implement-tickets`: triage, implement in worktrees, fewest PRs, move to In Review; releases what the session left In Progress |
| [`reset-stale-tickets.sh`](reset-stale-tickets.sh) | In Progress older than `STALE_HOURS`, when no implement pass is running | no model; moves back to Todo with a comment |
| [`triage-issues.sh`](triage-issues.sh) `[ID]` | Triage-stage issues opened by others | one session per issue: close per `policies.md`, or validate against the code (a failing test when it settles it), comment, label one outcome group, move to `tracker.triage.next` |
| [`review-prs.sh`](review-prs.sh) | open, non-draft PRs by other humans | one review session per PR in a detached worktree at the head; skips a head that already carries a human approve/changes-requested |
| [`update-prs.sh`](update-prs.sh) `[PR url]` | your PRs with a conflict or changes requested | session fixes or answers every point and merges the base; the script squashes, force-pushes with a lease and re-requests review |

```cron
3,18,33,48 * * * * /path/to/ai-sdlc/review-prs.sh          >> /path/to/ai-sdlc/logs/review-prs.log 2>&1
5,35       * * * * /path/to/ai-sdlc/update-prs.sh          >> /path/to/ai-sdlc/logs/update-prs.log 2>&1
7,37       * * * * /path/to/ai-sdlc/design-tickets.sh      >> /path/to/ai-sdlc/logs/design-tickets.log 2>&1
0 */4      * * * /path/to/ai-sdlc/implement-tickets.sh     >> /path/to/ai-sdlc/logs/implement-tickets.log 2>&1
*/30       * * * * /path/to/ai-sdlc/reset-stale-tickets.sh >> /path/to/ai-sdlc/logs/reset-stale-tickets.log 2>&1
9,39       * * * * /path/to/ai-sdlc/triage-issues.sh       >> /path/to/ai-sdlc/logs/triage-issues.log 2>&1
```

Each runner takes a `mkdir` lock so passes never overlap; the implement lock holds the pass's
pid, so it is only taken over once that pass is dead, and killing the pass (from `sdlc.py` or
Ctrl-C) stops its session and puts its tickets back. Per-script details:
[`scripts/scripts.md`](scripts/scripts.md).

### Interactive skills

Start `claude` in this folder (skills only load from here):

| Skill | Does |
|---|---|
| `/review-prs` | reviews the PRs awaiting you, same policy as the runner |
| `/implement-tickets [ids]` | the implement pass, on the given tickets or the Todo queue |

A project can ship more under `contexts/<key>/skills/` (for example a QA cycle); they are linked
into `.claude/skills/`.

### Models and sessions

Models are aliases (`sonnet`, `opus`, `fable`, `haiku`), so each run gets the latest of a family;
sessions pass `--fallback-model opus,fable`, which covers an overloaded model but not a usage
limit. Sessions run with auto-memory off and `--setting-sources project,local`, so nothing in
`~/.claude/` leaks in: a session knows only this folder, the repo and its prompt. The design and
implement sessions run through [`scripts/claude_run.py`](scripts/claude_run.py), which streams
events as a heartbeat, kills a session quiet for `STALL_MIN` minutes and resumes it once.

## Adding your project

Three things change, no script does: a block in `projects.yaml`, a `.env`, and a
`contexts/<key>/` folder. [`docs/new-project.md`](docs/new-project.md) has the full examples and
the list of context files the framework looks for.

## Several projects or environments

One instance can serve many projects, and each project can live in its own config file
(`projects.yaml`, `.yml` or `.json`), for example one per environment or customer. Name the
environment so logs and the scheduler say what runs where: a top-level `env:` in a file applies to
every project in it, and `env:` in a project block wins over it.

```yaml
env: staging                      # this file's projects; `env` can't be a project key
customer-x:
  forge: {kind: github, org: customer-x}
  ...
customer-x-eu:
  env: staging-eu                 # this one only
  ...
```

```bash
export SDLC_CONFIG=projects.yaml:../customer-x/projects.json   # ':'-separated, relative to this folder; or the same line in .env
./scripts/tracker.py config projects                           # the merged project keys
./sdlc.py                                                      # every project, its own rows per task
SDLC_PROJECT=customer-x ./review-prs.sh                        # one project
SDLC_ENV=staging ./review-prs.sh                               # the projects of one environment
./scripts/tracker.py config envs                               # project -> environment
SDLC_PROJECT=customer-x ./implement-tickets.sh customer-x#12   # a ticket id needs SDLC_PROJECT
./scripts/tracker.py --config ../customer-x/projects.json --project customer-x list
```

- Without `SDLC_CONFIG` or `--config`, only the first `projects.yaml|yml|json` here is read, plus
  whatever its `include:` names.
- Files merge by project key. Reusing a key with a different block, or putting a repo in two
  projects, stops every script and names the file. Give each environment its own key.
- Caches (`.<runner>-cache.<key>.json`) and logs (`logs/<key>/<runner>/`) are per key, so
  environments never share state. Resetting one means deleting that file.
- A relative `context:` resolves against the folder of the file that defines the project, so a
  project kept elsewhere brings its context folder along.
- `.env` and the `gh` login are shared. For different credentials per environment, export the
  variable (e.g. `LINEAR_API_KEY`) before the run: the environment wins over `.env`.
- Clones are looked for beside the folder holding the file that defined the project (a
  symlinked file counts as its real one), or in its `repos_root:`. `REPOS_ROOT` in the
  environment overrides that for every project.
- Another hub's project can be linked in under `contexts/`: `contexts/<key>.yaml` to its config
  file and `contexts/<key>` to its context folder, then `include: [contexts/<key>.yaml]` at the
  top of `projects.yaml` (paths relative to the including file; a file is read once). Then a plain
  `./sdlc.py` runs every workspace, each as its own group.

Details in [`docs/new-project.md`](docs/new-project.md#several-projects-in-one-instance).

## Safety

- **Worktrees only.** Anything that writes code works under
  `$REPOS_ROOT/.worktrees/<ticket-id>/<repo>`; reviews use a detached worktree per repo. The
  sibling checkouts may hold someone's uncommitted work and are never checked out, switched,
  stashed or merged in.
- **Never merge, never push to a default branch.** The default permission policies deny
  `gh pr merge` and the relevant `git push` forms (all pushes for review and design sessions;
  `update-prs.sh` pushes from the script, never the session). These are prefix rules, not a
  sandbox: also protect your default branches on the forge.
- **No destructive operations on cloud infrastructure or databases.** Investigation is
  read-only; project data scripts should carry their own write guards.
- **`DRY_RUN=1`** works on every runner, `tracker.py` and `sdlc.py`, and spends no tokens.
- Failure caches stop a broken PR or ticket from burning tokens every pass.
- `.claude/settings.json` turns off commit and PR attribution: changes read as the developer's.

## Conventions

- [`docs/conventions.md`](docs/conventions.md): read a repo's own `CLAUDE.md` and `docs/` first,
  worktrees, the tracker as a CLI, PR hygiene, safety.
- [`docs/code-style.md`](docs/code-style.md): reuse before new code, short comments, PR
  descriptions of 150 words or fewer that match the head commit, repo docs kept at the level of
  architecture, tests that fail without the fix, no compatibility shims between repos that ship
  together.
- [`docs/stacked-prs.md`](docs/stacked-prs.md): when to stack (only one ticket split into steps; an open PR you need is patched or a blocker) and how
  to keep a stack current.
- [`docs/workspace-setup.md`](docs/workspace-setup.md): the per-clone checklist.

## Design notes

From the history, the decisions worth knowing before you change things:

- **Tracker writes left the model.** Queue reads used to be a model turn over the Linear MCP with
  UUIDs hardcoded in skills. `tracker.py` replaced that with deterministic calls over two
  backends, resolving names at runtime.
- **Fetch, don't pull.** `pull-all.sh` once ran `git pull --ff-only` in every checkout and broke
  on dirty trees and feature branches. Worktrees only need `origin/*`, so it fetches.
- **Reviews moved into worktrees** after a review and an implement session ended up sharing one
  working tree. One detached worktree per repo, re-pointed per PR, keeps it cheap.
- **Review per commit, not per date.** A PR is skipped while a human verdict sits on its current
  head; only new commits bring it back.
- **Triage before implementing.** Sending vague tickets back with a comment,
  and never sending back a ticket that carries instructions, came after early runs implemented
  tickets that were already done or underspecified.
- **No session memory.** Runs that acted on per-machine auto-memory were acting on context nobody
  could see, so sessions load only project settings.
- **The session doesn't push** in `update-prs.sh`; the script squashes and force-pushes with a
  lease on the head it saw.
- **Project-agnostic by construction.** The framework started inside one product's workspace;
  everything about that product was moved into its context folder, and the runners read the
  rest from config.
- **Portable shell.** `mkdir` locks and BSD-first `date` helpers, after `flock` and `date -Is`
  made every pass silently exit on macOS.
