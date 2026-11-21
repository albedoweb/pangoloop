#!/bin/sh
# The toy run: a toy repository in a temporary folder, the plan examples/toy/plan.md, the Claude Code runner (examples/presets/claude-code-only.toml).
# From the project folder:
#
#     TOY_STUB=1 sh examples/toy/run.sh   # NO MODEL: the stand-in used by the tests (tests/claude_stub.py) plays the
#                                         # workers; it proves the engine's wiring, it does not write the toy's code
#     sh examples/toy/run.sh              # a real run, a handful of tiny model calls
#     TOY_DIR=/tmp/toy sh examples/toy/run.sh   # use (and keep) that folder; it must not hold a run named toy-1
#
# The real run needs `claude` on the PATH, logged in (PANGOLOOP_CLAUDE_COMMAND names another command). A new temporary
# folder each time; the script prints where it is and the verdict. Exit status: pangoloop's.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=${TOY_DIR:-$(mktemp -d)}
REPO=$WORK/repo
mkdir -p "$REPO"
cd "$REPO"
git init -q -b main
git config user.name toy
git config user.email toy@example.invalid
cat > calc.py <<'EOF'
def add(a, b):
    return a + b
EOF
cat > test_calc.py <<'EOF'
import unittest

from calc import add


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == '__main__':
    unittest.main()
EOF
git add -A
git commit -q -m "toy calculator"
echo "toy folder: $WORK"
cd "$WORK"
export PYTHONPATH="$ROOT"
if [ "${TOY_STUB:-}" = 1 ]; then
    printf '#!/bin/sh\nexec python3 "%s/tests/claude_stub.py" "$@"\n' "$ROOT" > "$WORK/claude"
    chmod +x "$WORK/claude"
    export PANGOLOOP_CLAUDE_COMMAND="$WORK/claude" STUB_LOG="$WORK/stub.log" STUB_NO_COST=1
fi
export PANGOLOOP_RUNS="$WORK/runs"
export SG_NODE_WORKTREES=1
export SG_MAX_TURNS=20
python3 -m pangoloop run --config "$ROOT/examples/presets/claude-code-only.toml" "$HERE/plan.md" "$REPO" toy-1
