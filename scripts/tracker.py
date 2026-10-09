#!/usr/bin/env python3
"""The ticket tracker behind the SDLC scripts and skills. Stdlib only.

One command line over two backends - Linear (GraphQL, personal API key) and GitHub Issues
(`gh api`) - so the runners list queues, move tickets between stages, set labels and
assignees, and read or create tickets without a Claude session in the loop. Which backend,
which team or repos, and which labels mean what comes from projects.yaml (or projects.json)
at the repo root, the files its top-level `include:` names, or the files in --config /
$SDLC_CONFIG, merged; names are resolved
to IDs at runtime, so no UUID lives in a script.

    tracker.py [--project P] [--json] me
    tracker.py list [--state STAGE|NAME] [--label L] [--cycle current] [--open] [--since DAYS] [--limit N] [--stage-since]
                    [--not-mine] [--unblocked]
    tracker.py get ID [--comments]
    tracker.py move ID STAGE|NAME
    tracker.py block ID --by OTHER
    tracker.py label ID [--add L]... [--remove L]...
    tracker.py assign ID (--me | --user EMAIL|LOGIN)
    tracker.py comment ID (--body TEXT | --body-file F)
    tracker.py create --title T (--body TEXT | --body-file F) [--state S] [--label L]... [--repo R] [--cycle current]
    tracker.py append ID (--body TEXT | --body-file F)
    tracker.py setup-labels                                (github only)
    tracker.py [--config FILE]... config projects|envs|env-name|name|org|host|context|repos-root|kind|queue-label|stages|repos|ticket-regex|ticket-example|design-labels|blocker-label|env|tasks|triage
                      |clone-url R|repo-for-label L|label-for-repo R
    tracker.py howto

Stages are the keys of `tracker.states` in projects.yaml (triage, design, todo, in_progress,
in_review, qa, discussion); a state name works everywhere a stage does. On GitHub a stage is a
label (`tracker.stage_labels`); a stage mapped to null is the *unlabeled* one, an open issue
carrying none of the stage labels, and two stages may share a label. `list` sorts by priority
(Urgent first, none last), then oldest created first: the order the implement pass works in.
`--stage-since` adds `stageSince`, when each ticket last entered its current stage (Linear's
state history, GitHub's label events), which is what reset-stale-tickets.sh ages tickets by.
`--not-mine` drops tickets the tracker user created; `--unblocked` drops tickets with an open
blocker, which `block` records through the tracker's own dependency (a GitHub issue dependency,
a Linear `blocks` relation) and `get` reports as `blockedBy`.

Env: LINEAR_API_KEY (or the same line in ./.env; https://linear.app/settings/api), SDLC_PROJECT
(default project when the config holds several), SDLC_CONFIG (config files, `:`-separated, env or ./.env),
SDLC_ENV (only the projects of that `env:`), REPOS_ROOT (where every project's clones live, overriding
`repos_root:`), DRY_RUN=1 (writes print `would:` and stop).
Exit 2 = usage, 1 = the tracker call failed.
"""
import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGES = ("triage", "design", "todo", "in_progress", "in_review", "qa", "discussion")
DEFAULT_STATES = {"triage": "Triage", "design": "Design", "todo": "Todo", "in_progress": "In Progress",
                  "in_review": "In Review", "qa": "QA", "discussion": "Requires Discussion"}
DEFAULT_STAGE_LABELS = {"triage": "stage:triage", "design": "stage:design", "todo": "stage:todo", "in_progress": "stage:in-progress",
                        "in_review": "stage:in-review", "qa": "stage:qa", "discussion": "stage:requires-discussion"}
# Built-in periodic tasks: name, runner (in ROOT), default interval in seconds, and the stage the
# project must name in its config for the task to apply (None: always). Project tasks: `tasks:`.
BUILTIN_TASKS = [("pull", "pull-all.sh", 3600, None), ("review", "review-prs.sh", 900, None),
                 ("update", "update-prs.sh", 1800, None), ("design", "design-tickets.sh", 1800, "design"),
                 ("implement", "implement-tickets.sh", 4 * 3600, "todo"), ("reset", "reset-stale-tickets.sh", 1800, "todo"),
                 ("triage", "triage-issues.sh", 1800, "triage")]
LINEAR_URL = "https://api.linear.app/graphql"
CONNECT_TIMEOUT = 10
PRIORITY_NAMES = {1: "urgent", 2: "high", 3: "medium", 4: "low"}


class _Https(http.client.HTTPSConnection):
    """Connect to the first address that answers, IPv4 first. A host with a global IPv6
    address but no working route otherwise sits through a full timeout per AAAA record before
    the stdlib tries IPv4 (two minutes per call against Linear); curl and gh race the families,
    http.client does not."""
    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
        infos.sort(key=lambda i: i[0] != socket.AF_INET)
        err = None
        for family, kind, proto, _, addr in infos:
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(CONNECT_TIMEOUT)
                sock.connect(addr)
                break
            except OSError as e:
                err = e
                sock.close()
        else:
            raise err or OSError("no address for %s" % self.host)
        sock.settimeout(self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class TrackerError(Exception):
    pass


def dry_run():
    return bool(os.environ.get("DRY_RUN"))


# ---------------------------------------------------------------- config

def _strip_comment(line):
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote and (quote == "'" or line[i - 1] != "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch; out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _split_top(s, sep=","):
    """Split on `sep` outside quotes and brackets."""
    parts, depth, quote, cur = [], 0, None, []
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch; cur.append(ch)
        elif ch in "[{":
            depth += 1; cur.append(ch)
        elif ch in "]}":
            depth -= 1; cur.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _split_key(s):
    """`key: value` / `key:` -> (key, rest) with the colon outside quotes, else None."""
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ":" and (i + 1 == len(s) or s[i + 1] in " \t"):
            return _scalar(s[:i].strip()), s[i + 1:].strip()
    return None


def _scalar(s):
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if s[0] == '"' and s[-1] == '"' and len(s) >= 2:
        return json.loads(s)
    if s[0] == "'" and s[-1] == "'" and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if s[0] == "{" and s[-1] == "}":
        out = {}
        for item in _split_top(s[1:-1]):
            kv = _split_key(item)
            if kv is None:
                raise TrackerError("unsupported YAML flow mapping item: %r" % item)
            out[kv[0]] = _scalar(kv[1])
        return out
    if s[0] == "[" and s[-1] == "]":
        return [_scalar(x) for x in _split_top(s[1:-1])]
    if s[0] in "&*!|>":
        raise TrackerError("unsupported YAML (%r): anchors, tags and block scalars are out of the "
                           "built-in subset; pip install pyyaml, or use projects.json" % s)
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", s):
        return float(s)
    return s


def parse_yaml(text):
    """The block subset projects.yaml uses: nested mappings, `- item` lists, one-line flow
    collections, plain/quoted scalars, comments. PyYAML is used instead when installed."""
    lines = []
    for raw in text.splitlines():
        if raw.strip() == "---":
            if lines:
                raise TrackerError("unsupported YAML: multiple documents")
            continue
        s = _strip_comment(raw.replace("\t", "  "))
        if s.strip():
            lines.append((len(s) - len(s.lstrip(" ")), s.strip()))
    if not lines:
        return {}
    value, i = _parse_block(lines, 0, lines[0][0])
    if i != len(lines):
        raise TrackerError("unsupported YAML near line: %r" % lines[i][1])
    return value


def _parse_block(lines, i, indent):
    if lines[i][1] == "-" or lines[i][1].startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- ") and lines[i][1] != "-":
        kv = _split_key(lines[i][1])
        if kv is None:
            raise TrackerError("unsupported YAML line: %r" % lines[i][1])
        key, rest = kv
        i += 1
        if rest:
            out[key] = _scalar(rest)
        elif i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][1].startswith("- "))):
            out[key], i = _parse_block(lines, i, lines[i][0])
        else:
            out[key] = None
    if i < len(lines) and lines[i][0] > indent:
        raise TrackerError("unsupported YAML indentation near: %r" % lines[i][1])
    return out, i


