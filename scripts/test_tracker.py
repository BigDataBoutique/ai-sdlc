#!/usr/bin/env python3
"""Unit tests for tracker.py: both backends against fake transports, the YAML subset parser,
the .env reader. No network, no gh.  Run:  python3 -m unittest scripts/test_tracker.py"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tracker  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LINEAR_CFG = {
    "forge": {"kind": "github", "org": "acme"},
    "tracker": {"kind": "linear", "team": "Abc", "queue_label": "Claude Ready",
                "states": {"discussion": "Requires Discussion"}},
    "repos": {"api": {"label": "API"}, "app": {"label": "App"}, "infra": {}},
}
GITHUB_CFG = {
    "forge": {"kind": "github", "org": "acme"},
    "tracker": {"kind": "github", "queue_label": "Claude Ready"},
    "repos": {"api": {}, "app": {}, "infra": {"issues": False}},
}


class YamlSubset(unittest.TestCase):
    def test_committed_file_matches_pyyaml(self):
        with open(os.path.join(ROOT, "projects.yaml")) as f:
            text = f.read()
        ours = tracker.parse_yaml(text)
        self.assertTrue(ours)
        for name, project in ours.items():
            self.assertIn(project["tracker"]["kind"], ("linear", "github"), name)
            self.assertTrue(project["forge"]["org"] and project["repos"], name)
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed here; the subset parser stands alone")
        self.assertEqual(ours, yaml.safe_load(text))

    def test_shapes(self):
        text = """
# comment
a:
  b: 1        # trailing comment
  c: "x # not a comment"
  d: 'it''s'
  e: [1, two, "three, 3"]
  f: {k: v, n: 2, empty: {}}
  g:
    - one
    - k: v
      m: 3
    - - nested
      - list
  h: true
  i: ~
  j: 1.5
