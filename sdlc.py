#!/usr/bin/env python3
"""Run the SDLC tasks on a schedule and watch them. Stdlib only.

Each task is one of the headless scripts next to this file, run as a single pass
on its interval (or on a keypress). Their output goes to the screen and to
logs/sdlc.log. All other env (DRY_RUN, MODEL, CLAUDE_FLAGS, REPOS ...) passes
through to the scripts, so `DRY_RUN=1 ./sdlc.py` exercises everything for free.
Which projects load comes from projects.yaml and its `include:` list (or SDLC_CONFIG).

Last finish times and disabled tasks live in .sdlc-cache.json, so a restart
resumes the schedule instead of running everything again.

Keys: 1-9 run a row now, a/Tab cycle the log view, p pause everything, d enable/disable the task
in view, k kill (the task in view, or every visible task), q quit, arrows/PgUp/PgDn/End scroll.
With several workspaces loaded (a project's `env:`, else its key), a tab bar under the header
shows `all` and one tab per workspace with its running count; ←/→ or e switch tabs (rows, log and
keys follow the tab; the all log tags each line with its workspace in the tab's colour), and P pauses the workspace in view (or the one of the task in view): its
tasks stop starting on schedule, run-now still works. Paused workspaces survive restarts in
.sdlc-cache.json.

  The tasks come from `scripts/tracker.py config tasks`, per project: the built-ins its config turns
  on (pull, review, update, design, implement, reset, triage) and its own `tasks:` from projects.yaml,
  each run with SDLC_PROJECT set and named `<task>-<key>` when several projects are loaded. `pull`
  covers every project and appears once. The interval is the wait after a pass ends before the next
  one: the default, `tasks.<name>.every` in projects.yaml, or <TASK>_EVERY in seconds (e.g.
  REVIEW_EVERY, REVIEW_SHOP_EVERY), which wins; 0 = manual only. SDLC_ENV runs one environment only.
"""
import collections
import curses
import json
import os
import signal
import subprocess
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def config_tasks():
    """[(name, script, every, env vars, project, environment)]: pull once for everything, then every task
    each project's config turns on, run with SDLC_PROJECT set. SDLC_ENV / SDLC_PROJECT narrow the set."""
    tracker = os.path.join(ROOT, "scripts", "tracker.py")
    def config(*args):
        p = subprocess.run([tracker, *args], capture_output=True, text=True)
        if p.returncode:
            raise SystemExit("sdlc.py: tracker.py %s: %s" % (" ".join(args), p.stderr.strip()))
        return p.stdout.strip()
    keys = [os.environ["SDLC_PROJECT"]] if os.environ.get("SDLC_PROJECT") else config("config", "projects").split()
    envs = json.loads(config("--json", "config", "envs"))
    out, seen = [], set()
    for key in keys:
        for t in json.loads(config("--project", key, "--json", "config", "tasks")):
            if t["skip"]:
                continue
            if t["scope"] == "global":
                if t["name"] not in seen:
                    seen.add(t["name"])
                    out.insert(0, (t["name"], t["script"], t["every"], None, None, ""))
                continue
            name = t["name"] if len(keys) == 1 else t["name"] + "-" + key
            out.append((name, t["script"], t["every"], {"SDLC_PROJECT": key}, key, envs.get(key, "")))
    return out


TASKS = config_tasks()
LOG = os.path.join(ROOT, "logs", "sdlc.log")
STATE = os.path.join(ROOT, ".sdlc-cache.json")  # {name: last finish epoch}, gitignored
lock = threading.Lock()
try:
    state = json.load(open(STATE))
except (OSError, ValueError):
    state = {}
combined = collections.deque(maxlen=5000)


def save_state():
    json.dump(state, open(STATE, "w"))


