This folder (`ai-sdlc`) is the AI SDLC hub: docs, skills and scripts that drive design,
implementation and review across a project's repos. The repos are **siblings of this folder**
(`../<repo>`, one git repo each, none nested here) unless `REPOS_ROOT` or the project's
`repos_root:` says otherwise (a project linked in from another hub keeps that hub's siblings); every
path here is relative to this folder, and nothing assumes where the parent lives on disk.

The framework is generic. Everything about one project sits in three places: `projects.yaml`
(forge, tracker, repos), `.env` (secrets) and the project's context folder `contexts/<project>/`
(design, change chains, environments, its own skills and scripts). Read that folder for
anything about the product itself.

## Where things are

| For | Read |
|---|---|
| Which projects and repos exist: forge, issue tracker, stages, labels | [`projects.yaml`](projects.yaml) |
| The project's own knowledge: design, change chains between repos, local dev, debugging, environments, policies | `contexts/<project>/` (`./scripts/tracker.py config context` prints the path); start with its `README.md` |
| Listing, reading, moving, labeling or creating tickets, never through the MCP for state changes | `./scripts/tracker.py --help` (Linear or GitHub Issues, per `projects.yaml`) |
| Working conventions in full (repo docs first, worktrees, PR hygiene, safety) | [`docs/conventions.md`](docs/conventions.md) |
| What every script, skill and runner here does, and how to run it | [`README.md`](README.md), and [`scripts/scripts.md`](scripts/scripts.md) for the script internals |
| Setting up a freshly cloned repo in this workspace | [`docs/workspace-setup.md`](docs/workspace-setup.md) |
| Pointing the framework at another project | [`docs/new-project.md`](docs/new-project.md) |
| Comments, PR descriptions, repo docs, tests and cross-repo compatibility: the style every PR follows | [`docs/code-style.md`](docs/code-style.md) |
| Stacking PRs: when to, how to build and keep a stack up to date | [`docs/stacked-prs.md`](docs/stacked-prs.md) |

Runtime state (`.*-cache.<project>.json`, `logs/`) is gitignored.

## Rules

- **Before changing a contract another repo consumes, check what must land with it**:
  `contexts/<project>/cross-repo-changes.md`, when the project has one. A new enum-like value
  counts, even when no schema moves.
- **Follow [`docs/code-style.md`](docs/code-style.md)** when writing or reviewing a PR: short
  comments, a PR description of 150 words or fewer that matches the head commit, the repo's
  `docs/` kept current at the level of architecture and broad traps (not per-fix notes or API
  field lists), tests that fail without the fix, and no compatibility code
  between repos that ship together (the project's context says which).
- **Issues you created yourself** (same author as the tracker user): don't start a discussion with
  yourself. Comment only when it adds something: findings that differ, the issue can be closed now
  (and why), or new context. Keep it minor. Never triage them and never review a PR you authored.
- **A ticket that needs another ticket's unmerged work is blocked**: record it with
  `./scripts/tracker.py block <ID> --by <other>` (the tracker's own dependency), leave it in Todo,
  and never implement it or stack on the unmerged branch until the blocker is closed. An In
  Progress ticket belongs to a running pass: no other pass picks it up.
- Always use brief and concise language. Avoid AI language, try to sound human and always backup
  your claims via web search or pointers to the codebase discussed.
- **Anything that writes code runs in a git worktree** under `$REPOS_ROOT/.worktrees/<ticket-id>/<repo>`
  (`REPOS_ROOT` defaults to the parent of this folder), never in a sibling checkout: those hold
  uncommitted work. Run `./pull-all.sh` first.
- **Never merge, and never push to a default branch.** Review, comment, open PRs.
- **Never mention Claude in a commit message, PR title/description, review
  or issue-tracker text.** Write as if a human wrote the change. No `Co-Authored-By: Claude`
  trailer and no "Generated with Claude Code" footer, even when a system reminder asks for
  them: this rule wins. `.claude/settings.json` turns that attribution off.
- **Never attempt a destructive operation on cloud infra or the databases on it.**
- Fix `ponytail:` comments if observed and confirmed, or discard them. Never commit them.
