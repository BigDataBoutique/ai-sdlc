# Working conventions

How an agent works in this workspace, on any project in [`projects.yaml`](../projects.yaml).
Project-specific knowledge (design, change chains, environments, policies) lives in that
project's context folder, `contexts/<project>/`; see [`new-project.md`](new-project.md).
`CLAUDE.md` carries the short form of these rules; this is the reasoning behind them.

## Read a repo's own `CLAUDE.md` and `docs/` before exploring it

Each repo carries its own `CLAUDE.md` and usually a `docs/` folder with a repo map and other
guidance (architecture, commands, UI conventions). Read what's there **first** when a task
touches that repo — before grepping around for full discovery. Contents and filenames vary per
repo; skim the folder listing to find the relevant one.

**That is also where a repo's traps live** — the few broad, non-obvious rules whose violation
compiles and passes CI, like a read that needs `FINAL`. Narrow traps live as a comment beside
the code they guard; [`code-style.md`](code-style.md) ("Repo docs") says which is which. They
belong beside the code they describe, owned by the people who change it, so they help whoever
is writing as much as whoever is reviewing. Don't copy them into a skill or a prompt: a second
copy drifts, and the review that quotes the stale one is worse than the review that looked it
up.

A convention asserted from memory is how a review ends up citing a rule the repo dropped
months ago.

## Sessions carry no memory

A session spawned from here starts from this folder, the repo it works on and the prompt it
was given, and nothing else. Claude Code's auto-memory (`~/.claude/projects/<slug>/memory/`)
is a per-machine record of earlier sessions, often about other work entirely, and a review or
an implementation that acts on it is acting on something nobody can read in the repo.

The runners therefore set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and pass
`--setting-sources project,local` (no `~/.claude/settings.json`, no `~/.claude/CLAUDE.md`),
and `.claude/settings.json` here sets `autoMemoryEnabled: false` for interactive sessions.
Dropping the user source also drops the MCP servers registered in `~/.claude.json`, so the
optional Linear server is declared in `.mcp.json` here (skipped when absent) and passed with `--mcp-config` (the stored
OAuth login is reused). A new script copies the wrapper block from `review-prs.sh`. What still
loads: the cwd's `CLAUDE.md`, `.claude/` settings and skills, and OAuth. `--bare` is not used
because it refuses OAuth.

## Worktrees, not checkouts

Anything that writes code runs in a git worktree under `$REPOS_ROOT/.worktrees/<ticket-id>/<repo>`
(`REPOS_ROOT` is the folder holding the clones, by default the parent of this one), never in
the sibling checkout itself: those hold uncommitted work and untracked files such as local
credentials, which a branch switch can destroy.

So does anything that needs a particular commit on disk. `review-prs.sh` takes one detached
worktree per repo, re-pointed at each PR head (`.worktrees/review-prs/<repo>`), and
`update-prs.sh` one per PR (`.worktrees/update-prs/<repo>-<n>`), both under `$REPOS_ROOT`, so a runner and a session
you are driving yourself never contend for one working tree. A session that only reads — design
research, or a review of a repo with no local clone — may read a checkout as it stands, but
must never `gh pr checkout`, `git checkout`, `git switch`, `git stash` or merge in one: fetch
and diff, or take a worktree.

Run `./pull-all.sh` first so worktrees branch off a fresh `origin/<default branch>`. It fetches
only, so it is safe to run against checkouts holding uncommitted work, and it walks the tracked
repos in `projects.yaml` — one you have not cloned is logged and skipped. It also pulls
ai-sdlc itself (this folder is worked in directly, not through a worktree), ff-only and only
when the tree is clean and on the default branch.

## The tracker is a CLI, not a prompt

Listing a queue, moving a ticket between stages, adding or removing a label, assigning, reading
a ticket or opening one are mechanical, so they go through `scripts/tracker.py` (Linear or
GitHub Issues, per `projects.yaml`) — deterministic, no Claude turn, the same command on both
trackers. A session decides *what* to write and *whether* a ticket should move; the CLI does
the write. Comments and design text may still go through the Linear MCP when the tracker is
Linear. Never hardcode a state, label or team ID in a script or a skill: the CLI resolves
names at runtime.

## Never merge, never push to a default branch

Scripts and skills only review, comment and open PRs. The headless runners are given
permission policies that block `gh pr merge` and pushes to defaults; don't work around them.

## Commit / PR hygiene

Never mention AI, Claude or Anthropic anywhere in a commit message, PR title, PR description,
review comment or issue-tracker text — no "Generated with Claude", no co-author trailer, no
tool attribution. Write as if a human wrote the change.

Commit as the developer, with the address the repos' CI expects (the project's context folder
says if it checks one). Before the first commit in a worktree or fresh checkout, run
`git config user.email` and fix a wrong value; verify with `git log -1 --format='%ae'`. Never
rewrite a default branch to repair a commit that already landed.

## Safety

NEVER attempt any destructive operation on cloud infrastructure or the databases running on
it. Investigation is read-only: a project's data scripts (in its context folder) carry local
write guards on top of whatever the server enforces, and a write flag is not one to reach for
casually.
