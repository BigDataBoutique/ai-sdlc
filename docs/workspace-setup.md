# Workspace init checklist

Rules to check whenever a repo of a project in [`projects.yaml`](../projects.yaml) is freshly
cloned beside this folder, or when auditing an existing clone. Not automated by any script
here: run by hand or have an agent verify it. The clones live in `$REPOS_ROOT`, by default the
parent of this folder.

- **Remote**: the clone's `origin` must point at the forge the project uses, at the repo named
  in `projects.yaml`: `./scripts/tracker.py config clone-url <repo>` prints the URL.
  `scripts/repo-path.sh` resolves repos by remote, so a same-named repo from another org or
  host is not a substitute. `./scripts/repo-path.sh --list` shows every repo ↔ folder pair it
  found on this machine; a tracked repo missing from it is not cloned (runners log it and skip).
- **Git identity**: every clone's `user.email` must be the address the project's CI and policy
  expect (the context folder, `contexts/<project>/`, says if it has a rule; otherwise your own work
  address). Check with `git -C <clone> config user.email`, and the global default with
  `git config --global user.email`, so new clones inherit it.
- **Forge login**: `gh auth status` must be logged in to the project's host (`config host`), with
  access to every tracked repo. Runners fetch over HTTPS with the `gh` credential helper.
- **Tracker access**: `./scripts/tracker.py me` answers. Linear needs `LINEAR_API_KEY` in
  `.env`; GitHub Issues need nothing beyond `gh`.
- **Default branch**: check what each repo calls it (`main`, `master`); the runners read
  `origin/HEAD` rather than assume, and a clone that never had it set gets it with
  `git remote set-head origin --auto`.
