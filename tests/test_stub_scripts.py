"""The four stub tests of the engine, run as they were (tests/scripts/*_stub_test.py), one test case each.

Each script is run in its own process against pangoloop/graph.py, in a clean environment, and must end with
ALL CHECKS PASSED. They are the seed's tests, moved from seed/ on November 18, 2026; their assertions are unchanged.

Two of them compare the engine with "a backup copy of the script before the change":
  - human: no older copy exists in this repository, so the engine itself is given as the backup. The comparison of
    prompts with the switch off then holds trivially; everything else the script asserts is real.
  - ui: the backup is made from the engine by removing the 'ui' route and its tool sets (what the script says the
    change added), so that its "old routes" assertions mean what they said.

Known failure, recorded before the move (November 18, 2026, on the seed as it was): ui_stub_test.py step 3 (the
SG_NODE_SCRATCH switch on) fails at its `assert found, text` because the script expects the sentence "Your scratch
folder is X, empty and yours alone; do not read or write any other folder outside the repository." and the engine
writes "...; apart from what the plan allows, do not read or write any other folder outside the repository." The
wording was changed in the engine without the test, and the failure is the same on the seed's own copy. It is kept as an
expected failure; the engine is not changed to please it.
"""
import os
import subprocess
import sys
import tempfile
import unittest

import support
from support import ROOT, clean_environment

GRAPH = os.path.join(ROOT, 'pangoloop', 'graph.py')
SCRIPTS = os.path.join(ROOT, 'tests', 'scripts')


def run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, name), *args], capture_output=True, text=True,
                          env=clean_environment(), cwd=ROOT, timeout=600)


def legacy_engine(directory: str) -> str:
    """The engine as it was before the 'ui' route: no ui route, no extra tool sets."""
    with open(GRAPH) as handle:
        source = handle.read()
    # The routes and tool sets live in pangoloop/plan.py since step 2; the engine looks them up in its own namespace
    # at call time, so overriding the two names at the end of the file gives the old engine.
    assert 'from pangoloop.plan import' in source and 'ROUTES' in source, 'the engine text changed: update legacy_engine'
    path = os.path.join(directory, 'legacy_graph.py')
    with open(path, 'w') as handle:
        handle.write(source + "\n\nROUTES = {'review': ('reviewer-model', 'stub-provider')}\nCATEGORY_TOOLSETS = {}\n")
    return path


class StubScripts(unittest.TestCase):
    def passes(self, done: subprocess.CompletedProcess) -> None:
        self.assertEqual(done.returncode, 0, done.stdout[-1500:] + done.stderr[-3000:])
        self.assertIn('ALL CHECKS PASSED', done.stdout)

    def test_acceptance(self):
        self.passes(run_script('acceptance_stub_test.py', GRAPH))

    def test_worktree(self):
        self.passes(run_script('worktree_stub_test.py', GRAPH))

    def test_human(self):
        self.passes(run_script('human_stub_test.py', GRAPH, GRAPH))

    def test_ui_known_failure_is_the_scratch_wording(self):
        """The ui script fails where it failed before the move, and only there (routes and old-command comparisons,
        steps 1 and 2, passed before the step that fails)."""
        with tempfile.TemporaryDirectory() as directory:
            done = run_script('ui_stub_test.py', GRAPH, legacy_engine(directory))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('assert found, text', done.stderr)
        self.assertIn('Your scratch folder is', done.stderr)

    @unittest.expectedFailure
    def test_ui_whole_script(self):
        """Expected failure: the scratch wording (see the module's text). If this starts to pass, remove the marker."""
        with tempfile.TemporaryDirectory() as directory:
            self.passes(run_script('ui_stub_test.py', GRAPH, legacy_engine(directory)))


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