list:
- a
- b
"""
        got = tracker.parse_yaml(text)
        self.assertEqual(got, {
            "a": {"b": 1, "c": "x # not a comment", "d": "it's", "e": [1, "two", "three, 3"],
                  "f": {"k": "v", "n": 2, "empty": {}},
                  "g": ["one", {"k": "v", "m": 3}, ["nested", "list"]], "h": True, "i": None, "j": 1.5},
            "list": ["a", "b"]})

    def test_unsupported(self):
        for bad in ("a: &x 1\n", "a: |\n  block\n", "a: 1\n---\nb: 2\n"):
            with self.assertRaises(tracker.TrackerError):
                tracker.parse_yaml(bad)


class ConfigAndEnv(unittest.TestCase):
    def test_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w") as f:
                f.write("# c\nOTHER=1\nLINEAR_API_KEY='lin_abc'\nEMPTY=\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(tracker.read_env_key("LINEAR_API_KEY", d), "lin_abc")
                self.assertIsNone(tracker.read_env_key("EMPTY", d))
                self.assertIsNone(tracker.read_env_key("MISSING", d))
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "fromenv"}):
                self.assertEqual(tracker.read_env_key("LINEAR_API_KEY", d), "fromenv")

    def test_load_json_and_pick(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "projects.json"), "w") as f:
                json.dump({"p1": LINEAR_CFG, "p2": dict(GITHUB_CFG, forge={"kind": "github", "org": "other"})}, f)
            cfg = tracker.load_config(d)
            with self.assertRaises(tracker.TrackerError):
                tracker.pick_project(cfg)
            self.assertEqual(tracker.pick_project(cfg, "p2")[0], "p2")
            with mock.patch.dict(os.environ, {"SDLC_PROJECT": "p1"}):
                self.assertEqual(tracker.pick_project(cfg)[0], "p1")

    def test_several_config_files(self):
        with tempfile.TemporaryDirectory() as d:
            def write(name, data):
                path = os.path.join(d, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    json.dump(data, f)
                return path
            lin = write("a/linear.json", {"lin": LINEAR_CFG})
            gh = write("b/github.json", {"gh": dict(GITHUB_CFG, forge={"kind": "github", "org": "other"})})
            cfg = tracker.load_config(paths=[lin, gh])
            self.assertEqual(sorted(cfg), ["gh", "lin"])
            self.assertEqual(cfg["lin"]["_dir"], os.path.join(d, "a"))
            t = tracker.open_tracker("gh", config=cfg)
            self.assertEqual((t.kind, t.org()), ("github", "other"))
            self.assertEqual(t.context_dir(), os.path.join(d, "b", "contexts", "gh"))   # next to its own file
            # the same block twice is fine; a different one is a contradiction, named by key path
            self.assertEqual(sorted(tracker.load_config(paths=[lin, write("dup.json", {"lin": LINEAR_CFG})])), ["lin"])
            changed = dict(LINEAR_CFG, tracker=dict(LINEAR_CFG["tracker"], queue_label="Other"))
            with self.assertRaises(tracker.TrackerError) as cm:
                tracker.load_config(paths=[lin, write("bad.json", {"lin": changed})])
            self.assertIn("tracker.queue_label", str(cm.exception))
            # one repo, two projects: which tracker owns its PRs is ambiguous
            with self.assertRaises(tracker.TrackerError) as cm:
                tracker.load_config(paths=[lin, write("twice.json", {"lin2": GITHUB_CFG})])
            self.assertIn("github.com/acme/api", str(cm.exception))
            with self.assertRaises(tracker.TrackerError):
                tracker.load_config(paths=[os.path.join(d, "missing.yaml")])
            # --config beats $SDLC_CONFIG, which beats the default file
            with mock.patch.dict(os.environ, {"SDLC_CONFIG": os.pathsep.join([lin, gh])}):
                self.assertEqual(sorted(tracker.load_config(d)), ["gh", "lin"])
                self.assertEqual(sorted(tracker.load_config(d, paths=[gh])), ["gh"])
            with mock.patch.dict(os.environ, {"SDLC_CONFIG": ""}), mock.patch("sys.stdout") as out:
                self.assertEqual(tracker.main(["--config", lin, "--config", gh, "config", "projects"]), 0)
                self.assertIn("lin\ngh", "".join(c.args[0] for c in out.write.call_args_list))

    def test_context_dir(self):
        t = tracker.GitHubTracker("p", GITHUB_CFG, runner=FakeGh({}))
        self.assertEqual(t.context_dir(), os.path.join(ROOT, "contexts", "p"))
        t = tracker.GitHubTracker("p", dict(GITHUB_CFG, context="docs/p"), runner=FakeGh({}))
        self.assertEqual(t.context_dir(), os.path.join(ROOT, "docs", "p"))

    def test_repo_label_mapping(self):
        t = tracker.LinearTracker("p", LINEAR_CFG, transport=lambda q, v: {})
        self.assertEqual(t.repo_for_label("api"), "api")
        self.assertEqual(t.label_for_repo("app"), "App")
        self.assertIsNone(t.label_for_repo("infra"))
        self.assertEqual(t.repos_for_labels(["Claude Ready", "App", "API"]), ["app", "api"])
        self.assertEqual(t.state_name("discussion"), "Requires Discussion")
        self.assertEqual(t.state_name("qa"), "QA")
        self.assertEqual(t.state_name("in review"), "In Review")
        self.assertEqual(t.stage_of("Todo"), "todo")


def issue(ident, title, state="Todo", labels=("Claude Ready",), priority=0, created="2026-01-01", desc="orig"):
    return {"id": "uuid-" + ident, "identifier": ident, "title": title, "description": desc, "url": "https://l/" + ident,
            "priority": priority, "branchName": "x/" + ident.lower(), "createdAt": created, "updatedAt": created,
            "state": {"name": state, "type": "unstarted"}, "labels": {"nodes": [{"name": l} for l in labels]},
            "team": {"key": "ABC"}, "assignee": None, "creator": {"id": "u1", "name": "Ann", "email": "ann@x"}}


class FakeLinear:
    """Answers the GraphQL shapes tracker.py sends; records mutations."""
    def __init__(self, issues=None):
        self.issues = issues or []
        self.calls = []
        self.updates = []
        self.cycle = {"id": "cyc1"}
        self.blockers = {}   # identifier -> inverseRelations nodes

    def __call__(self, query, variables):
        self.calls.append((query, variables))
        q = " ".join(query.split())
        if q.startswith("query { teams"):
            return {"data": {"teams": {"nodes": [{"id": "team1", "name": "Abc", "key": "ABC", "states": {"nodes": [
                {"id": "s-todo", "name": "Todo", "type": "unstarted"}, {"id": "s-prog", "name": "In Progress", "type": "started"},
                {"id": "s-disc", "name": "Requires Discussion", "type": "unstarted"},
                {"id": "s-tri", "name": "Triage", "type": "triage"}]}}]}}}
        if "activeCycle" in q:
            return {"data": {"team": {"activeCycle": self.cycle}}}
        if "issueLabels(" in q:
            n = variables["n"]
            nodes = {"Claude Ready": [{"id": "l-cr", "name": "Claude Ready", "team": None}],
                     "API": [{"id": "l-api", "name": "API", "team": {"id": "team1"}}],
                     "Other Team": [{"id": "l-ot", "name": "Other Team", "team": {"id": "team2"}}]}.get(n, [])
            return {"data": {"issueLabels": {"nodes": nodes}}}
        if q.startswith("query { viewer"):
            return {"data": {"viewer": {"id": "me1", "name": "Me", "displayName": "me", "email": "me@x"}}}
        if "issues(filter" in q:
            after = variables.get("a")
            page = self.issues[:2] if after is None else self.issues[2:]
            if "history(" in q:
                page = [dict(i, startedAt="2026-01-01T00:00:00.000Z", history={"nodes": [
                    {"createdAt": "2026-01-05T00:00:00.000Z", "toState": {"name": "In Progress"}},
                    {"createdAt": "2026-01-09T00:00:00.000Z", "toState": {"name": "Todo"}},
                    {"createdAt": "2026-01-07T00:00:00.000Z", "toState": {"name": "In Progress"}}]}) for i in page]
            if "inverseRelations" in q:
                page = [dict(i, inverseRelations={"nodes": self.blockers.get(i["identifier"], [])}) for i in page]
            return {"data": {"issues": {"nodes": page, "pageInfo": {"hasNextPage": after is None and len(self.issues) > 2,
                                                                    "endCursor": "c1"}}}}
        if q.startswith("query($id: String!) { issue("):
            for i in self.issues:
                if i["identifier"] == variables["id"]:
                    i = dict(i, attachments={"nodes": []}, relations={"nodes": []},
                             inverseRelations={"nodes": self.blockers.get(i["identifier"], [])},
                             children={"nodes": []}, parent=None)
                    if "comments {" in q:
                        i["comments"] = {"nodes": [{"id": "c1", "body": "hi", "createdAt": "t", "user": {"name": "Bob"}}]}
                    return {"data": {"issue": i}}
            return {"data": {"issue": None}}
        if "issueUpdate(" in q:
            self.updates.append((variables["id"], variables["in"]))
            for i in self.issues:
                if i["identifier"] == variables["id"]:
                    if "description" in variables["in"]:
                        i = dict(i, description=variables["in"]["description"])
                    return {"data": {"issueUpdate": {"success": True, "issue": i}}}
            return {"errors": [{"message": "Entity not found"}]}
        if "commentCreate(" in q:
            self.updates.append(("comment", variables["in"]))
            return {"data": {"commentCreate": {"success": True, "comment": {"id": "c9", "url": "https://l/c9"}}}}
        if "issueCreate(" in q:
            self.updates.append(("create", variables["in"]))
            return {"data": {"issueCreate": {"success": True, "issue": issue("ABC-9", variables["in"]["title"])}}}
        if "issueRelationCreate(" in q:
            self.updates.append(("relation", variables["in"]))
            return {"data": {"issueRelationCreate": {"success": True}}}
        raise AssertionError("unexpected query: " + q[:80])


class Linear(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLinear([
            issue("ABC-1", "low, old", priority=4, created="2026-01-01"),
            issue("ABC-2", "none", priority=0, created="2025-01-01"),
            issue("ABC-3", "urgent", priority=1, created="2026-06-01", labels=("Claude Ready", "API")),
            issue("ABC-4", "low, older", priority=4, created="2025-06-01"),
        ])
        self.t = tracker.LinearTracker("p", LINEAR_CFG, transport=self.fake, api_key="k")

    def test_list_filters_paginates_sorts(self):
        got = self.t.list(state="todo", label="Claude Ready")
        self.assertEqual([t["identifier"] for t in got], ["ABC-3", "ABC-4", "ABC-1", "ABC-2"])
        self.assertEqual(got[0]["repos"], ["api"])
        self.assertEqual(got[0]["stage"], "todo")
        q, v = [c for c in self.fake.calls if "issues(filter" in c[0]][0]
        self.assertEqual(v["f"], {"team": {"id": {"eq": "team1"}}, "state": {"name": {"eq": "Todo"}},
                                  "labels": {"name": {"eq": "Claude Ready"}}})
        self.assertEqual(len([c for c in self.fake.calls if "issues(filter" in c[0]]), 2)

    def test_not_mine(self):
        self.fake.issues[1] = dict(self.fake.issues[1], creator={"id": "me1", "name": "Me", "email": "me@x"})
        self.assertEqual([t["identifier"] for t in self.t.not_mine(self.t.list(state="todo"))], ["ABC-3", "ABC-4", "ABC-1"])

    def test_unblocked_and_block(self):
        blocker = lambda ident, kind: {"type": "blocks", "issue": {"identifier": ident, "title": "b", "state": {"type": kind}}}
        self.fake.blockers = {"ABC-1": [blocker("ABC-7", "started")], "ABC-2": [blocker("ABC-8", "completed")],
                              "ABC-4": [{"type": "related", "issue": {"identifier": "ABC-9", "title": "r", "state": {"type": "started"}}}]}
        self.assertNotIn("blockedBy", self.t.list(state="todo")[0])                                  # opt-in: relations not fetched
        got = {t["identifier"]: t.get("blockedBy") for t in self.t.list(state="todo", unblocked=True)}
        self.assertEqual(got, {"ABC-3": [], "ABC-4": [], "ABC-2": [{"identifier": "ABC-8", "title": "b", "open": False}]})
        self.assertEqual(self.t.get("ABC-1")["blockedBy"], [{"identifier": "ABC-7", "title": "b", "open": True}])
        self.t.block("ABC-2", by="ABC-3")
        self.assertEqual(self.fake.updates[-1], ("relation", {"issueId": "uuid-ABC-3", "relatedIssueId": "uuid-ABC-2", "type": "blocks"}))
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}):
            self.assertIsNone(self.t.block("ABC-2", by="ABC-3"))
        self.assertEqual(len([u for u in self.fake.updates if u[0] == "relation"]), 1)

    def test_list_open_only(self):
        self.t.list(open_only=True)
        q, v = [c for c in self.fake.calls if "issues(filter" in c[0]][0]
        self.assertEqual(v["f"]["state"], {"type": {"nin": ["completed", "canceled"]}})

    def test_list_current_cycle(self):
        self.t.list(state="todo", cycle="current")
        q, v = [c for c in self.fake.calls if "issues(filter" in c[0]][0]
        self.assertEqual(v["f"]["cycle"], {"id": {"eq": "cyc1"}})
        self.fake.cycle = None
        with self.assertRaises(tracker.TrackerError):
            self.t.list(state="todo", cycle="current")    # no active cycle: fail, never list every cycle

    def test_stage_since(self):
        self.assertNotIn("stageSince", self.t.list(state="todo")[0])          # opt-in: history is not fetched
        self.assertNotIn("history(", self.fake.calls[-1][0])
        fake = FakeLinear([issue("ABC-7", "stuck", state="In Progress"), issue("ABC-8", "waiting")])
        t = tracker.LinearTracker("p", LINEAR_CFG, transport=fake, api_key="k")
        got = {x["identifier"]: x["stageSince"] for x in t.list(stage_since=True)}
        self.assertEqual(got, {"ABC-7": "2026-01-07T00:00:00.000Z",   # newest entry into In Progress, not the first
                               "ABC-8": "2026-01-09T00:00:00.000Z"})
        since = tracker.LinearTracker._stage_since
        n = {"state": {"name": "In Progress", "type": "started"}, "history": {"nodes": []}, "startedAt": "s", "updatedAt": "u"}
        self.assertEqual(since(n), "s")                                       # history page exhausted: startedAt while started
        self.assertEqual(since(dict(n, state={"name": "Todo", "type": "unstarted"})), "u")

    def test_move_labels_assign(self):
        self.t.move("ABC-1", "in_progress")
        self.t.move("ABC-1", "Requires Discussion")
        self.t.set_labels("ABC-1", add=["API"], remove=["Claude Ready"])
        self.t.assign("ABC-1", me=True)
        self.assertEqual(self.fake.updates, [
            ("ABC-1", {"stateId": "s-prog"}), ("ABC-1", {"stateId": "s-disc"}),
            ("ABC-1", {"addedLabelIds": ["l-api"], "removedLabelIds": ["l-cr"]}), ("ABC-1", {"assigneeId": "me1"})])
        with self.assertRaises(tracker.TrackerError):
            self.t.move("ABC-1", "Nope")
        with self.assertRaises(tracker.TrackerError):
            self.t.label_id("Other Team")   # another team's label never applies here

    def test_get_comments_create_append(self):
        got = self.t.get("ABC-3", comments=True)
        self.assertEqual(got["comments"][0]["body"], "hi")
        self.assertEqual(got["creator"]["email"], "ann@x")
        with self.assertRaises(tracker.TrackerError):
            self.t.get("ABC-99")
        self.t.create("New", "body", state="todo", labels=["Claude Ready", "API"])
        self.assertEqual(self.fake.updates[-1], ("create", {"teamId": "team1", "title": "New", "description": "body",
                                                              "stateId": "s-todo", "labelIds": ["l-cr", "l-api"]}))
        self.t.create("Now", "b", state="todo", labels=["API"], cycle="current")
        self.assertEqual(self.fake.updates[-1][1]["cycleId"], "cyc1")
        self.t.create("Unsure", "b", state="triage")
        self.assertEqual(self.fake.updates[-1][1]["stateId"], "s-tri")
        self.assertNotIn("cycleId", self.fake.updates[-1][1])
        self.fake.cycle = None
        with self.assertRaises(tracker.TrackerError):
            self.t.create("No cycle", "b", state="todo", cycle="current")   # never silently falls back to no cycle
        self.t.comment("ABC-3", "note")
        self.assertEqual(self.fake.updates[-1], ("comment", {"issueId": "uuid-ABC-3", "body": "note"}))
        got = self.t.append("ABC-3", "## Design\nstuff")
        self.assertEqual(got["description"], "orig\n\n## Design\nstuff\n")

    def test_append_detects_clobber(self):
        real = self.fake.__call__

        def clobber(q, v):
            r = real(q, v)
            if "issueUpdate(" in q:
                r["data"]["issueUpdate"]["issue"]["description"] = "gone"
            return r
        self.t._transport = clobber
        with self.assertRaises(tracker.TrackerError):
            self.t.append("ABC-3", "x")

    def test_dry_run_writes_nothing(self):
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}):
            self.assertIsNone(self.t.move("ABC-1", "todo"))
            self.assertIsNone(self.t.create("t", "b"))
            self.assertIsNone(self.t.comment("ABC-1", "b"))
        self.assertEqual(self.fake.updates, [])

    def test_errors_and_regex(self):
        self.assertEqual(self.t.ticket_regex(), r"\bABC-[0-9]+\b")
        self.t.list(since_days=3)
        self.assertIn("updatedAt", [c for c in self.fake.calls if "issues(filter" in c[0]][-1][1]["f"])
        self.t._transport = lambda q, v: {"errors": [{"message": "boom"}]}
        with self.assertRaises(tracker.TrackerError):
            self.t.me()
        self.assertIn("move <ID>", tracker.LinearTracker("p", LINEAR_CFG, transport=self.fake, api_key="k").howto())

    def test_connect_ipv4_first_and_skips_dead_addresses(self):
        tried = []

        class Sock:
            def __init__(self, family, kind, proto): self.family = family
            def settimeout(self, t): pass
            def close(self): pass
            def connect(self, addr):
                tried.append(addr[0])
                if addr[0] in ("::1", "10.0.0.1"):
                    raise OSError("no route")
        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]
        conn = tracker._Https("api.linear.app", 443, timeout=1)
        conn._context = mock.Mock(wrap_socket=lambda s, server_hostname=None: s)
        with mock.patch.object(socket, "getaddrinfo", return_value=infos), mock.patch.object(socket, "socket", Sock):
            conn.connect()
        self.assertEqual(tried, ["10.0.0.1", "10.0.0.2"])   # IPv4 before the AAAA record, dead address skipped
        self.assertEqual(conn.sock.family, socket.AF_INET)
        dead = [i for i in infos if i[4][0] != "10.0.0.2"]
        with mock.patch.object(socket, "getaddrinfo", return_value=dead), mock.patch.object(socket, "socket", Sock):
            with self.assertRaises(OSError):
                tracker._Https("api.linear.app", 443, timeout=1).connect()


def gh_issue(repo, n, title, labels=(), state="open", body="orig", user="ann", blocked_by=()):
    """blocked_by: [(repo, number, state)] the fake serves from the dependencies endpoint."""
    return {"node_id": "I%d" % n, "id": 1000 + n, "number": n, "title": title, "body": body, "state": state,
            "html_url": "https://github.com/acme/%s/issues/%d" % (repo, n), "created_at": "2026-01-%02d" % n,
            "updated_at": "t", "labels": [{"name": l} for l in labels], "assignees": [],
            "user": {"login": user, "node_id": "U1" if user == "me" else "U-" + user},
            "issue_dependencies_summary": {"blocked_by": len(blocked_by)},
            "x_blocked_by": [{"number": bn, "state": bs, "title": "blocker", "repository": {"name": br}} for br, bn, bs in blocked_by]}


class FakeGh:
    def __init__(self, issues):
        self.issues = issues  # {repo: {n: issue}}
        self.calls = []

    def __call__(self, args, input=None):
        self.calls.append((args, input))
        method, path = args[1], args[2]
        params = {args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1] for i, a in enumerate(args) if a == "-f"}
        body = json.loads(input) if input else None
        if path == "user":
            return {"login": "me", "node_id": "U1"}
        parts = path.split("/")
        repo = parts[2]
        if path.endswith("/issues") and method == "GET":
            want = set(params.get("labels", "").split(",")) - {""}
            out = [i for i in self.issues.get(repo, {}).values() if want <= {l["name"] for l in i["labels"]}]
            out.append({"number": 999, "pull_request": {}, "labels": [{"name": l} for l in want], "title": "pr"})
            return [out] if "--paginate" in args else out
        if path.endswith("/issues") and method == "POST":
            n = 50
            i = gh_issue(repo, n, body["title"], body["labels"], body=body["body"])
            self.issues.setdefault(repo, {})[n] = i
            return i
        if path.endswith("/labels") and method == "POST" and "issues" not in path:
            if body["name"] == "Claude Ready":
                raise tracker.TrackerError("gh api: HTTP 422 already_exists")
            return body
        n = int(parts[4])
        i = self.issues[repo][n]
        if len(parts) == 5 and method == "GET":
            return i
        if len(parts) == 5 and method == "PATCH":
            i["body"] = body["body"]; return i
        if parts[5] == "dependencies" and method == "GET":
            return [i["x_blocked_by"]]
        if parts[5] == "dependencies" and method == "POST":
            i["x_blocked_by"].append({"number": body["issue_id"] - 1000, "state": "open", "title": "b", "repository": {"name": repo}})
            i["issue_dependencies_summary"]["blocked_by"] += 1
            return i
        if parts[5] == "comments" and method == "GET":
            return [[{"id": 1, "body": "hi", "created_at": "t", "user": {"login": "bob"}}]]
        if parts[5] == "events" and method == "GET":
            return [[{"event": "labeled", "label": {"name": "stage:todo"}, "created_at": "2026-01-01T00:00:00Z"},
                     {"event": "labeled", "label": {"name": "stage:in-progress"}, "created_at": "2026-01-03T00:00:00Z"},
                     {"event": "unlabeled", "label": {"name": "stage:in-progress"}, "created_at": "2026-01-04T00:00:00Z"},
                     {"event": "labeled", "label": {"name": "stage:in-progress"}, "created_at": "2026-01-06T00:00:00Z"}]]
        if parts[5] == "comments" and method == "POST":
            return {"id": 2, "html_url": "https://c/2"}
        if parts[5] == "labels" and method == "POST":
            i["labels"] += [{"name": l} for l in body["labels"]]; return i["labels"]
        if parts[5] == "labels" and method == "DELETE":
            name = tracker.urllib.request.unquote(parts[6])
            i["labels"] = [l for l in i["labels"] if l["name"] != name]; return None
        if parts[5] == "assignees":
            i["assignees"] = [{"login": l} for l in body["assignees"]]; return i
        raise AssertionError("unexpected gh call: %s" % args)


class GitHub(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGh({
            "api": {1: gh_issue("api", 1, "Fix the API", ["Claude Ready", "stage:todo", "priority:low"]),
                    2: gh_issue("api", 2, "Design me", ["Claude Ready", "stage:design"])},
            "app": {3: gh_issue("app", 3, "App todo", ["Claude Ready", "stage:todo", "priority:urgent"]),
                    5: gh_issue("app", 5, "Stuck", ["Claude Ready", "stage:in-progress"])},
            "infra": {4: gh_issue("infra", 4, "not swept", ["Claude Ready", "stage:todo"])},
        })
        self.t = tracker.GitHubTracker("p", GITHUB_CFG, runner=self.fake)

    def test_stage_since(self):
        got = self.t.list(state="in_progress", label="Claude Ready", stage_since=True)
        self.assertEqual([(t["identifier"], t["stageSince"]) for t in got], [("app#5", "2026-01-06T00:00:00Z")])
        self.assertEqual(len([c for c in self.fake.calls if c[0][2].endswith("/events")]), 1)
        self.t.list(state="in_progress")
        self.assertEqual(len([c for c in self.fake.calls if c[0][2].endswith("/events")]), 1)   # opt-in only

    def test_list(self):
        got = self.t.list(state="todo", label="Claude Ready")
        self.assertEqual([t["identifier"] for t in got], ["app#3", "api#1"])   # urgent first; infra has issues: false
        self.assertEqual(got[1]["state"], "Todo")
        self.assertEqual(got[1]["stage"], "todo")
        self.assertEqual(got[1]["repos"], ["api"])
        self.assertEqual(got[1]["branchName"], "1-fix-the-api")
        args = [c[0] for c in self.fake.calls if "repos/acme/api/issues" in c[0][2]][0]
        self.assertIn("labels=Claude Ready,stage:todo", args)
        self.assertEqual([t["identifier"] for t in self.t.list(state="Design")], ["api#2"])
        self.assertEqual([t["identifier"] for t in self.t.list(state="Design", cycle="current")], ["api#2"])   # ignored

    def test_not_mine(self):
        fake = FakeGh({"api": {1: gh_issue("api", 1, "theirs", ["stage:todo"]), 2: gh_issue("api", 2, "mine", ["stage:todo"], user="me")}})
        t = tracker.GitHubTracker("p", GITHUB_CFG, runner=fake)
        self.assertEqual([x["identifier"] for x in t.not_mine(t.list(state="todo"))], ["api#1"])

    def test_unblocked_and_block(self):
        fake = FakeGh({"api": {
            1: gh_issue("api", 1, "free", ["stage:todo"]),
            2: gh_issue("api", 2, "blocked", ["stage:todo"], blocked_by=[("api", 9, "open")]),
            3: gh_issue("api", 3, "blocker closed", ["stage:todo"], blocked_by=[("app", 8, "closed")]),
            9: gh_issue("api", 9, "the blocker", ["stage:todo"])}})
        t = tracker.GitHubTracker("p", GITHUB_CFG, runner=fake)
        self.assertEqual([x["identifier"] for x in t.list(state="todo")], ["api#1", "api#2", "api#3", "api#9"])
        got = t.list(state="todo", unblocked=True)
        self.assertEqual([x["identifier"] for x in got], ["api#1", "api#3", "api#9"])
        self.assertEqual(got[1]["blockedBy"], [{"identifier": "app#8", "title": "blocker", "open": False}])
        # the summary count spares the dependencies call for issues with nothing blocking them
        self.assertEqual(len([c for c in fake.calls if c[0][2].endswith("/dependencies/blocked_by")]), 2)
        self.assertEqual(t.get("api#2")["blockedBy"], [{"identifier": "api#9", "title": "blocker", "open": True}])
        got = t.block("api#1", by="api#9")
        post = [c for c in fake.calls if c[0][1] == "POST" and c[0][2].endswith("/dependencies/blocked_by")]
        self.assertEqual((post[0][0][2], json.loads(post[0][1])), ("repos/acme/api/issues/1/dependencies/blocked_by", {"issue_id": 1009}))
        self.assertEqual([b["identifier"] for b in got["blockedBy"]], ["api#9"])
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}):
            self.assertIsNone(t.block("api#3", by="api#9"))
        self.assertEqual(len([c for c in fake.calls if c[0][1] == "POST"]), 1)

    def test_ready_labels_filter_todo_only(self):
        cfg = dict(GITHUB_CFG, tracker=dict(GITHUB_CFG["tracker"], ready_labels=[["feature", "approved"], ["bug"]]))
        fake = FakeGh({"api": {
            1: gh_issue("api", 1, "bug", ["stage:todo", "bug"]),
            2: gh_issue("api", 2, "approved feature", ["stage:todo", "Feature", "approved"]),
            3: gh_issue("api", 3, "unapproved feature", ["stage:todo", "feature"]),
            4: gh_issue("api", 4, "approved only", ["stage:todo", "approved"]),
            5: gh_issue("api", 5, "design", ["stage:design", "feature"])}})
        t = tracker.GitHubTracker("p", cfg, runner=fake)
        self.assertEqual([x["identifier"] for x in t.list(state="todo")], ["api#1", "api#2"])
        self.assertEqual([x["identifier"] for x in t.list(state="design")], ["api#5"])   # other stages unfiltered

    def test_ident_parsing(self):
        self.assertEqual(self.t.parse_ident("api#1"), ("api", 1))
        self.assertEqual(self.t.parse_ident("https://github.com/acme/app/issues/3"), ("app", 3))
        with self.assertRaises(tracker.TrackerError):
            self.t.parse_ident("#1")          # two issue repos: ambiguous
        with self.assertRaises(tracker.TrackerError):
            self.t.parse_ident("ABC-1")

    def test_move_replaces_stage_labels_only(self):
        got = self.t.move("api#1", "in_review")
        self.assertEqual(got["labels"], ["Claude Ready", "priority:low", "stage:in-review"])
        deletes = [c[0] for c in self.fake.calls if c[0][1] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0][2].endswith("/labels/stage%3Atodo"))
        n_calls = len(self.fake.calls)
        self.t.move("api#1", "In Review")      # already there: no writes
        self.assertEqual([c for c in self.fake.calls[n_calls:] if c[0][1] != "GET"], [])

    def test_assign_comment_create_append(self):
        got = self.t.assign("api#1", me=True)
        self.assertEqual(got["assignee"]["login"], "me")
        self.assertEqual(self.t.comment("api#1", "x")["url"], "https://c/2")
        got = self.t.get("api#1", comments=True)
        self.assertEqual(got["comments"][0]["author"]["login"], "bob")
        with self.assertRaises(tracker.TrackerError):
            self.t.create("t", "b", state="todo")    # two issue repos: --repo required
        got = self.t.create("t", "b", state="todo", labels=["Claude Ready"], repo="app")
        self.assertEqual(got["identifier"], "app#50")
        self.assertEqual(got["labels"], ["Claude Ready", "stage:todo"])
        got = self.t.create("t", "b", state="triage", repo="app", cycle="current")    # no cycles on GitHub: ignored
        self.assertEqual(got["labels"], ["stage:triage"])
        got = self.t.append("api#1", "## Design\nmore")
        self.assertEqual(got["description"], "orig\n\n## Design\nmore\n")

    def test_setup_labels_and_regex(self):
        self.t.setup_labels()
        posts = [c for c in self.fake.calls if c[0][1] == "POST"]
        self.assertEqual(len(posts), 2 * 8)     # 2 swept repos x (queue + 7 stages), 422 on the queue label tolerated
        self.assertEqual(self.t.ticket_regex(), r"((api|app)#[0-9]+|#[0-9]+)")
        self.assertIn("--repo <repo>", self.t.howto())

    def test_dry_run(self):
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}):
            self.assertIsNone(self.t.move("api#1", "in_review"))     # reads the labels first, then stops
            self.assertIsNone(self.t.set_labels("api#1", add=["x"]))
            self.assertIsNone(self.t.create("t", "b", repo="api"))
        self.assertEqual([c for c in self.fake.calls if c[0][1] != "GET"], [])

    def test_gh_failure_names_the_call(self):
        failed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="HTTP 404: Not Found")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaises(tracker.TrackerError) as cm:
                tracker.GitHubTracker._gh(["-X", "GET", "repos/acme/api/issues/9", "-H", "Accept: x"])
        self.assertIn("gh api GET repos/acme/api/issues/9: HTTP 404", str(cm.exception))


class GitHubUnlabeledQueue(unittest.TestCase):
    """A repo that keeps only a `triage` label: no queue label, Todo is "open issue with no stage label"."""
    CFG = {
        "name": "Ops", "forge": {"kind": "github", "org": "acme", "host": "git.example.com"},
        "tracker": {"kind": "github", "stage_labels": {"triage": "triage", "design": "triage", "todo": None},
                    "exclude_labels": ["Question"]},
        "repos": {"op": {}},
    }

    def setUp(self):
        self.fake = FakeGh({"op": {
            1: gh_issue("op", 1, "needs triage", ["triage"]),
            2: gh_issue("op", 2, "ready", ["bug"]),
            3: gh_issue("op", 3, "asking", ["question"]),
            4: gh_issue("op", 4, "running", ["stage:in-progress"]),
            5: gh_issue("op", 5, "triaged while a pass holds it", ["triage", "stage:in-progress"]),
        }})
        self.t = tracker.GitHubTracker("p", self.CFG, runner=self.fake)

    def test_stages_are_labels_or_the_absence_of_one(self):
        self.assertEqual([t["identifier"] for t in self.t.list(state="todo")], ["op#2"])   # not triaged, not in progress, not excluded
        self.assertEqual([t["identifier"] for t in self.t.list(state="design")], ["op#1"])   # op#5 is in progress: no other queue lists it
        self.assertEqual([t["identifier"] for t in self.t.list(state="triage")], ["op#1"])
        self.assertEqual([t["identifier"] for t in self.t.list(state="in_progress")], ["op#4", "op#5"])
        self.assertEqual(self.t.get("op#2")["state"], "Todo")
        self.assertEqual(self.t.get("op#1")["stage"], "triage")

    def test_move_to_the_unlabeled_stage_drops_the_stage_label(self):
        got = self.t.move("op#1", "todo")
        self.assertEqual(got["labels"], [])
        got = self.t.move("op#2", "design")
        self.assertEqual(got["labels"], ["bug", "triage"])

    def test_no_queue_label_and_config(self):
        self.assertEqual(self.t.queue_label, "")
        self.assertEqual(self.t.clone_url("op"), "https://git.example.com/acme/op.git")
        self.assertEqual(self.t.display_name(), "Ops")
        self.assertEqual(self.t.ticket_example(), "op#123")
        self.assertNotIn("Work queue", self.t.howto())
        self.t.setup_labels()
        made = [json.loads(c[1])["name"] for c in self.fake.calls if c[0][1] == "POST" and c[0][2].endswith("/labels")]
        self.assertEqual(sorted(made), ["stage:in-progress", "stage:in-review", "stage:qa", "stage:requires-discussion", "triage"])

    def test_two_unlabeled_stages_are_refused(self):
        cfg = dict(self.CFG, tracker=dict(self.CFG["tracker"], stage_labels={"todo": None, "design": None}))
        with self.assertRaises(tracker.TrackerError):
            tracker.GitHubTracker("p", cfg, runner=self.fake)


class Tasks(unittest.TestCase):
    def tasks(self, cfg, files=()):
        with tempfile.TemporaryDirectory() as d:
            for f in files:
                path = os.path.join(d, f)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    fh.write("#!/bin/sh\n")
                os.chmod(path, 0o755)
            t = tracker.GitHubTracker("p", dict(cfg, context=d), runner=FakeGh({}))
            return {x["name"]: x for x in t.tasks()}

    def test_builtins_need_their_stage_and_config(self):
        got = self.tasks(GITHUB_CFG)
        self.assertEqual(list(got), ["pull", "review", "update", "design", "implement", "reset", "triage"])
        self.assertEqual([n for n, x in got.items() if not x["skip"]], ["pull", "review", "update"])
        self.assertIn("no design stage in tracker.stage_labels", got["design"]["skip"])
        cfg = dict(GITHUB_CFG, tracker=dict(GITHUB_CFG["tracker"], stage_labels={"triage": "untriaged", "todo": None},
                                            triage={"outcomes": [["bug", "approved"], "invalid"]}))
        got = self.tasks(cfg)
        self.assertIsNone(got["implement"]["skip"])
        self.assertEqual(got["triage"]["skip"], "no triage.md in the context folder")
        self.assertIsNone(self.tasks(cfg, files=["triage.md"])["triage"]["skip"])
        t = tracker.GitHubTracker("p", cfg, runner=FakeGh({}))
        self.assertEqual(t.triage_config()["outcomes"], [["bug", "approved"], ["invalid"]])
        self.assertEqual(t.triage_config()["next"], "todo")

    def test_project_tasks(self):
        cfg = dict(GITHUB_CFG, tasks={"monitor": {"script": "scripts/monitor.sh", "every": "10m"}, "nightly": {"script": "n.sh", "every": 60}})
        got = self.tasks(cfg, files=["scripts/monitor.sh"])
        self.assertEqual((got["monitor"]["every"], got["monitor"]["builtin"], got["monitor"]["skip"]), (600, False, None))
        self.assertTrue(got["monitor"]["script"].endswith("/scripts/monitor.sh"))
        self.assertIn("no executable", got["nightly"]["skip"])
        for bad in ({"review": {"script": "r.sh"}}, {"x": {"every": 60}}, {"x": {"script": "x.sh", "every": "soon"}}):
            with self.assertRaises(tracker.TrackerError):
                self.tasks(dict(GITHUB_CFG, tasks=bad))
        self.assertEqual([tracker.seconds(v) for v in (90, "90", "2h", "1d", "30s")], [90, 90, 7200, 86400, 30])


    def test_builtin_intervals_from_config(self):
        got = self.tasks(dict(GITHUB_CFG, tasks={"review": {"every": "1h"}, "update": {"every": 0}}))
        self.assertEqual((got["review"]["every"], got["update"]["every"], got["pull"]["every"]), (3600, 0, 3600))
        self.assertEqual((got["pull"]["scope"], got["review"]["scope"]), ("global", "project"))
        with self.assertRaises(tracker.TrackerError):
            self.tasks(dict(GITHUB_CFG, tasks={"review": {"script": "r.sh"}}))


class Envs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        def write(name, text):
            with open(os.path.join(self.d.name, name), "w") as f:
                f.write(text)
            return os.path.join(self.d.name, name)
        block = "  forge: {kind: github, org: %s}\n  tracker: {kind: github}\n  repos: {r: {}}\n"
        self.paths = [write("a.yaml", "env: staging\na:\n" + block % "o1" + "b:\n" + block % "o2" + "  env: qa\n"),
                      write("c.yaml", "c:\n" + block % "o3")]

    def tearDown(self):
        self.d.cleanup()

    def test_file_env_is_the_default_and_a_project_env_wins(self):
        cfg = tracker.load_config(paths=self.paths)
        self.assertEqual({k: b.get("env") for k, b in cfg.items()}, {"a": "staging", "b": "qa", "c": None})
        self.assertEqual(list(tracker.in_env(cfg, "staging")), ["a"])
        self.assertEqual(list(tracker.in_env(cfg, "")), ["a", "b", "c"])
        with self.assertRaises(tracker.TrackerError):
            tracker.in_env(cfg, "prod")
        with mock.patch.dict(os.environ, {"SDLC_ENV": "qa"}):
            self.assertEqual(tracker.pick_project(cfg)[0], "b")
        self.assertEqual(tracker.GitHubTracker("a", cfg["a"], runner=FakeGh({})).env_name(), "staging")
        self.assertIn("ENV_NAME=staging", tracker.GitHubTracker("a", cfg["a"], runner=FakeGh({})).shell_env())

    def test_env_must_be_a_name(self):
        with open(self.paths[1], "a") as f:
            f.write("env:\n  forge: {kind: github, org: x}\n")
        with self.assertRaises(tracker.TrackerError):
            tracker.load_config(paths=self.paths)


class Cli(unittest.TestCase):
    def test_config_and_errors(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "projects.yaml"), "w") as f:
                f.write("p:\n  forge: {kind: github, org: acme}\n  tracker: {kind: nope}\n  repos: {a: {}}\n")
            with mock.patch.object(tracker, "ROOT", d):
                self.assertEqual(tracker.main(["config", "repos"]), 1)   # bad kind -> exit 1, message on stderr
        with mock.patch.object(tracker, "open_tracker", lambda p, **kw: tracker.GitHubTracker("p", GITHUB_CFG, runner=FakeGh({}))):
            self.assertEqual(tracker.main(["config", "repos"]), 0)
            self.assertEqual(tracker.main(["config", "label-for-repo", "api"]), 1)
            self.assertEqual(tracker.main(["--json", "list", "--state", "todo"]), 0)
        with self.assertRaises(SystemExit) as cm:
            tracker.main(["assign", "x"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