class Task:
    def __init__(self, name, script, every, env=None, project=None, env_name=""):
        self.name, self.script, self.env = name, script, env
        self.project, self.env_name = project, env_name   # None / "" for a task that spans every project
        self.every = int(os.environ.get(f"{name.upper().replace('-', '_')}_EVERY", every))
        self.proc = self.started = self.last_exit = None
        self.last_line = ""
        last = state.get(name)  # resume the schedule; never run before = due now
        self.next_due = None if self.every <= 0 else (last + self.every if last else 0)
        self.lines = collections.deque(maxlen=2000)
        self.enabled = name not in state.get("_disabled", [])

    @property
    def running(self):
        return self.proc is not None

    def start(self):
        if self.running:
            return
        self.proc = subprocess.Popen([self.script], cwd=ROOT, env=self.env and {**os.environ, **self.env}, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.started = time.time()
        self.next_due = None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        with open(LOG, "a") as f:
            for raw in self.proc.stdout:
                line = raw.rstrip("\n")
                stamp = time.strftime("%H:%M:%S")
                f.write(f"{stamp} [{self.name}] {line}\n"); f.flush()
                with lock:
                    self.lines.append((stamp, line))
                    combined.append((stamp, self, f"[{self.name}] {line}"))
                    self.last_line = line
            code = self.proc.wait()
        with lock:
            self.last_exit = code
            self.proc = None
            state[self.name] = time.time()
            self.next_due = state[self.name] + self.every if self.every > 0 else None
            save_state()

    def kill(self):
        if self.running:
            os.killpg(self.proc.pid, signal.SIGTERM)


def fmt_delta(secs):
    secs = max(0, int(secs))
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


def main(scr):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    scr.timeout(500)
    tasks = [Task(*t) for t in TASKS]
    ws = lambda t: t.env_name or t.project  # None = global (pull), shown in every tab
    spaces = list(dict.fromkeys(ws(t) for t in tasks if ws(t)))
    palette = [curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_GREEN]
    for i, _ in enumerate(spaces):   # one colour per workspace, for its tab and its lines in the all log
        curses.init_pair(10 + i, palette[i % len(palette)], -1)
    color = lambda g: curses.color_pair(10 + spaces.index(g)) if g in spaces else curses.A_DIM
    multi = len(spaces) > 1
    tabs = [None] + spaces  # None = all workspaces
    tab = None  # the workspace shown
    view = -1  # -1 = combined log, else index into the visible tasks
    paused = False
    scroll = 0  # lines from the bottom; 0 = follow
    quit_queued = False  # q was pressed once: no new tasks, exit once running ones finish

    while True:
        now = time.time()
        paused_ws = set(state.get("_paused_envs", []))   # the key predates the workspace wording
        if not paused and not quit_queued:
            for t in tasks:
                if ws(t) not in paused_ws and t.enabled and not t.running and t.next_due is not None and t.next_due <= now:
                    t.start()
        if quit_queued and not any(t.running for t in tasks):
            return

        shown = [t for t in tasks if tab is None or ws(t) in (None, tab)]
        view = min(view, len(shown) - 1)
        h, w = scr.getmaxyx()
        scr.erase()

        def put(y, x, s, attr=0):
            if 0 <= y < h and x < w:
                scr.addnstr(y, x, s, w - x - 1, attr)

        head = f"SDLC  {os.path.basename(ROOT)}  {'PAUSED' if paused else 'auto'}"
        if multi:
            head += f"  workspace: {tab or 'all'}"
        put(0, 1, head, curses.A_BOLD)
        put(0, w - 10, time.strftime("%H:%M:%S"))

        top = 1
        if multi:   # one tab per workspace: its running count, red when a task failed, dim when paused
            x = 1
            with lock:
                for g in tabs:
                    mine = [t for t in tasks if g is None or ws(t) in (None, g)]
                    run = sum(t.running for t in mine)
                    label = f" {g or 'all'}{f' ●{run}' if run else ''}{' ⏸' if g in paused_ws else ''} "
                    attr = curses.A_REVERSE | curses.A_BOLD if g == tab else (color(g) if g else 0)
                    if g in paused_ws and g != tab:
                        attr |= curses.A_DIM
                    elif any(t.last_exit for t in mine if g is not None or ws(t) is None):
                        attr = curses.color_pair(2) | (attr & ~curses.A_COLOR)
                    put(top, x, label, attr)
                    x += len(label) + 1
            top += 1

        nw = max(len(t.name) for t in tasks) + 2 if multi else 10
        gw = max(len(g) for g in spaces) + 2 if multi and tab is None else 0
        for i, t in enumerate(shown):
            with lock:
                if t.running:
                    st, when, attr = "running", f"since {time.strftime('%H:%M', time.localtime(t.started))}", curses.color_pair(1) | curses.A_BOLD
                elif not t.enabled:
                    st, when, attr = "disabled", "-", curses.A_DIM
                elif ws(t) in paused_ws:
                    st, when, attr = "paused", "-", curses.A_DIM
                elif t.next_due is None:
                    st, when, attr = "idle", "manual", 0
                else:
                    st, when, attr = "idle", f"next in {fmt_delta(t.next_due - now)}", 0
                ex = "-" if t.last_exit is None else str(t.last_exit)
                if t.last_exit:
                    attr |= curses.color_pair(2)
                g = f"{ws(t) or '-':<{gw}}" if gw else ""
                mark = ">" if i == view else " "
                put(top + i, 0, f"{mark}[{i + 1:>{2 if len(shown) > 9 else 1}}] {t.name:<{nw}}{g}{st:<9}{when:<16}exit {ex:<4}{t.last_line}", attr)

        sep_y = top + len(shown)
        title = "all" if view < 0 else shown[view].name + ("" if shown[view].enabled else " [disabled]")
        if multi and view < 0 and tab:
            title += " in " + tab
        put(sep_y, 0, f"── log: {title} " + "─" * w)

        with lock:
            if view >= 0:
                src = [(stamp, None, line) for stamp, line in shown[view].lines]
            else:
                src = [(stamp, ws(t), line) for stamp, t, line in combined if tab is None or t in shown]
        body_h = h - sep_y - 2
        scroll = min(scroll, max(0, len(src) - body_h))
        end = len(src) - scroll
        tag_w = max(len(g) for g in spaces) + 2 if multi and tab is None and view < 0 else 0   # the all log names the workspace
        for j, (stamp, g, line) in enumerate(src[max(0, end - body_h):end]):
            put(sep_y + 1 + j, 0, f"{stamp} ")
            if tag_w:
                put(sep_y + 1 + j, 9, f"{g or '-':<{tag_w}}", color(g))
            put(sep_y + 1 + j, 9 + tag_w, line)

        ws_keys = "←→/e workspace  P pause workspace  " if multi else ""
        keys = f"1-{min(len(shown), 9)} run now  a/tab view  {ws_keys}p pause  d enable/disable  k kill  q quit  ↑↓ PgUp PgDn scroll  End follow"
        if quit_queued:
            n = sum(t.running for t in tasks)
            keys = (f"exit queued, no new tasks will start: waiting on {n} running "
                    "— q again = kill & quit now, x = quit now and leave them running")
        put(h - 1, 1, keys, curses.A_REVERSE)
        scr.refresh()

        k = scr.getch()
        if k == -1:
            continue
        if k == ord("q"):
            if quit_queued or not any(t.running for t in tasks):
                for t in tasks:
                    t.kill()
                return
            quit_queued = True
            continue
        if quit_queued and k == ord("x"):
            return
        if ord("1") <= k <= ord("9") and k - ord("1") < len(shown):
            if not quit_queued and shown[k - ord("1")].enabled:
                shown[k - ord("1")].start()
        elif k in (ord("a"), ord("\t")):
            view = view + 1 if view + 1 < len(shown) else -1
        elif k == ord("p"):
            paused = not paused
        elif multi and k in (ord("e"), curses.KEY_RIGHT, curses.KEY_LEFT):
            step = -1 if k == curses.KEY_LEFT else 1
            tab = tabs[(tabs.index(tab) + step) % len(tabs)]
            view, scroll = -1, 0
        elif k == ord("P"):  # with one workspace loaded too, so a pause left by a multi-project run can be undone
            g = tab or (ws(shown[view]) if view >= 0 else spaces[0] if len(spaces) == 1 else None)
            if g:
                with lock:
                    paused_ws ^= {g}
                    state["_paused_envs"] = sorted(paused_ws)
                    save_state()
        elif k == ord("d"):
            if view >= 0:
                t = shown[view]
                t.enabled = not t.enabled
                with lock:
                    dis = set(state.get("_disabled", []))
                    dis.discard(t.name) if t.enabled else dis.add(t.name)
                    state["_disabled"] = sorted(dis)
                    save_state()
        elif k == ord("k"):
            for t in (shown if view < 0 else [shown[view]]):
                t.kill()
        elif k == curses.KEY_UP:
            scroll += 1
        elif k == curses.KEY_DOWN:
            scroll = max(0, scroll - 1)
        elif k == curses.KEY_PPAGE:
            scroll += body_h
        elif k == curses.KEY_NPAGE:
            scroll = max(0, scroll - body_h)
        elif k == curses.KEY_END:
            scroll = 0


if __name__ == "__main__":
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    curses.wrapper(main)
