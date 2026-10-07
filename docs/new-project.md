# Pointing the framework at another project

Three things change; no script or skill does.

1. **`projects.yaml`**: forge, tracker and repos of the project (examples below).
2. **`.env`**: secrets. `LINEAR_API_KEY` for a Linear project; GitHub Issues need only `gh auth login`.
3. **`contexts/<key>/`**: what an agent should know about the product: design, how repos depend on
   each other, local dev, environments. Optional files the framework looks for:

   | File | Read by |
   |---|---|
   | `README.md` | every session: what the project is, the file index, repo notes |
   | `cross-repo-changes.md` | review, update and design passes, when a change touches a shared contract |
   | `hld.md`, `local-dev.md`, `debugging.md` | design, implement and QA sessions, as they apply |
   | `qa.md` | the `qa-cycle` skill: how a fix reaches each environment and how to verify it |
   | `code-style.md` | additions to [`code-style.md`](code-style.md) |
   | `skills/<name>/`, `commands/`, `scripts/` | project skills and tools, linked into `.claude/skills/<name>` and `.claude/commands/` by a relative symlink |
   | `triage.md` | the triage pass: what each outcome in `tracker.triage` means, who it writes as; required for it to run |
   | `scripts/*.sh` named under `tasks:` | run by `sdlc.py` as the project's own periodic tasks |
   | `.env.example` | the project's own `.env` settings |

The context folder is `contexts/<key>/` unless `context:` in `projects.yaml` says otherwise (see
[several projects](#several-projects-in-one-instance) for files kept elsewhere). Drop the
old project by deleting its `contexts/<key>/` folder and its symlinks under `.claude/`.

A built-in runner only works a project whose config names what it needs: `design` needs a design
stage, `implement` and `reset` a todo stage, `triage` a triage stage plus `tracker.triage` and
`triage.md` (table in the [README](../README.md#configuration)). `./scripts/tracker.py config tasks`
lists them with the reason for each skipped one, followed by the project's own tasks:

```yaml
  tasks:
    monitor: {script: scripts/monitor-deploys.sh, every: 10m}   # path relative to the context folder
    review: {every: 1h}                                         # a built-in's wait between passes
```

Then `./scripts/tracker.py me`, `./scripts/tracker.py list --state todo` and
`DRY_RUN=1 ./sdlc.py` show what the runners would do without spending a token.

## A GitHub-only project

Issues and PRs live in the repos; the queue is expressed with labels.

```yaml
widgets:
  name: Widgets
  forge: {kind: github, org: acme-inc}    # host: for GitHub Enterprise
  tracker:
    kind: github
    stage_labels:                 # a stage is a label; null is the stage with no stage label
      triage: triage              # issues that still need triage ...
      design: triage              # ... are the design pass's queue
      todo: null                  # an open issue without the triage label is ready to implement
    exclude_labels: [question, wontfix]   # never picked up
  repos:
    widgets: {}
```

- **No `queue_label`**: the stage alone is the queue. Set one (`queue_label: automation`) to
  restrict the passes to issues carrying it.
- **Stages not listed** keep their defaults (`stage:in-progress`, `stage:in-review`, `stage:qa`,
  `stage:requires-discussion`); `./scripts/tracker.py setup-labels` creates the labels the
  config names in each repo. Two stages may share a label; at most one may be `null`.
- **Ticket IDs** are `<repo>#N`; `create` needs `--repo` when there are several repos.
- **What each runner does**: `design-tickets.sh` designs `triage` issues and hands them over
  (moves them to ready, i.e. drops the label); `implement-tickets.sh` implements ready issues
  and opens PRs; `review-prs.sh` reviews open non-draft PRs of others with no verdict on the
  current head; `update-prs.sh` fixes only your own PRs that have a merge conflict with the base
  branch or changes requested. Nothing merges or pushes to a default branch.

## A Linear project

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
    api: {label: API}             # the Linear label that targets this repo
    web: {label: Web}
    infra: {}                     # tracked for review, no ticket automation
```

Optional keys: `tracker.design_labels` (`{group: [labels]}` the design pass may add),
`tracker.blocker_label` (put on follow-ups that must land before a release), `key` (ticket
prefix, saves a lookup).

## Several projects in one instance

Put each project in its own file, or several blocks in one, and list the files in `SDLC_CONFIG`
(`:`-separated, relative to this folder) or pass `--config` to `tracker.py`. Without either, only
`projects.yaml` is read.

```bash
SDLC_CONFIG=projects.yaml:../customer-x/projects.yaml ./sdlc.py
```

- The files are merged by project key. A key in two files must have the same block there, and a
  repo belongs to one project only; either conflict stops every script with the file and key named.
- A relative `context:` (and the `contexts/<key>/` default) resolves against the folder of the
  file that defined the project, so a project kept outside this folder brings its context along.
- With several projects and no `SDLC_PROJECT`, each runner runs once per project in turn, with
  its own cache (`.<runner>-cache.<key>.json`) and logs (`logs/<key>/<runner>/`). A runner given a
  ticket id needs `SDLC_PROJECT`. `sdlc.py` gives each project its own `<task>-<key>` rows.
- `env: <name>` at the top of a file names the environment of its projects (a project's own `env:`
  wins). `SDLC_ENV=<name>` limits the runners and `sdlc.py` to it; `tracker.py config envs` lists them.
- `.env` is shared: one `LINEAR_API_KEY` and one `gh` login serve every project.