def _parse_list(lines, i, indent):
    out = []
    while i < len(lines) and lines[i][0] == indent and (lines[i][1] == "-" or lines[i][1].startswith("- ")):
        item = lines[i][1][1:].strip()
        if not item:
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                v, i = _parse_block(lines, i, lines[i][0])
            else:
                v = None
        elif item == "-" or item.startswith("- "):
            lines[i] = (indent + 2, item)   # `- - x` is a list indented two past the dash
            v, i = _parse_list(lines, i, indent + 2)
        elif _split_key(item) is not None and not item.startswith(("{", "[", '"', "'")):
            lines[i] = (indent + 2, item)   # `- k: v` is a mapping indented two past the dash
            v, i = _parse_map(lines, i, indent + 2)
        else:
            v = _scalar(item); i += 1
        out.append(v)
    return out, i


CONFIG_NAMES = ("projects.yaml", "projects.yml", "projects.json")


def config_paths(paths=None, root=None):
    """The project files to read: `paths` (--config), else SDLC_CONFIG (os.pathsep-separated, from
    the environment or `root`/.env), else the first projects.yaml|yml|json in `root`. Relative paths
    are relative to ROOT, so a link under contexts/ (contexts/<key>.yaml) works from any cwd."""
    root = root or ROOT
    paths = paths or [p for p in (read_env_key("SDLC_CONFIG", root) or "").split(os.pathsep) if p]
    if paths:
        return [os.path.join(ROOT, p) for p in paths]
    for name in CONFIG_NAMES:
        if os.path.exists(os.path.join(root, name)):
            return [os.path.join(root, name)]
    raise TrackerError("no projects.yaml (or .yml/.json) in %s" % root)


def _read_config(path):
    if not os.path.exists(path):
        raise TrackerError("no project config at %s" % path)
    with open(path) as f:
        text = f.read()
    if path.endswith(".json"):
        data = json.loads(text)
    else:
        try:
            import yaml  # noqa: optional; the subset parser below covers our own file
            data = yaml.safe_load(text) or {}
        except ImportError:
            data = parse_yaml(text)
    if not isinstance(data, dict):
        raise TrackerError("%s: expected one block per project" % path)
    return data


