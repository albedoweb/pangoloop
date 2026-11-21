"""Helpers of the tests of the project commands (init, short run, detached runs, status, the page, roles): a temporary project
made by `pangoloop init`, the toy repository, a stand-in for the model, and `pangoloop` run as a command."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

import support

STUB = os.path.join(support.ROOT, 'tests', 'claude_stub.py')
TOY_PLAN = os.path.join(support.ROOT, 'examples', 'toy', 'plan.md')

CALC = 'def add(a, b):\n    return a + b\n'
TEST_CALC = ('import unittest\n\nfrom calc import add\n\n\nclass CalcTest(unittest.TestCase):\n    def test_add(self):\n'
             '        self.assertEqual(add(2, 3), 5)\n\n\nif __name__ == \'__main__\':\n    unittest.main()\n')

# a plan with no model worker: two script nodes and an acceptance; it runs in about a second
SCRIPT_PLAN = '''# Plan: scripts only

Repository: `{{REPO}}`, branch `{{BRANCH}}`.

All nodes work in the same folder, the repository folder.

## Nodes

### one
- label: First check
- kind: script
- depends_on: none
- write_scope: nothing
- STEPS: run `true`

### accept
- label: Acceptance
- kind: acceptance
- depends_on: one
- write_scope: nothing
- TASK: the script runs the command in VERIFY.
- DELIVERABLE: exit 0.
- SCOPE: the repository.
- VERIFY: `true`
- STOP WHEN: the command exits 0.

## Task statement

Nothing to do.
'''


def environment(**extra) -> dict:
    """A clean environment for `pangoloop` as a command: no variable of the package, no routes file of the suite (the project's
    own pangoloop.toml is what is read), no notifications."""
    env = support.clean_environment(RUN_VIEW_NOTIFY='0', SG_HUMAN_NOTIFY='0', **extra)
    env.pop('PANGOLOOP_CONFIG', None)
    return env


def git(cwd: str, *args: str) -> str:
    done = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout


def toy_repository(root: str, name: str = 'proj') -> str:
    """A git repository with a commit, holding the toy calculator (calc.py and its test)."""
    folder = os.path.join(root, name)
    os.makedirs(folder)
    git(folder, 'init', '-q', '-b', 'main')
    for path, text in (('calc.py', CALC), ('test_calc.py', TEST_CALC)):
        with open(os.path.join(folder, path), 'w') as handle:
            handle.write(text)
    git(folder, 'add', '-A')
    git(folder, 'commit', '-q', '-m', 'toy calculator')
    return folder


def stub_command(root: str) -> str:
    """The `claude` command of the stand-in runs: a script that runs tests/claude_stub.py."""
    path = os.path.join(root, 'claude')
    with open(path, 'w') as handle:
        handle.write(f'#!/bin/sh\nexec {sys.executable} {STUB} "$@"\n')
    os.chmod(path, 0o755)
    return path


def pangoloop(*args: str, cwd: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-m', 'pangoloop', *args], capture_output=True, text=True, cwd=cwd,
                          env=env or environment(), timeout=timeout)


def make_project(test, name: str = 'proj', preset: str = 'claude-code-only', *init_args: str) -> tuple[str, dict]:
    """(the project folder, the environment to run commands in it with the stub standing in for the model). The folder is a git
    repository with the toy files, `pangoloop init` has been run in it, and its pangoloop.toml, .gitignore, plans and roles are
    committed (an untracked file would be carried through the run)."""
    home = support.temp_dir(test)
    folder = toy_repository(home, name)
    env = environment(PANGOLOOP_CLAUDE_COMMAND=stub_command(home), STUB_LOG=os.path.join(home, f'{name}-stub.log'))
    done = pangoloop('init', '--preset', preset, *init_args, cwd=folder, env=env)
    assert done.returncode == 0, done.stderr
    git(folder, 'add', '-A')
    git(folder, 'commit', '-q', '-m', 'pangoloop project')
    test.addCleanup(stop_page, folder)
    return folder, env


def write_plan(folder: str, name: str, text: str) -> str:
    path = os.path.join(folder, '.pangoloop', 'plans', name)
    with open(path, 'w') as handle:
        handle.write(text)
    return path


def read_json(path: str):
    with open(path) as handle:
        return json.load(handle)


def stop_page(folder: str) -> None:
    """Ends the page of the project, if one is serving."""
    try:
        pid = read_json(os.path.join(folder, '.pangoloop', 'view.json'))['pid']
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass


def wait_for(check, seconds: float = 60.0, pause: float = 0.3):
    """The first truthy result of check(), polled; None when the time runs out."""
    end = time.time() + seconds
    while time.time() < end:
        found = check()
        if found:
            return found
        time.sleep(pause)
    return None


def run_state(folder: str, env: dict, name: str | None = None) -> dict:
    done = pangoloop('status', *([name] if name else []), '--json', cwd=folder, env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def wait_until_over(folder: str, env: dict, name: str, seconds: float = 90.0) -> dict:
    """`status <name> --json` until the run is no longer running; that object."""
    last = {}

    def check():
        nonlocal last
        last = run_state(folder, env, name)
        return last if last['state'] != 'running' else None
    found = wait_for(check, seconds, 0.5)
    assert found, f'the run did not end in {seconds} seconds: {last}'
    return found


def fetch(url: str, timeout: float = 10) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read().decode()
