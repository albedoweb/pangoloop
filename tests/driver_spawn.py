"""Test helper, not a test: `pangoloop run` with the real process machinery (the runner's command is really started) but
with no pause between session starts, so that a stub command can be used without the engine's 4 second gap.

    PANGOLOOP_RUNNER=claude-code PANGOLOOP_CLAUDE_COMMAND=<stub> python3 tests/driver_spawn.py <plan> <repo> <run name>
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pangoloop import graph  # noqa: E402

graph.START_GAP_SECONDS = 0
graph.HUMAN_POLL_SECONDS = 0.05
if os.environ.get('STUB_FAST_REFUSAL'):
    graph.RUNNER.refusal_wait_seconds = 0.05   # the wait after a refusal, for a test
sys.exit(graph.main(sys.argv[1:]))
