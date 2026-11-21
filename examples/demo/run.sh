#!/bin/sh
# The demonstration run: a small repository in a temporary folder, the plan examples/demo/plan.md (two build nodes side by side,
# a script node, an acceptance, a review), and a stand-in for the model, so that it costs nothing and shows the page with a whole
# run on it. From the project folder:
#
#     sh examples/demo/run.sh                 # runs it, prints the page's address and the verdict, stops the page
#     DEMO_KEEP=1 sh examples/demo/run.sh     # leaves the page serving (stop it with `kill <pid>`, the pid is printed)
#     DEMO_DIR=/tmp/demo sh examples/demo/run.sh   # use (and keep) that folder; it must be new or empty
#     DEMO_PORT=5470 sh examples/demo/run.sh  # serve the page on that port (default: the first free one from 5420)
#
# Needs git, sh and Python 3.11 or newer. The stand-in is tests/claude_stub.py: it writes no real code, it plays the workers
# (each takes a few seconds, so that the timeline has something to show). Exit status: 0 when the run was verified.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=${DEMO_DIR:-$(mktemp -d)}
mkdir -p "$WORK"
REPO=$WORK/repo
mkdir "$REPO"
cp "$HERE"/files/*.py "$REPO"/
cp "$HERE/files/gitignore" "$REPO/.gitignore"
cd "$REPO"
git init -q -b main
git config user.name demo
git config user.email demo@example.invalid
git add -A
git commit -q -m "text statistics"
export PYTHONPATH="$ROOT"
python3 -m pangoloop init --preset claude-code-only > /dev/null
if [ -n "${DEMO_PORT:-}" ]; then printf '\n[view]\nport = %s\n' "$DEMO_PORT" >> pangoloop.toml; fi
cp "$HERE/plan.md" .pangoloop/plans/demo.md
git add -A
git commit -q -m "pangoloop project"
printf '#!/bin/sh\nexec python3 "%s/tests/claude_stub.py" "$@"\n' "$ROOT" > "$WORK/claude"
chmod +x "$WORK/claude"
export PANGOLOOP_CLAUDE_COMMAND="$WORK/claude" STUB_LOG="$WORK/stub.log" STUB_NO_COST=1 STUB_ECHO_MODEL=1
export STUB_DELAY=${STUB_DELAY:-4}
echo "demo folder: $WORK"
python3 -m pangoloop run demo --detach --view --json > "$WORK/started.json"
RUN=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['run'])" "$WORK/started.json")
PAGE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['page'])" "$WORK/started.json")
echo "run: $RUN"
echo "page: $PAGE"
while [ "$(python3 -m pangoloop status "$RUN" --json | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")" = running ]; do
    sleep 2
done
python3 -m pangoloop status "$RUN"
VERDICT=$(python3 -m pangoloop status "$RUN" --json | python3 -c "import json,sys; print(json.load(sys.stdin)['verdict'])")
VIEW_PID=$(python3 -c "import json; print(json.load(open('.pangoloop/view.json'))['pid'])")
if [ "${DEMO_KEEP:-}" = 1 ]; then
    echo "the page keeps serving at $PAGE (pid $VIEW_PID)"
else
    kill "$VIEW_PID" 2>/dev/null || true
fi
[ "$VERDICT" = verified ]
