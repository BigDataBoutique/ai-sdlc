# Code and PR style

Rules for anything that writes a commit, a PR or a review in this workspace, on any project.
A project's own additions are in `contexts/<project>/code-style.md`, when it has one.

## Writing code

Read the code the change touches and trace the flow end to end first. Then take the first of
these that works:

1. Skip it, if nothing needs it yet.
2. Reuse what this codebase already has: a helper, a type, a pattern a few files over.
3. The standard library.
4. A platform feature: a DB constraint over app code, a native input over a component library.
5. A dependency that's already installed. Never add one for what a few lines can do.
6. Only then, the least new code that works.

- No interface with one implementation, no factory with one product, no config for a value
  that never changes, no scaffolding for later.
- **Fix the root cause, once.** Grep every caller of the function you touch. A guard in the
  shared function is a smaller diff than one per caller, and fixing only the path the ticket
  names leaves the sibling caller broken.
- Never simplify away validation at a trust boundary, error handling that prevents data loss,
  security, or anything the ticket asks for.
- A shortcut with a known limit (a global lock, an O(n²) scan) gets a line in the PR body:
  `Skipped X; add when Y.`

## Comments

A comment carries only what the reader can't get from the code: a measured number and where it
came from, or a one-line warning that the obvious approach is wrong.

- **About 3 lines inline.** Docstrings say what the code does. A test docstring gets 1-2 lines.
- **Don't describe what a few lines of code do.** No comment above a block to summarize it, and
  none that restates the next line. If a block needs one, give it a name instead: a function or
  a variable.
- **Say it once.** The reasoning goes in one place, either the comment or the PR body. Don't also
  copy it into test docstrings or the other repo's PR. Copies drift apart, and they contradict
  each other.
- **No history.** No "used to", "the old design", "the first version was wrong", and no
  alternatives nobody wrote. git has the history.
- **No ticket refs, milestones or version changelogs** in code.
- **No instance names**: no cluster ids, customer or host names.
- **Don't copy a number owned by another repo.** Cadences, TTLs and deadlines live in deploy
  config or in another service, and a copy goes stale when they change. Name the source
  instead.
- **Check a "because" before you write it.** Run the check first. Don't infer it.

## PR descriptions

**150 words at most.** State what changed, why, and what the reviewer should look at. A one-line
diff gets one to three sentences.

- **Keep it true to the head commit.** When a commit changes something the body claims, edit the
  body in the same push, before you ask for re-review.
- **Leave out anything that goes stale by itself**: test counts, pin hashes, "(merged)", "not
  opened yet", merge-order caveats that are already resolved. `Depends on <PR>` is fine.
- **Don't retell the investigation or the review.** That belongs in the thread.
- No test-plan section.

## Repo docs

Every repo has a `CLAUDE.md` and a `docs/` folder. Read them before changing the repo. They
exist so the next implementer skips discovery: the repo map, how each major feature area is
built and why, the commands, and the few traps that cost real time. They are guidance, not a
changelog, an API reference or a log of every fix.

Keep them current in the same PR as the change:

- When the change adds, moves, renames or removes something a doc describes (a feature area,
  a module, an endpoint group, a store, a command, a config key), update the doc.
- **Describe the shape, not the detail**: where a feature lives, how its data flows, which
  pattern to copy, which decision not to undo. Field lists, per-endpoint request and response
  shapes, constants and line numbers belong in the code; point at the file instead.
- **A gotcha earns a line only if it's broad**: it applies beyond the code you just changed, it
  compiles and passes CI, and it would cost the next person real time. A trap local to one
  function is a comment next to that function. A bug you fixed is not a gotcha.
- One doc per major feature area. Don't start a file for one endpoint or one fix; fold it into
  its area's doc, or leave it out.
- Delete what's no longer true. Don't append a correction below it.
- The same style rules as comments apply: short, no history, no ticket refs.

What else must change with a feature (a docs site page, a companion repo) is in the project's
cross-repo doc, `contexts/<project>/cross-repo-changes.md` when it has one.

## Tests

A test earns its place by failing when something that matters breaks.

- **New or changed logic gets tests.** Any non-trivial branch, loop, parser, query or
  calculation gets tests for the happy path and the edge cases that can actually happen: empty
  and null input, boundaries (time windows, pagination, limits), and partial failure. A
  one-line or purely mechanical change needs none.
- **Check that it fails without the fix.** Remove the fix, watch the test go red, then put the
  fix back.
- **Run the code; don't grep it.** Don't use `inspect.getsource`, match substrings of SQL, or
  compare a constant to itself. A query is tested by running it.
- **Don't assert on the mock or the config.** `assert_not_called` on patched functions,
  re-reading a chart's axis config, or asserting against the same shared constant the code
  reads: all of these pass while the bug is live.
- **Don't mock away what the fix relies on.** If the fix depends on sort order, don't feed the
  test data that is already sorted.
- **Test the cross-repo contract.** That's where the real bugs are. This covers:
  - a field that becomes nullable, and every consumer's type for it
  - enum-like strings that one repo emits and another matches on
  - env var and config key names that another service reads

  Build fixtures from the other side's real output, not from your own type. Test through the
  path the app actually uses, not only a pure helper.
- Test the branch most likely to fire in production. Don't test the branch nothing can reach.
- If you skip testing the failure mode the PR exists to prevent, say so in the body.

## Cross-repo changes and compatibility

**Repos that ship to prod together need no compatibility code between them** (the project's
context says which do). A stack across them carries none of:

- an old field kept so one side can deploy first, or a test pinning it
- a fallback for a field the same stack adds
- "older backend" code paths, or "safe to merge in either order"
- a loose type so an unknown value quietly falls back to a default

Rename or remove the field in every PR of the stack at once.

Compatibility is real where versions really differ, and only for a shape that has shipped: a
client that upgrades slowly, a dependency pinned in another repo and deployed on its own
schedule, data already written to a store. Say which deployed version needs it.

What must land with a change: `contexts/<project>/cross-repo-changes.md`.

## Review hygiene

- **Fix the same bug next door.** When you fix a pattern, grep for it and fix every copy in the
  same PR.
- **Answer every inline thread before merge, even on an approved PR.** Fix it or reply.