def _first_diff(a, b, at=""):
    """The dotted key path where two config values first differ, or None when they are equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            d = _first_diff(a.get(k), b.get(k), "%s.%s" % (at, k) if at else str(k))
            if d is not None:
                return d
        return None
    return None if a == b else at


def load_config(root=None, paths=None):
    """{project: block} merged from every config file: the ones named, plus what their top-level
    `include:` lists (a path or a list of paths, relative to the including file's real folder;
    a file is read once). A project may appear in several files only with identical blocks, and a
    repo may belong to one project only. Each block gets `_dir`, the real folder of the file that
    defined it (a symlink is followed, so a link under contexts/ to another hub's file still finds
    that hub), which a relative `context:` and the default repos root resolve against."""
    merged, source, seen = {}, {}, set()
    queue = list(config_paths(paths, root))
    while queue:
        path = queue.pop(0)
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        data = dict(_read_config(path))
        env = data.pop("env", None)   # a file-level `env: <name>`: the default for its projects
        if env is not None and not isinstance(env, str):
            raise TrackerError("%s: top-level `env` is the file's environment name; a project can't be called env" % path)
        include = data.pop("include", None) or []
        include = [include] if isinstance(include, str) else include
        if not all(isinstance(i, str) for i in include):
            raise TrackerError("%s: top-level `include` lists other config files; a project can't be called include" % path)
        queue[:0] = [os.path.join(os.path.dirname(real), i) for i in include]
        for key, block in data.items():
            if env and isinstance(block, dict) and not block.get("env"):
                block = dict(block, env=env)
            if key in merged:
                d = _first_diff(merged[key], block)
                if d is not None:
                    raise TrackerError("project %r differs between %s and %s (at %s)" % (key, source[key], path, d or "the top"))
                continue
            merged[key], source[key] = block, path
    owner = {}
    for key, block in merged.items():
        forge = block.get("forge") or {}
        for repo in block.get("repos") or {}:
            r = "%s/%s/%s" % (forge.get("host") or "github.com", forge.get("org"), repo)
            if r in owner:
                raise TrackerError("%s belongs to both %r and %r; a repo can be in one project only" % (r, owner[r], key))
            owner[r] = key
    return {k: dict(b, _dir=os.path.dirname(os.path.realpath(source[k]))) for k, b in merged.items()}


def in_env(config, env=None):
    """The projects of one environment (`env:` of the project or its file), all when env is empty."""
    env = env if env is not None else os.environ.get("SDLC_ENV", "")
    if not env:
        return config
    picked = {k: b for k, b in config.items() if (b.get("env") or "") == env}
    if not picked:
        envs = sorted({b.get("env") for b in config.values() if b.get("env")})
        raise TrackerError("no project in environment %r (have: %s)" % (env, ", ".join(envs) or "none named"))
    return picked


def pick_project(config, name=None):
    name = name or os.environ.get("SDLC_PROJECT")
    if not name:
        config = in_env(config)
    if name:
        if name not in config:
            raise TrackerError("no project %r in the project config (have: %s)" % (name, ", ".join(config)))
        return name, config[name]
    if len(config) == 1:
        return next(iter(config.items()))
    raise TrackerError("the project config holds several projects (%s); pass --project or set SDLC_PROJECT" % ", ".join(config))


def read_env_key(name, root=None):
    """A value from the environment, else from ./.env (KEY=VALUE, optional quotes, # comments)."""
    root = root or ROOT
    if os.environ.get(name):
        return os.environ[name]
    path = os.path.join(root, ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    if v:
                        return v
    return None


# ---------------------------------------------------------------- common

def priority_key(t):
    p = t.get("priority") or 0
    return (p if 1 <= p <= 4 else 5, t.get("createdAt") or "")


def since_iso(days):
    import datetime
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds(v):
    """An interval: plain seconds, or a number with s, m, h or d."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(v))
    if not m:
        raise TrackerError("not an interval: %r (use seconds, or 10m, 4h, 1d)" % (v,))
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def slugify(title, limit=50):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:limit].rstrip("-") or "ticket"


class Tracker:
    kind = ""

    def __init__(self, project, cfg):
        self.project, self.cfg = project, cfg
        self.forge = cfg.get("forge") or {}
        self.tcfg = cfg.get("tracker") or {}
        self.repos = cfg.get("repos") or {}
        self.states = dict(DEFAULT_STATES, **(self.tcfg.get("states") or {}))
        self.queue_label = self.tcfg.get("queue_label") or ""   # empty = no queue label: the stage alone is the queue

    # -- config lookups shared by both backends
    def state_name(self, stage_or_name):
        if stage_or_name in self.states:
            return self.states[stage_or_name]
        for name in self.states.values():
            if name.lower() == stage_or_name.lower():
                return name
        return stage_or_name

    def stage_of(self, state_name):
        for k, v in self.states.items():
            if v.lower() == (state_name or "").lower():
                return k
        return None

    def org(self):
        return self.forge.get("org", "")

    def host(self):
        return self.forge.get("host") or "github.com"

    def display_name(self):
        return self.cfg.get("name") or self.project

    def env_name(self):
        """The environment this project belongs to (`env:`), empty when none is named."""
        return self.cfg.get("env") or ""

    def context_dir(self):
        """The folder holding the project's own docs (design, change chains, local dev)."""
        return os.path.join(self.cfg.get("_dir") or ROOT, self.cfg.get("context") or os.path.join("contexts", self.project))

    def repos_root(self):
        """Where this project's clones live: $REPOS_ROOT (every project, e.g. from a worktree of
        ai-sdlc), else `repos_root:` relative to the config file's folder, else that folder's parent:
        the clones sit beside the hub that defines the project."""
        here = self.cfg.get("_dir") or ROOT
        if os.environ.get("REPOS_ROOT"):
            return os.path.abspath(os.environ["REPOS_ROOT"])
        return os.path.normpath(os.path.join(here, self.cfg.get("repos_root") or ".."))

    def clone_url(self, repo):
        return "https://%s/%s/%s.git" % (self.host(), self.org(), repo)

    def design_labels(self):
        """`tracker.design_labels`: {group: [labels]} the design pass may add besides the repo label."""
        return self.tcfg.get("design_labels") or {}

    def shell_env(self):
        """KEY=value lines for scripts/env.sh to eval: everything a runner needs from the config."""
        import shlex
        vals = [("PROJECT", self.project), ("ENV_NAME", self.env_name()), ("NAME", self.display_name()), ("ORG", self.org()), ("HOST", self.host()), ("CONTEXT", self.context_dir()),
                ("REPOS_ROOT", self.repos_root()), ("KIND", self.kind), ("QUEUE", self.queue_label), ("BLOCKER", self.blocker_label()),
                ("EXAMPLE", self.ticket_example())]
        return "\n".join("%s=%s" % (k, shlex.quote(v)) for k, v in vals)

    def blocker_label(self):
        return self.tcfg.get("blocker_label") or ""

    def exclude_labels(self):
        return [l.lower() for l in self.tcfg.get("exclude_labels") or []]

    def ready_labels(self):
        """`tracker.ready_labels`: [[labels]...]; a ticket is ready (todo) when it carries every
        label of at least one group, e.g. [[feature, approved], [bug]]. Empty = no extra filter."""
        return [{str(l).lower() for l in (g if isinstance(g, list) else [g])} for g in self.tcfg.get("ready_labels") or []]

    def stage_configured(self, stage):
        """Whether the config names this stage itself, rather than leaving it to the defaults."""
        return stage in (self.tcfg.get("states") or {}) or stage in (self.tcfg.get("stage_labels") or {})

    def triage_config(self):
        """`tracker.triage`: outcomes ([[labels]...], a triaged issue carries every label of one group),
        labels (others the pass may add) and next (the stage it hands issues to, default todo)."""
        c = self.tcfg.get("triage") or {}
        return {"outcomes": [g if isinstance(g, list) else [g] for g in c.get("outcomes") or []],
                "labels": list(c.get("labels") or []), "next": c.get("next") or "todo",
                "guide": os.path.join(self.context_dir(), "triage.md")}

    def tasks(self):
        """What sdlc.py may schedule for this project: [{name, script, every, builtin, scope, skip}].
        `every` is the wait after a pass ends before the next starts: the default, or `tasks.<name>.every`.
        `scope` is global for pull (it fetches every project's repos at once). `skip` says why a task
        does not apply (a stage or config it needs is missing), else None."""
        stages_key = "stage_labels" if self.kind == "github" else "states"
        own = self.cfg.get("tasks") or {}
        out = []
        for name, script, every, stage in BUILTIN_TASKS:
            c = own.get(name) or {}
            if c.get("script"):
                raise TrackerError("projects.yaml: %s: %r is a built-in task; set only its `every`, or give the script another name" % (self.project, name))
            every = seconds(c["every"]) if "every" in c else every
            skip = None
            if stage and not self.stage_configured(stage):
                skip = "no %s stage in tracker.%s" % (stage, stages_key)
            elif name == "triage" and not self.triage_config()["outcomes"]:
                skip = "no tracker.triage.outcomes"
            elif name == "triage" and not os.path.exists(self.triage_config()["guide"]):
                skip = "no triage.md in the context folder"
            out.append({"name": name, "script": os.path.join(ROOT, script), "every": every, "builtin": True, "skip": skip,
                        "scope": "global" if name == "pull" else "project"})
        for name, c in own.items():
            if name in {b[0] for b in BUILTIN_TASKS}:
                continue
            if not (c or {}).get("script"):
                raise TrackerError("projects.yaml: %s: task %r needs a script (relative to the context folder)" % (self.project, name))
            script = os.path.join(self.context_dir(), c["script"])
            out.append({"name": name, "script": script, "every": seconds(c.get("every", 3600)), "builtin": False, "scope": "project",
                        "skip": None if os.access(script, os.X_OK) else "no executable %s" % script})
        return out

    def not_mine(self, tickets):
        """`list --not-mine`: the tickets someone other than the tracker user created."""
        me = self.me()["id"]
        return [t for t in tickets if (t.get("creator") or {}).get("id") != me]

    def repo_names(self):
        return list(self.repos)

    def label_for_repo(self, repo):
        return (self.repos.get(repo) or {}).get("label")

    def repo_for_label(self, label):
        for repo, c in self.repos.items():
            if (c or {}).get("label", "").lower() == (label or "").lower():
                return repo
        return None

    def repos_for_labels(self, labels):
        return [r for r in (self.repo_for_label(l) for l in labels) if r]

    def repo_labels(self):
        return {r: c["label"] for r, c in self.repos.items() if (c or {}).get("label")}

    # -- backend API (see subclasses)
    def me(self): raise NotImplementedError
    def list(self, state=None, label=None, open_only=False, since_days=None, limit=None, stage_since=False, cycle=None, unblocked=False): raise NotImplementedError
    def get(self, ident, comments=False): raise NotImplementedError
    def move(self, ident, stage_or_name): raise NotImplementedError
    def block(self, ident, by): raise NotImplementedError
    def set_labels(self, ident, add=(), remove=()): raise NotImplementedError
    def assign(self, ident, user=None, me=False): raise NotImplementedError
    def comment(self, ident, body): raise NotImplementedError
    def create(self, title, body, state=None, labels=(), repo=None, cycle=None): raise NotImplementedError
    def append(self, ident, text): raise NotImplementedError
    def ticket_regex(self): raise NotImplementedError
    def ticket_example(self): raise NotImplementedError
    def howto(self): raise NotImplementedError
    def setup_labels(self): raise TrackerError("setup-labels only applies to a github tracker")

    def _howto_common(self, cli):
        lines = ["Mechanical ticket bookkeeping goes through the tracker CLI (deterministic, no MCP):",
                 "  %s move <ID> <%s>" % (cli, "|".join(STAGES)),
                 "  %s block <ID> --by <OTHER>   # the tracker's own dependency; a blocked ticket waits until OTHER is closed" % cli,
                 "  %s label <ID> --add \"<label>\" --remove \"<label>\"" % cli,
                 "  %s assign <ID> --me" % cli,
                 "  %s create --title \"...\" --body \"<text>\" --state todo%s%s%s"
                 % (cli, " --cycle current" if self.kind == "linear" else "",
                    ' --label "%s"' % self.queue_label if self.queue_label else "",
                    " --repo <repo>" if self.kind == "github" else ' --label "<repo label>"'),
                 "  %s get <ID> --comments   # the ticket, its labels, target repos and comments" % cli,
                 "  %s comment <ID> --body \"<text>\"   # --body-file FILE works everywhere --body does" % cli,
                 "Stages: " + ", ".join("%s = %s" % (k, v) for k, v in self.states.items()) + "."]
        if self.queue_label:
            lines.append("Work queue: tickets in Todo carrying the label \"%s\"." % self.queue_label)
        rl = self.repo_labels()
        if rl:
            lines.append("Repo labels: " + ", ".join("%s -> %s" % (v, k) for k, v in rl.items()) + ".")
        return lines


# ---------------------------------------------------------------- Linear

ISSUE_FIELDS = """
  id identifier title description url priority branchName createdAt updatedAt
  state { name type } labels { nodes { name } } team { key }
  assignee { id name displayName email } creator { id name displayName email }
"""
# An inverse `blocks` relation is what blocks this issue: Linear stores "A blocks B" on A, so B
# sees it through inverseRelations. Its state type says whether the blocker is still open.
ISSUE_BLOCKERS = """
  inverseRelations { nodes { type issue { identifier title state { type } } } }
"""
ISSUE_EXTRA = """
  parent { identifier title } children { nodes { identifier title } }
  attachments { nodes { title url } }
  relations { nodes { type relatedIssue { identifier title } } }
""" + ISSUE_BLOCKERS
ISSUE_COMMENTS = """
  comments { nodes { id body createdAt user { name displayName email } } }
"""
ISSUE_HISTORY = """
  startedAt history(first: 100) { nodes { createdAt toState { name } } }
"""


class LinearTracker(Tracker):
    kind = "linear"

    def __init__(self, project, cfg, transport=None, api_key=None):
        super().__init__(project, cfg)
        self.team_query = self.tcfg.get("team")
        if not self.team_query:
            raise TrackerError("projects.yaml: tracker.team is required for a linear tracker")
        self._transport = transport or self._http
        self._key = api_key
        self._team = None
        self._labels = {}
        self._me = None

    # -- transport
    def _http(self, query, variables):
        if self._key is None:
            self._key = read_env_key("LINEAR_API_KEY")
            if not self._key:
                raise TrackerError("LINEAR_API_KEY is not set: put it in %s/.env (see .env.example), "
                                   "from https://linear.app/settings/api" % ROOT)
        u = urllib.parse.urlsplit(LINEAR_URL)
        conn = _Https(u.hostname, u.port, timeout=60)
        try:
            conn.request("POST", u.path, json.dumps({"query": query, "variables": variables}),
                         {"Content-Type": "application/json", "Authorization": self._key})
            resp = conn.getresponse()
            body = resp.read().decode(errors="replace")
        except OSError as e:
            raise TrackerError("Linear unreachable: %s" % e)
        finally:
            conn.close()
        if resp.status != 200:
            raise TrackerError("Linear HTTP %s: %s" % (resp.status, body[:500]))
        return json.loads(body)

    def gql(self, query, **variables):
        resp = self._transport(query, variables)
        if resp.get("errors"):
            raise TrackerError("Linear: " + "; ".join(e.get("message", str(e)) for e in resp["errors"]))
        return resp.get("data") or {}

    # -- name -> id
    def team(self):
        if self._team is None:
            nodes = self.gql("query { teams(first: 100) { nodes { id name key states { nodes { id name type } } } } }")["teams"]["nodes"]
            q = self.team_query.lower()
            match = [t for t in nodes if t["name"].lower() == q or t["key"].lower() == q]
            if not match:
                raise TrackerError("no Linear team %r (have: %s)" % (self.team_query, ", ".join(t["name"] for t in nodes)))
            self._team = match[0]
        return self._team

    def state_id(self, stage_or_name):
        name = self.state_name(stage_or_name)
        for s in self.team()["states"]["nodes"]:
            if s["name"].lower() == name.lower():
                return s["id"]
        raise TrackerError("no state %r on team %s (have: %s)" % (
            name, self.team()["name"], ", ".join(s["name"] for s in self.team()["states"]["nodes"])))

    def label_id(self, name):
        if name not in self._labels:
            nodes = self.gql("query($n: String!) { issueLabels(filter: { name: { eqIgnoreCase: $n } }, first: 20) "
                             "{ nodes { id name team { id } } } }", n=name)["issueLabels"]["nodes"]
            tid = self.team()["id"]
            nodes.sort(key=lambda l: 0 if (l.get("team") or {}).get("id") == tid else (1 if not l.get("team") else 2))
            if not nodes or (nodes[0].get("team") and nodes[0]["team"]["id"] != tid):
                raise TrackerError("no Linear label %r on team %s or the workspace" % (name, self.team()["name"]))
            self._labels[name] = nodes[0]["id"]
        return self._labels[name]

    def user_id(self, query):
        if query == "me":
            return self.me()["id"]
        nodes = self.gql("query($q: String!) { users(filter: { or: [ { email: { eq: $q } }, { name: { eqIgnoreCase: $q } }, "
                         "{ displayName: { eqIgnoreCase: $q } } ] }, first: 5) { nodes { id name email } } }", q=query)["users"]["nodes"]
        if len(nodes) != 1:
            raise TrackerError("user %r matched %d Linear users" % (query, len(nodes)))
        return nodes[0]["id"]

    # -- normalize
    def _ticket(self, n):
        labels = [l["name"] for l in (n.get("labels") or {}).get("nodes", [])]
        st = n.get("state") or {}
        t = {"id": n["id"], "identifier": n["identifier"], "title": n.get("title"), "url": n.get("url"),
             "description": n.get("description") or "", "state": st.get("name"), "stage": self.stage_of(st.get("name")),
             "stateType": st.get("type"), "labels": labels, "repos": self.repos_for_labels(labels),
             "priority": n.get("priority") or 0, "priorityName": PRIORITY_NAMES.get(n.get("priority") or 0),
             "branchName": n.get("branchName"), "createdAt": n.get("createdAt"), "updatedAt": n.get("updatedAt"),
             "assignee": self._user(n.get("assignee")), "creator": self._user(n.get("creator"))}
        if "inverseRelations" in n:
            t["blockedBy"] = [{"identifier": r["issue"]["identifier"], "title": r["issue"].get("title"),
                               "open": ((r["issue"].get("state") or {}).get("type")) not in ("completed", "canceled")}
                              for r in (n.get("inverseRelations") or {}).get("nodes", [])
                              if r.get("type") == "blocks" and r.get("issue")]
        if "attachments" in n:
            t["parent"] = n.get("parent")
            t["children"] = (n.get("children") or {}).get("nodes", [])
            t["attachments"] = (n.get("attachments") or {}).get("nodes", [])
            rel = [{"type": r["type"], "issue": r["relatedIssue"]["identifier"], "title": r["relatedIssue"]["title"]}
                   for r in (n.get("relations") or {}).get("nodes", []) if r.get("relatedIssue")]
            rel += [{"type": r["type"], "issue": r["issue"]["identifier"], "title": r["issue"]["title"], "inverse": True}
                    for r in (n.get("inverseRelations") or {}).get("nodes", []) if r.get("issue")]
            t["relations"] = rel
        if "comments" in n:
            t["comments"] = [{"id": c["id"], "body": c["body"], "createdAt": c["createdAt"],
                              "author": self._user(c.get("user"))} for c in n["comments"]["nodes"]]
        if "history" in n:
            t["stageSince"] = self._stage_since(n)
        return t

    @staticmethod
    def _stage_since(n):
        """When the issue last entered its current state: the newest history entry into it;
        for a history longer than the page fetched, startedAt while in a started state, else
        updatedAt."""
        st = n.get("state") or {}
        into = [h["createdAt"] for h in (n.get("history") or {}).get("nodes", [])
                if (h.get("toState") or {}).get("name") == st.get("name")]
        if into:
            return max(into)
        if st.get("type") == "started" and n.get("startedAt"):
            return n["startedAt"]
        return n.get("updatedAt")

    @staticmethod
    def _user(u):
        if not u:
            return None
        return {"id": u.get("id"), "name": u.get("name"), "displayName": u.get("displayName"), "email": u.get("email")}

    # -- API
    def me(self):
        if self._me is None:
            v = self.gql("query { viewer { id name displayName email } }")["viewer"]
            self._me = self._user(v)
        return self._me

    def list(self, state=None, label=None, open_only=False, since_days=None, limit=None, stage_since=False, cycle=None, unblocked=False):
        f = {"team": {"id": {"eq": self.team()["id"]}}}
        fields = ISSUE_FIELDS + (ISSUE_HISTORY if stage_since else "") + (ISSUE_BLOCKERS if unblocked else "")
        if state:
            f["state"] = {"name": {"eq": self.state_name(state)}}
        elif open_only:
            f["state"] = {"type": {"nin": ["completed", "canceled"]}}
        if label:
            f["labels"] = {"name": {"eq": label}}
        if cycle:
            f["cycle"] = {"id": {"eq": self.cycle_id(cycle)}}
        if since_days:
            f["updatedAt"] = {"gte": since_iso(since_days)}
        out, after = [], None
        while True:
            d = self.gql("query($f: IssueFilter, $a: String) { issues(filter: $f, first: 50, after: $a, orderBy: updatedAt) "
                         "{ nodes { %s } pageInfo { hasNextPage endCursor } } }" % fields, f=f, a=after)["issues"]
            out += [self._ticket(n) for n in d["nodes"]]
            if not d["pageInfo"]["hasNextPage"]:
                break
            after = d["pageInfo"]["endCursor"]
        if unblocked:
            out = [t for t in out if not any(b["open"] for b in t.get("blockedBy") or [])]
        out.sort(key=priority_key)
        return out[:limit] if limit else out

    def get(self, ident, comments=False):
        fields = ISSUE_FIELDS + ISSUE_EXTRA + (ISSUE_COMMENTS if comments else "")
        d = self.gql("query($id: String!) { issue(id: $id) { %s } }" % fields, id=ident)
        if not d.get("issue"):
            raise TrackerError("no Linear issue %r" % ident)
        return self._ticket(d["issue"])

    def _update(self, ident, what, **input):
        if dry_run():
            print("would: %s %s %s" % (what, ident, json.dumps(input)))
            return None
        d = self.gql("mutation($id: String!, $in: IssueUpdateInput!) { issueUpdate(id: $id, input: $in) "
                     "{ success issue { %s } } }" % ISSUE_FIELDS, id=ident, **{"in": input})["issueUpdate"]
        if not d.get("success"):
            raise TrackerError("issueUpdate %s failed" % ident)
        return self._ticket(d["issue"])

    def move(self, ident, stage_or_name):
        return self._update(ident, "move to %s" % self.state_name(stage_or_name), stateId=self.state_id(stage_or_name))

    def block(self, ident, by):
        """Record `by` blocks `ident` as a Linear relation ("A blocks B" lives on A)."""
        blocker, blocked = self.get(by), self.get(ident)
        if dry_run():
            print("would: block %s by %s" % (ident, by))
            return None
        d = self.gql("mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }",
                     **{"in": {"issueId": blocker["id"], "relatedIssueId": blocked["id"], "type": "blocks"}})["issueRelationCreate"]
        if not d.get("success"):
            raise TrackerError("issueRelationCreate %s blocks %s failed" % (by, ident))
        return self.get(ident)

    def set_labels(self, ident, add=(), remove=()):
        input = {}
        if add:
            input["addedLabelIds"] = [self.label_id(l) for l in add]
        if remove:
            input["removedLabelIds"] = [self.label_id(l) for l in remove]
        if not input:
            raise TrackerError("nothing to add or remove")
        return self._update(ident, "labels +%s -%s" % (list(add), list(remove)), **input)

    def assign(self, ident, user=None, me=False):
        uid = self.me()["id"] if me else self.user_id(user)
        return self._update(ident, "assign to %s" % ("me" if me else user), assigneeId=uid)

    def comment(self, ident, body):
        issue = self.get(ident)
        if dry_run():
            print("would: comment on %s (%d chars)" % (ident, len(body)))
            return None
        d = self.gql("mutation($in: CommentCreateInput!) { commentCreate(input: $in) { success comment { id url } } }",
                     **{"in": {"issueId": issue["id"], "body": body}})["commentCreate"]
        if not d.get("success"):
            raise TrackerError("commentCreate on %s failed" % ident)
        return d["comment"]

    def cycle_id(self, which):
        if which != "current":
            raise TrackerError("--cycle takes only 'current', got %r" % which)
        c = self.gql("query($id: String!) { team(id: $id) { activeCycle { id } } }", id=self.team()["id"])["team"]["activeCycle"]
        if not c:
            raise TrackerError("team %s has no active cycle" % self.team()["name"])
        return c["id"]

    def create(self, title, body, state=None, labels=(), repo=None, cycle=None):
        input = {"teamId": self.team()["id"], "title": title, "description": body or ""}
        if state:
            input["stateId"] = self.state_id(state)
        if labels:
            input["labelIds"] = [self.label_id(l) for l in labels]
        if cycle:
            input["cycleId"] = self.cycle_id(cycle)
        if dry_run():
            print("would: create %r state=%s labels=%s cycle=%s" % (title, state, list(labels), cycle))
            return None
        d = self.gql("mutation($in: IssueCreateInput!) { issueCreate(input: $in) { success issue { %s } } }" % ISSUE_FIELDS,
                     **{"in": input})["issueCreate"]
        if not d.get("success"):
            raise TrackerError("issueCreate %r failed" % title)
        return self._ticket(d["issue"])

    def append(self, ident, text):
        before = self.get(ident)
        old = before["description"]
        new = (old.rstrip() + "\n\n" + text.strip() + "\n") if old.strip() else text.strip() + "\n"
        t = self._update(ident, "append %d chars to the description of" % len(text), description=new)
        if t is not None and not t["description"].startswith(old.rstrip()):
            raise TrackerError("%s: description no longer starts with the original text after the append" % ident)
        return t

    def team_key(self):
        return self.tcfg.get("key") or self.team()["key"]   # `key: ABC` in projects.yaml saves the call

    def ticket_regex(self):
        return r"\b%s-[0-9]+\b" % re.escape(self.team_key())

    def ticket_example(self):
        return "%s-123" % self.team_key()

    def howto(self):
        cli = os.path.join(ROOT, "scripts", "tracker.py")
        lines = ["Ticket tracker: Linear, team %s. Ticket IDs look like %s-123." % (self.team_query, self.team_key()),
                 "Read a ticket, its relations and comments through the linear-server MCP (get_issue with includeRelations, "
                 "list_comments) or `%s get <ID> --comments`. Post comments with the MCP (save_comment) or the CLI." % cli]
        return "\n".join(lines + self._howto_common(cli))


# ---------------------------------------------------------------- GitHub Issues

class GitHubTracker(Tracker):
    kind = "github"

    def __init__(self, project, cfg, runner=None):
        super().__init__(project, cfg)
        if not self.org():
            raise TrackerError("projects.yaml: forge.org is required for a github tracker")
        # stage -> label; None = the unlabeled stage (an open issue with no stage label at all)
        self.stage_labels = dict(DEFAULT_STAGE_LABELS, **(self.tcfg.get("stage_labels") or {}))
        if sum(1 for v in self.stage_labels.values() if v is None) > 1:
            raise TrackerError("projects.yaml: at most one stage may be unlabeled (null) in tracker.stage_labels")
        self._run_env = dict(os.environ, GH_HOST=self.host()) if self.host() != "github.com" else None
        self.priority_labels = self.tcfg.get("priority_labels") or {
            "priority:urgent": 1, "priority:high": 2, "priority:medium": 3, "priority:low": 4}
        self._run = runner or (lambda args, input=None: self._gh(args, input, self._run_env))
        self._me = None

    # -- transport
    @staticmethod
    def _gh(args, input=None, env=None):
        cmd = ["gh", "api"] + args
        p = subprocess.run(cmd, input=input, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            raise TrackerError("gh api %s: %s" % (" ".join(args[1:3]), (p.stderr or p.stdout).strip()[:500]))
        return json.loads(p.stdout) if p.stdout.strip() else None

    def api(self, path, method="GET", body=None, params=None, paginate=False):
        args = ["-X", method, path, "-H", "Accept: application/vnd.github+json"]
        for k, v in (params or {}).items():
            args += ["-f", "%s=%s" % (k, v)]
        if paginate:
            args += ["--paginate", "--slurp"]
        input = json.dumps(body) if body is not None else None
        if input is not None:
            args += ["--input", "-"]
        out = self._run(args, input)
        if paginate and isinstance(out, list) and out and isinstance(out[0], list):
            out = [x for page in out for x in page]
        return out

    # -- naming
    def issue_repos(self):
        return [r for r, c in self.repos.items() if (c or {}).get("issues", True)]

    def parse_ident(self, ident):
        m = re.fullmatch(r"(?:https://github\.com/([^/]+)/([^/]+)/issues/(\d+))", ident.strip())
        if m:
            return m.group(2), int(m.group(3))
        m = re.fullmatch(r"(?:([A-Za-z0-9_.-]+))?#(\d+)", ident.strip())
        if not m:
            raise TrackerError("not a GitHub issue reference: %r (use repo#N)" % ident)
        repo = m.group(1)
        if not repo:
            repos = self.issue_repos()
            if len(repos) != 1:
                raise TrackerError("%r is ambiguous: say which repo, as repo#N" % ident)
            repo = repos[0]
        return repo, int(m.group(2))

    def _path(self, ident, suffix=""):
        repo, n = self.parse_ident(ident)
        return "repos/%s/%s/issues/%d%s" % (self.org(), repo, n, suffix), repo, n

    # -- normalize
    def _ticket(self, repo, n, comments=None):
        labels = [l["name"] if isinstance(l, dict) else l for l in n.get("labels") or []]
        stage = next((k for k, v in self.stage_labels.items() if v and v in labels), None)
        if stage is None and n.get("state") == "open":
            stage = next((k for k, v in self.stage_labels.items() if v is None), None)
        state = self.states[stage] if stage else n.get("state")
        prio = next((p for l, p in self.priority_labels.items() if l in labels), 0)
        t = {"id": n.get("node_id"), "identifier": "%s#%d" % (repo, n["number"]), "number": n["number"], "repo": repo,
             "title": n.get("title"), "url": n.get("html_url"), "description": n.get("body") or "",
             "state": state, "stage": stage, "stateType": n.get("state"), "labels": labels, "repos": [repo],
             "priority": prio, "priorityName": PRIORITY_NAMES.get(prio),
             "branchName": "%d-%s" % (n["number"], slugify(n.get("title"))),
             "createdAt": n.get("created_at"), "updatedAt": n.get("updated_at"),
             "assignee": self._user((n.get("assignees") or [None])[0] if n.get("assignees") else n.get("assignee")),
             "creator": self._user(n.get("user"))}
        if comments is not None:
            t["comments"] = [{"id": c["id"], "body": c.get("body") or "", "createdAt": c.get("created_at"),
                              "author": self._user(c.get("user"))} for c in comments]
            t["attachments"], t["relations"], t["parent"], t["children"] = [], [], None, []
        return t

    @staticmethod
    def _user(u):
        if not u:
            return None
        return {"id": u.get("node_id"), "login": u.get("login"), "name": u.get("name"), "email": u.get("email")}

    # -- API
    def me(self):
        if self._me is None:
            self._me = self._user(self.api("user"))
        return self._me

    def list(self, state=None, label=None, open_only=False, since_days=None, limit=None, stage_since=False, cycle=None, unblocked=False):
        if cycle:
            print("note: GitHub Issues have no cycles; --cycle ignored", file=sys.stderr)
        labels, unlabeled, stage = [], False, None
        if label:
            labels.append(label)
        if state:
            stage = self._stage(state)
            if self.stage_labels[stage] is None:
                unlabeled = True
            else:
                labels.append(self.stage_labels[stage])
        ready = self.ready_labels() if stage == "todo" else []
        # An issue a running pass holds keeps its in-progress label whatever else it carries (an
        # `untriaged` one, say); no other stage's queue may hand it to a second session.
        taken = self.stage_labels.get("in_progress") if stage and stage != "in_progress" else None
        params = {"state": "open", "per_page": "100"}
        if labels:
            params["labels"] = ",".join(labels)
        if since_days:
            params["since"] = since_iso(since_days)
        out = []
        for repo in self.issue_repos():
            for n in self.api("repos/%s/%s/issues" % (self.org(), repo), params=params, paginate=True) or []:
                if "pull_request" in n:
                    continue
                t = self._ticket(repo, n)
                if unlabeled and t["stage"] != stage:
                    continue
                if taken and taken in t["labels"]:
                    continue
                if any(l.lower() in self.exclude_labels() for l in t["labels"]):
                    continue
                have = {l.lower() for l in t["labels"]}
                if ready and not any(g <= have for g in ready):
                    continue
                if unblocked:
                    t["blockedBy"] = self._blocked_by(repo, n)
                    if any(b["open"] for b in t["blockedBy"]):
                        continue
                if stage_since:
                    t["stageSince"] = self._stage_since(repo, n["number"], t)
                out.append(t)
        out.sort(key=priority_key)
        return out[:limit] if limit else out

    def _blocked_by(self, repo, n):
        """The issues blocking this one (GitHub issue dependencies). The issue payload carries a
        summary count, so the dependencies call is made only when there is something to fetch."""
        summary = n.get("issue_dependencies_summary")
        if summary is not None and not summary.get("blocked_by"):
            return []
        items = self.api("repos/%s/%s/issues/%d/dependencies/blocked_by" % (self.org(), repo, n["number"]), paginate=True) or []
        return [{"identifier": "%s#%d" % ((b.get("repository") or {}).get("name") or repo, b["number"]),
                 "title": b.get("title"), "open": b.get("state") == "open"} for b in items]

    def _stage(self, stage_or_name):
        stage = stage_or_name if stage_or_name in self.stage_labels else self.stage_of(stage_or_name)
        if stage not in self.stage_labels:
            raise TrackerError("no stage %r (have: %s)" % (stage_or_name, ", ".join(self.stage_labels)))
        return stage

    def _stage_since(self, repo, num, t):
        """When the issue last got its current stage label, from its timeline; else updatedAt."""
        if not t["stage"] or self.stage_labels[t["stage"]] is None:
            return t["updatedAt"]
        want = self.stage_labels[t["stage"]]
        events = self.api("repos/%s/%s/issues/%d/events" % (self.org(), repo, num), paginate=True) or []
        into = [e["created_at"] for e in events
                if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == want]
        return max(into) if into else t["updatedAt"]

    def get(self, ident, comments=False):
        path, repo, _ = self._path(ident)
        n = self.api(path)
        if "pull_request" in n:
            raise TrackerError("%s is a pull request, not an issue" % ident)
        c = self.api(path + "/comments", paginate=True) if comments else None
        t = self._ticket(repo, n, c)
        t["blockedBy"] = self._blocked_by(repo, n)
        return t

    def block(self, ident, by):
        """Record `by` blocks `ident` as a GitHub issue dependency. The API wants the blocker's
        numeric id, not its number."""
        path, _, _ = self._path(ident)
        by_path, _, _ = self._path(by)
        blocker = self.api(by_path)
        if dry_run():
            print("would: block %s by %s (id %s)" % (ident, by, blocker.get("id")))
            return None
        try:
            self.api(path + "/dependencies/blocked_by", method="POST", body={"issue_id": blocker["id"]})
        except TrackerError as e:
            if "already" not in str(e).lower():
                raise
        return self.get(ident)

    def move(self, ident, stage_or_name):
        target = self.stage_labels[self._stage(stage_or_name)]
        current = self.get(ident)["labels"]
        remove = [l for l in current if l in self.stage_labels.values() and l != target]
        add = [] if target is None or target in current else [target]
        if not add and not remove:
            return self.get(ident)
        return self.set_labels(ident, add=add, remove=remove)

    def set_labels(self, ident, add=(), remove=()):
        path, _, _ = self._path(ident)
        if dry_run():
            print("would: labels %s +%s -%s" % (ident, list(add), list(remove)))
            return None
        for l in remove:
            self.api(path + "/labels/" + urllib.request.quote(l, safe=""), method="DELETE")
        if add:
            self.api(path + "/labels", method="POST", body={"labels": list(add)})
        return self.get(ident)

    def assign(self, ident, user=None, me=False):
        path, _, _ = self._path(ident)
        login = self.me()["login"] if me else user
        if dry_run():
            print("would: assign %s to %s" % (ident, login))
            return None
        self.api(path + "/assignees", method="POST", body={"assignees": [login]})
        return self.get(ident)

    def comment(self, ident, body):
        path, _, _ = self._path(ident)
        if dry_run():
            print("would: comment on %s (%d chars)" % (ident, len(body)))
            return None
        c = self.api(path + "/comments", method="POST", body={"body": body})
        return {"id": c["id"], "url": c.get("html_url")}

    def create(self, title, body, state=None, labels=(), repo=None, cycle=None):
        if cycle:
            print("note: GitHub Issues have no cycles; --cycle ignored", file=sys.stderr)
        repos = self.issue_repos()
        repo = repo or (repos[0] if len(repos) == 1 else None)
        if not repo:
            raise TrackerError("--repo is required: issues live in each repo (%s)" % ", ".join(repos))
        labels = list(labels)
        if state and self.stage_labels[self._stage(state)]:
            labels.append(self.stage_labels[self._stage(state)])
        if dry_run():
            print("would: create %r in %s labels=%s" % (title, repo, labels))
            return None
        n = self.api("repos/%s/%s/issues" % (self.org(), repo), method="POST",
                     body={"title": title, "body": body or "", "labels": labels})
        return self._ticket(repo, n)

    def append(self, ident, text):
        path, repo, _ = self._path(ident)
        old = self.get(ident)["description"]
        new = (old.rstrip() + "\n\n" + text.strip() + "\n") if old.strip() else text.strip() + "\n"
        if dry_run():
            print("would: append %d chars to the body of %s" % (len(text), ident))
            return None
        n = self.api(path, method="PATCH", body={"body": new})
        t = self._ticket(repo, n)
        if not t["description"].startswith(old.rstrip()):
            raise TrackerError("%s: body no longer starts with the original text after the append" % ident)
        return t

    def setup_labels(self):
        wanted = [(self.queue_label, "1d76db", "Ready for the automated SDLC passes")] if self.queue_label else []
        seen = {self.queue_label}
        for k, v in self.stage_labels.items():
            if v and v not in seen:
                seen.add(v); wanted.append((v, "c5def5", "Stage: %s" % self.states[k]))
        for repo in self.issue_repos():
            for name, color, desc in wanted:
                if dry_run():
                    print("would: create label %r in %s" % (name, repo)); continue
                try:
                    self.api("repos/%s/%s/labels" % (self.org(), repo), method="POST",
                             body={"name": name, "color": color, "description": desc})
                    print("%s: created %s" % (repo, name))
                except TrackerError as e:
                    if "already_exists" in str(e) or "422" in str(e):
                        print("%s: %s exists" % (repo, name))
                    else:
                        raise

    def ticket_regex(self):
        repos = "|".join(re.escape(r) for r in self.issue_repos())
        return r"((%s)#[0-9]+|#[0-9]+)" % repos

    def ticket_example(self):
        return "%s#123" % (self.issue_repos() or ["<repo>"])[0]

    def howto(self):
        cli = os.path.join(ROOT, "scripts", "tracker.py")
        staged = ", ".join("%s = %s" % (self.states[k], v or "no stage label") for k, v in self.stage_labels.items())
        lines = ["Ticket tracker: GitHub Issues in the %s repos (%s). Ticket IDs look like <repo>#123; a stage is a label (%s)."
                 % (self.org(), ", ".join(self.issue_repos()), staged),
                 "Read a ticket and its comments with `%s get <repo>#N --comments` or `gh issue view N -R %s/<repo> --comments`; "
                 "post comments with the CLI or `gh issue comment`." % (cli, self.org())]
        return "\n".join(lines + self._howto_common(cli))


def open_tracker(project=None, config=None, root=None, paths=None):
    name, cfg = pick_project(config or load_config(root, paths), project)
    kind = (cfg.get("tracker") or {}).get("kind")
    if kind == "linear":
        return LinearTracker(name, cfg)
    if kind == "github":
        return GitHubTracker(name, cfg)
    raise TrackerError("project %s: tracker.kind must be linear or github, got %r" % (name, kind))


# ---------------------------------------------------------------- CLI

def _body_from(args):
    if getattr(args, "body_file", None):
        with open(args.body_file) as f:
            return f.read()
    if getattr(args, "body", None) is not None:
        return args.body
    raise TrackerError("give --body or --body-file")


def _print_ticket(t, full=False):
    if t is None:
        return
    who = t.get("assignee") or {}
    print("%-14s %-20s %-7s %s" % (t["identifier"], t.get("state") or "", t.get("priorityName") or "-", t.get("title") or ""))
    if not full:
        return
    print("url:       %s" % t.get("url"))
    print("labels:    %s" % ", ".join(t.get("labels") or []))
    print("repos:     %s" % ", ".join(t.get("repos") or []))
    print("assignee:  %s" % (who.get("email") or who.get("login") or who.get("name") or "-"))
    cr = t.get("creator") or {}
    print("creator:   %s" % (cr.get("email") or cr.get("login") or cr.get("name") or "-"))
    print("branch:    %s" % t.get("branchName"))
    if t.get("parent"):
        print("parent:    %s %s" % (t["parent"].get("identifier"), t["parent"].get("title")))
    for r in t.get("relations") or []:
        print("related:   %s %s (%s)" % (r["issue"], r.get("title", ""), r["type"]))
    for b in t.get("blockedBy") or []:
        print("blocked by: %s %s (%s)" % (b["identifier"], b.get("title") or "", "open" if b["open"] else "closed"))
    for a in t.get("attachments") or []:
        print("attached:  %s %s" % (a.get("url"), a.get("title") or ""))
    print("\n" + (t.get("description") or "(no description)").rstrip() + "\n")
    for c in t.get("comments") or []:
        a = c.get("author") or {}
        print("--- comment by %s at %s\n%s\n" % (a.get("email") or a.get("login") or a.get("name") or "?", c.get("createdAt"), c["body"].rstrip()))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracker.py", description=__doc__.split("\n\n")[0])
    # --project / --json are accepted before or after the subcommand. SUPPRESS on the
    # per-command copies, or their None default would overwrite a value given up front.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS, help="project key in projects.yaml (default: the only one, or $SDLC_PROJECT)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print the normalized JSON instead of a table")
    common.add_argument("--config", action="append", default=argparse.SUPPRESS, metavar="FILE",
                        help="a project config file, repeatable (default: $SDLC_CONFIG, else projects.yaml)")
    ap.add_argument("--project", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--config", action="append", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", default=False, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = lambda name: sub.add_parser(name, parents=[common])
    add("me")
    p = add("list"); p.add_argument("--state"); p.add_argument("--label"); p.add_argument("--open", action="store_true"); p.add_argument("--since", type=int, metavar="DAYS"); p.add_argument("--limit", type=int)
    p.add_argument("--cycle", choices=["current"], help="Linear: only tickets in the team's active cycle")
    p.add_argument("--stage-since", action="store_true", help="add stageSince: when each ticket last entered its current stage")
    p.add_argument("--not-mine", action="store_true", help="skip tickets the tracker user created")
    p.add_argument("--unblocked", action="store_true", help="skip tickets with an open blocker (see `block`)")
    p = add("get"); p.add_argument("id"); p.add_argument("--comments", action="store_true")
    p = add("move"); p.add_argument("id"); p.add_argument("state")
    p = add("block"); p.add_argument("id"); p.add_argument("--by", required=True, metavar="OTHER", help="the ticket that must close first")
    p = add("label"); p.add_argument("id"); p.add_argument("--add", action="append", default=[]); p.add_argument("--remove", action="append", default=[])
    p = add("assign"); p.add_argument("id"); g = p.add_mutually_exclusive_group(required=True); g.add_argument("--me", action="store_true"); g.add_argument("--user")
    p = add("comment"); p.add_argument("id"); p.add_argument("--body"); p.add_argument("--body-file")
    p = add("create"); p.add_argument("--title", required=True); p.add_argument("--body"); p.add_argument("--body-file"); p.add_argument("--state"); p.add_argument("--label", action="append", default=[]); p.add_argument("--repo")
    p.add_argument("--cycle", choices=["current"], help="Linear: put the ticket in the team's active cycle")
    p = add("append"); p.add_argument("id"); p.add_argument("--body"); p.add_argument("--body-file")
    add("setup-labels")
    p = add("config"); p.add_argument("what", choices=["projects", "name", "org", "host", "context", "repos-root", "kind", "queue-label", "stages", "repos", "ticket-regex", "ticket-example",
                                                "design-labels", "blocker-label", "env", "envs", "env-name", "tasks", "triage", "clone-url", "repo-for-label", "label-for-repo"]); p.add_argument("arg", nargs="?")
    add("howto")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "config" and args.what == "projects":
            print("\n".join(in_env(load_config(paths=args.config))))
            return 0
        if args.cmd == "config" and args.what == "envs":
            cfg = load_config(paths=args.config)
            out = {k: b.get("env") or "" for k, b in cfg.items()}
            print(json.dumps(out) if args.json else "\n".join("%s\t%s" % kv for kv in out.items()))
            return 0
        tr = open_tracker(args.project, paths=args.config)
        out = None
        if args.cmd == "me":
            out = tr.me()
            if not args.json:
                print(out.get("email") or out.get("login") or out.get("name"))
        elif args.cmd == "list":
            out = tr.list(state=args.state, label=args.label, open_only=args.open, since_days=args.since,
                          limit=None if args.not_mine else args.limit, stage_since=args.stage_since, cycle=args.cycle,
                          unblocked=args.unblocked)
            if args.not_mine:
                out = tr.not_mine(out)
                out = out[:args.limit] if args.limit else out
            if not args.json:
                for t in out:
                    _print_ticket(t)
        elif args.cmd == "get":
            out = tr.get(args.id, comments=args.comments)
            if not args.json:
                _print_ticket(out, full=True)
        elif args.cmd == "move":
            out = tr.move(args.id, args.state)
            if not args.json and out:
                print("%s -> %s" % (out["identifier"], out["state"]))
        elif args.cmd == "block":
            out = tr.block(args.id, args.by)
            if not args.json and out:
                print("%s blocked by %s" % (out["identifier"], ", ".join(b["identifier"] for b in out.get("blockedBy") or [])))
        elif args.cmd == "label":
            out = tr.set_labels(args.id, add=args.add, remove=args.remove)
            if not args.json and out:
                print("%s labels: %s" % (out["identifier"], ", ".join(out["labels"])))
        elif args.cmd == "assign":
            out = tr.assign(args.id, user=args.user, me=args.me)
            if not args.json and out:
                a = out.get("assignee") or {}
                print("%s assigned to %s" % (out["identifier"], a.get("email") or a.get("login") or a.get("name")))
        elif args.cmd == "comment":
            out = tr.comment(args.id, _body_from(args))
            if not args.json and out:
                print(out.get("url") or out.get("id"))
        elif args.cmd == "create":
            out = tr.create(args.title, _body_from(args), state=args.state, labels=args.label, repo=args.repo, cycle=args.cycle)
            if not args.json and out:
                print("%s %s" % (out["identifier"], out["url"]))
        elif args.cmd == "append":
            out = tr.append(args.id, _body_from(args))
            if not args.json and out:
                print("%s: %d chars now" % (out["identifier"], len(out["description"])))
        elif args.cmd == "setup-labels":
            tr.setup_labels()
        elif args.cmd == "config":
            out, text = {
                "name": lambda: (tr.display_name(),) * 2,
                "env-name": lambda: (tr.env_name(),) * 2,
                "org": lambda: (tr.org(), tr.org()),
                "host": lambda: (tr.host(),) * 2,
                "context": lambda: (tr.context_dir(),) * 2,
                "repos-root": lambda: (tr.repos_root(),) * 2,
                "kind": lambda: (tr.kind, tr.kind),
                "queue-label": lambda: (tr.queue_label, tr.queue_label),
                "ticket-example": lambda: (tr.ticket_example(),) * 2,
                "design-labels": lambda: (tr.design_labels(), "\n".join("%s: %s" % (g, ", ".join(v)) for g, v in tr.design_labels().items())),
                "env": lambda: (None, tr.shell_env()),
                "blocker-label": lambda: (tr.blocker_label(),) * 2,
                "clone-url": lambda: (tr.clone_url(args.arg or ""),) * 2,
                "repos": lambda: (tr.repo_names(), "\n".join(tr.repo_names())),
                "tasks": lambda: (tr.tasks(), "\n".join("%s\t%s\t%s%s" % (t["name"], t["every"], t["script"], "\tskipped: " + t["skip"] if t["skip"] else "")
                                                         for t in tr.tasks())),
                "triage": lambda: (tr.triage_config(), "\n".join("%s: %s" % kv for kv in tr.triage_config().items())),
                "stages": lambda: (tr.states, "\n".join("%s\t%s" % kv for kv in tr.states.items())),
                "ticket-regex": lambda: (tr.ticket_regex(), tr.ticket_regex()),
                "repo-for-label": lambda: (tr.repo_for_label(args.arg or ""),) * 2,
                "label-for-repo": lambda: (tr.label_for_repo(args.arg or ""),) * 2,
            }[args.what]()
            if out is None and args.what == "env":
                print(text); return 0
            if out is None and args.what in ("repo-for-label", "label-for-repo"):
                raise TrackerError("no %s %r" % (args.what.replace("-", " "), args.arg))
            print(json.dumps(out) if args.json else text)
            return 0
        elif args.cmd == "howto":
            print(tr.howto()); return 0
        if args.json:
            print(json.dumps(out, indent=2))
        return 0
    except TrackerError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
