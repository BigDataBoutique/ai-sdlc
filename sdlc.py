#!/usr/bin/env python3
"""Run the SDLC tasks on a schedule and watch them. Stdlib only.

Each task is one of the headless scripts next to this file, run as a single pass
on its interval (or on a keypress). Their output goes to the screen and to
logs/sdlc.log. All other env (DRY_RUN, MODEL, CLAUDE_FLAGS, REPOS ...) passes
through to the scripts, so `DRY_RUN=1 ./sdlc.py` exercises everything for free.

Last finish times and disabled tasks live in .sdlc-cache.json, so a restart
resumes the schedule instead of running everything again.

Keys: 1-9 run a row now, a/Tab cycle the log view, p pause everything, d enable/disable the task
in view, k kill (the task in view, or every visible task), q quit, arrows/PgUp/PgDn/End scroll.
With several environments loaded (a project's `env:`, else its key), each row shows its group,
e cycles the view between all groups and each one, and P pauses the group in view (or the group
of the task in view): its tasks stop starting on schedule, run-now still works. Paused groups
survive restarts in .sdlc-cache.json.

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
                    combined.append((stamp, f"[{self.name}] {line}", self))
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
    grp = lambda t: t.env_name or t.project  # None = global, shown in every view
    groups = list(dict.fromkeys(grp(t) for t in tasks if grp(t)))
    multi = len(groups) > 1
    gview = None  # None = all groups, else the group shown
    view = -1  # -1 = all, else index into the visible tasks
    paused = False
    scroll = 0  # lines from the bottom; 0 = follow
    quit_queued = False  # q was pressed once: no new tasks, exit once running ones finish

    while True:
        now = time.time()
        paused_envs = set(state.get("_paused_envs", []))
        if not paused and not quit_queued:
            for t in tasks:
                if grp(t) not in paused_envs and t.enabled and not t.running and t.next_due is not None and t.next_due <= now:
                    t.start()
        if quit_queued and not any(t.running for t in tasks):
            return

        shown = [t for t in tasks if gview is None or grp(t) in (None, gview)]
        view = min(view, len(shown) - 1)
        h, w = scr.getmaxyx()
        scr.erase()

        def put(y, x, s, attr=0):
            if 0 <= y < h and x < w:
                scr.addnstr(y, x, s, w - x - 1, attr)

        head = f"SDLC  {os.path.basename(ROOT)}  {'PAUSED' if paused else 'auto'}"
        if multi:
            head += f"  env: {gview or 'all'}"
        hold = [g for g in groups if g in paused_envs]
        if hold:
            head += "  paused: " + ",".join(hold)
        put(0, 1, head, curses.A_BOLD)
        put(0, w - 10, time.strftime("%H:%M:%S"))
        nw = max(len(t.name) for t in tasks) + 2 if multi else 10
        gw = max(len(g) for g in groups) + 2 if multi else 0
        for i, t in enumerate(shown):
            with lock:
                if t.running:
                    st, when, attr = "running", f"since {time.strftime('%H:%M', time.localtime(t.started))}", curses.color_pair(1) | curses.A_BOLD
                elif not t.enabled:
                    st, when, attr = "disabled", "-", curses.A_DIM
                elif grp(t) in paused_envs:
                    st, when, attr = "paused", "-", curses.A_DIM
                elif t.next_due is None:
                    st, when, attr = "idle", "manual", 0
                else:
                    st, when, attr = "idle", f"next in {fmt_delta(t.next_due - now)}", 0
                ex = "-" if t.last_exit is None else str(t.last_exit)
                if t.last_exit:
                    attr |= curses.color_pair(2)
                g = f"{grp(t) or '-':<{gw}}" if multi else ""
                put(1 + i, 1, f"[{i + 1}] {t.name:<{nw}}{g}{st:<9}{when:<16}exit {ex:<4}{t.last_line}", attr)

        sep_y = 1 + len(shown)
        title = "all" if view < 0 else shown[view].name + ("" if shown[view].enabled else " [disabled]")
        if multi and view < 0 and gview:
            title += " in " + gview
        put(sep_y, 0, f"── log: {title} " + "─" * w)

        with lock:
            if view >= 0:
                src = list(shown[view].lines)
            else:
                src = [(stamp, line) for stamp, line, t in combined if gview is None or t in shown]
        body_h = h - sep_y - 2
        scroll = min(scroll, max(0, len(src) - body_h))
        end = len(src) - scroll
        for j, (stamp, line) in enumerate(src[max(0, end - body_h):end]):
            put(sep_y + 1 + j, 0, f"{stamp} {line}")

        env_keys = "e env  P pause env  " if multi else ""
        keys = f"1-{min(len(shown), 9)} run now  a/tab view  {env_keys}p pause  d enable/disable  k kill  q quit  ↑↓ PgUp PgDn scroll  End follow"
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
        elif multi and k == ord("e"):
            cycle = [None] + groups
            gview = cycle[(cycle.index(gview) + 1) % len(cycle)]
            view, scroll = -1, 0
        elif k == ord("P"):  # with one group loaded, so a pause left by a multi-project run can be undone
            g = gview or (grp(shown[view]) if view >= 0 else groups[0] if len(groups) == 1 else None)
            if g:
                with lock:
                    paused_envs ^= {g}
                    state["_paused_envs"] = sorted(paused_envs)
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
