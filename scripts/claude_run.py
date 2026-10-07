#!/usr/bin/env python3
"""Run one headless `claude -p` session under a stall watchdog. Stdlib only.

  echo "$prompt" | claude_run.py LOG [--stall MIN] [--resumes N] -- claude [flags...]

The prompt comes from stdin; everything after `--` is the claude command and its flags
(no -p, no --output-format). The session streams its events to LOG.jsonl, so that file's
mtime is a heartbeat: tool calls, subagent progress and background-task events all touch
it. If it stays quiet for --stall minutes (default 30) the session is killed and resumed
with a nudge (`--resume <session id>`), at most --resumes times (default 1); a session
that stalls again is killed for good and the exit status is 124. LOG gets the final
result text (what `claude -p` prints by default), plus any non-JSON stderr lines, so
callers can tail and grep it as before.

Print mode normally gives background subagents 600s after the main turn ends and then
kills them. The watchdog replaces that ceiling: CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS
defaults to 0 here (wait for them), unless the caller sets it.

The session runs in its own process group (so the watchdog can kill it and its subagents
together), which means a signal sent to this script's group does not reach it. SIGTERM and
SIGINT are therefore forwarded: the session group gets TERM, then KILL after 15s, and this
script exits 128 + signal. Without that, killing a runner from sdlc.py left the session
running on and still moving tickets.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

NUDGE = ("You produced no output for {mins} minutes (last event: {last}). Check what is still "
         "running (subagents, background commands) and do not wait on it silently: finish it, "
         "or stop it and carry on without it. Then complete the original task. Anything you "
         "cannot finish gets a comment saying what blocked it and moves to Requires Discussion, "
         "so no ticket stays In Progress.")


def events(path):
    for line in open(path, errors="replace"):
        try:
            yield json.loads(line)
        except ValueError:
            yield {"_raw": line.rstrip("\n")}


def describe(ev):
    if "_raw" in ev:
        return "stderr"
    parts = [ev.get("type", "?"), ev.get("subtype")]
    for block in (ev.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            parts.append(block.get("name"))
    return " ".join(p for p in parts if p)


current = {"proc": None}   # the running session, for the signal handler


def stop(proc):
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def forward(signum, frame):
    proc = current["proc"]
    if proc is not None and proc.poll() is None:
        stop(proc)
    sys.exit(128 + signum)


def run_once(cmd, prompt, jsonl, stall_s, poll_s):
    """-> (exit code, stalled)"""
    with open(jsonl, "a") as out:
        proc = subprocess.Popen(cmd + ["-p", prompt, "--output-format", "stream-json", "--verbose"],
                                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        current["proc"] = proc
        while True:
            try:
                return proc.wait(timeout=poll_s), False
            except subprocess.TimeoutExpired:
                pass
            if time.time() - os.path.getmtime(jsonl) > stall_s:
                stop(proc)
                return proc.returncode, True


def write_text(log, jsonl):
    """LOG = the final result text, else whatever non-JSON noise the session printed."""
    evs = list(events(jsonl))
    results = [e["result"] for e in evs if e.get("type") == "result" and isinstance(e.get("result"), str)]
    noise = [e["_raw"] for e in evs if "_raw" in e]
    with open(log, "w") as f:
        f.write("\n".join(noise + results[-1:]) + "\n")
    return evs


def main():
    ap = argparse.ArgumentParser(usage="%(prog)s LOG [--stall MIN] [--resumes N] -- claude [flags...]")
    ap.add_argument("log")
    ap.add_argument("--stall", type=float, default=float(os.environ.get("STALL_MIN", 30)))
    ap.add_argument("--resumes", type=int, default=int(os.environ.get("STALL_RESUMES", 1)))
    ap.add_argument("cmd", nargs="+")
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    os.environ.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "0")
    jsonl = a.log + ".jsonl"
    open(jsonl, "w").close()
    prompt, resume = sys.stdin.read(), []
    poll_s = max(1.0, min(30.0, a.stall * 60 / 4))
    for attempt in range(a.resumes + 1):
        code, stalled = run_once(a.cmd + resume, prompt, jsonl, a.stall * 60, poll_s)
        evs = write_text(a.log, jsonl)
        if not stalled:
            return code
        last = describe(evs[-1]) if evs else "none"
        sid = next((e["session_id"] for e in reversed(evs) if e.get("session_id")), None)
        print(f"stalled {a.stall:g} min after: {last}", file=sys.stderr)
        if attempt == a.resumes or not sid:
            return 124
        print(f"resuming session {sid}", file=sys.stderr)
        resume, prompt = ["--resume", sid], NUDGE.format(mins=f"{a.stall:g}", last=last)
    return 124


if __name__ == "__main__":
    sys.exit(main())
