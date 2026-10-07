# Sourced by every runner after it sets ROOT (this folder): reads the project config once and
# sets what the scripts share. Nothing here names a project, an org or a host.
#   TRACKER      the ticket CLI            REPOS_ROOT   where the clones live (default: parent of ROOT)
#   PROJECT      the project key           ENV_NAME     its environment (`env:`), may be empty
#                                          state_file N  .N-cache.<project>.json, this project's cache
#   NAME ORG HOST  project name, forge org and host     CONTEXT      the project's docs folder
#   KIND         linear | github           QUEUE        queue label, empty when the stage alone is the queue
#   EXAMPLE      a sample ticket id        BLOCKER      release-blocker label, may be empty
#   QUEUE_ARGS   `--label $QUEUE` or nothing   CLONE_BASE  https://<host>/<org>, where <repo>.git is fetched
#   MCP_TOOLS    the Linear MCP tool rule, or nothing
TRACKER="$ROOT/scripts/tracker.py"
REPOS_ROOT=${REPOS_ROOT:-$(cd "$ROOT/.." && pwd)}   # override when running from a worktree of this folder
export REPOS_ROOT
# Several projects and none picked: rerun the calling script once per project, one after the other,
# looping here when INTERVAL is set; SDLC_ENV narrows that to one environment's projects. A target
# argument (a ticket, a repo) needs SDLC_PROJECT instead.
if [ -z "${SDLC_PROJECT:-}" ]; then
  _projects=$("$TRACKER" config projects) || exit 1
  if [ "$(printf '%s\n' "$_projects" | wc -l)" -gt 1 ]; then
    for _a in ${1+"$@"}; do
      case $_a in -*) ;; *) echo "several projects ($(echo $_projects)): set SDLC_PROJECT for $_a" >&2; exit 2 ;; esac
    done
    [ -z "${ALL_CYCLES:-}" ] || export ALL_CYCLES   # design-tickets.sh has shifted --all-cycles off already
    _every=${INTERVAL:-}
    while :; do
      _rc=0
      for _p in $_projects; do
        _e=$("$TRACKER" --project "$_p" config env-name) || _e=
        echo "== $_p${_e:+ (env $_e)}"
        SDLC_PROJECT=$_p INTERVAL= "$0" ${1+"$@"} || _rc=$?
      done
      [ -n "$_every" ] || exit "$_rc"
      sleep "$_every"
    done
  fi
fi
eval "$("$TRACKER" config env)"
export SDLC_PROJECT=$PROJECT                          # sessions and helpers stay on this project
"$ROOT/scripts/prune-logs.sh"                         # old run logs out, at most once a day
[ "$HOST" = github.com ] || export GH_HOST=$HOST    # gh talks to the project's forge, GitHub Enterprise included
CLONE_BASE="https://$HOST/$ORG"
QUEUE_ARGS=(); [ -z "$QUEUE" ] || QUEUE_ARGS=(--label "$QUEUE")
MCP_TOOLS=; [ "$KIND" != linear ] || MCP_TOOLS="mcp__linear-server,"
# Exit 0 when this project's config doesn't turn the calling runner's task on (`tracker.py config tasks`).
need_task() {
  local why
  why=$("$TRACKER" --json config tasks | jq -r --arg t "$1" '.[] | select(.name == $t) | .skip // empty') || exit 1
  [ -z "$why" ] || { echo "$PROJECT: $1 skipped, $why"; exit 0; }
}
# A runner's mkdir lock may hold the pid of the pass that took it (`pid` inside the directory).
# Live when that pid still runs. A lock with no pid file is live for 6h, after which the pass
# that left it is assumed dead.
lock_live() { # lockdir
  local pid; pid=$(cat "$1/pid" 2>/dev/null) || pid=
  if [ -n "$pid" ]; then kill -0 "$pid" 2>/dev/null; return; fi
  [ -d "$1" ] && [ -z "$(find "$1" -maxdepth 0 -mmin +360 2>/dev/null)" ]
}
# A runner's cache, one per project. The unsuffixed file from before is adopted on first use.
state_file() {
  local f="$ROOT/.$1-cache.$PROJECT.json"
  [ -e "$f" ] || [ ! -e "$ROOT/.$1-cache.json" ] || mv "$ROOT/.$1-cache.json" "$f"
  echo "$f"
}
