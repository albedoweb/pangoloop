"""More helpers of the tests (step 2): a repository, plan nodes and plan files, and a run of the engine with a stub."""
import json
import os
import subprocess
import sys

import support


def vcs(cwd: str, *args: str) -> str:
    """One version-control command in `cwd`, with a fixed identity; its output."""
    done = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, capture_output=True,
                          text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout


def make_repo(root: str, name: str = 'repo', files: dict | None = None) -> str:
    """A repository on branch `work` with one commit holding `files` (path -> text; default base.txt)."""
    repo = os.path.join(root, name)
    os.makedirs(repo)
    vcs(repo, 'init', '-q', '-b', 'work')
    for path, text in (files or {'base.txt': 'base\n'}).items():
        os.makedirs(os.path.dirname(os.path.join(repo, path)) or repo, exist_ok=True)
        with open(os.path.join(repo, path), 'w') as handle:
            handle.write(text)
    vcs(repo, 'add', '-A')
    vcs(repo, 'commit', '-q', '-m', 'base')
    return repo


def node_text(node_id: str, role: str = 'build', deps: str = 'none', category: str = 'deep', **fields) -> str:
    """One plan node. Keyword fields override the defaults (`STOP_WHEN` is the field `STOP WHEN`); None leaves a field out."""
    values = {'label': node_id, 'category': category, 'role': role, 'depends_on': deps, 'write_scope': 'x',
              'TASK': 'do ' + node_id, 'DELIVERABLE': 'd', 'SCOPE': 's', 'VERIFY': 'v', 'STOP WHEN': 'w'}
    values.update({('STOP WHEN' if key == 'STOP_WHEN' else key): value for key, value in fields.items()})
    return f'### {node_id}\n' + ''.join(f'- {key}: {value}\n' for key, value in values.items() if value is not None)


def script_node(node_id: str, steps: str, deps: str = 'none', **fields) -> str:
    """A node with the role script: only the fields it needs."""
    return node_text(node_id, role='script', deps=deps, category=None, TASK=None, DELIVERABLE=None, SCOPE=None,
                     VERIFY=None, STOP_WHEN=None, STEPS=steps, **fields)


def write_plan(root: str, *nodes: str, before: str = '', after: str = '', name: str = 'plan.md') -> str:
    """A plan file with the nodes; `before` goes between the preamble and `## Nodes`, `after` after the task statement."""
    path = os.path.join(root, name)
    with open(path, 'w') as handle:
        handle.write('# Plan\n\nRepository: `{{REPO}}`.\n\nAll nodes work in the same folder.\n\n' + before
                     + '\n## Nodes\n\n' + '\n'.join(nodes) + '\n## Task statement\n\nnothing\n' + after)
    return path


DRIVER = os.path.join(support.ROOT, 'tests', 'driver.py')


def drive(args: list, mode: str = 'ok', cwd: str | None = None, env: dict | None = None, **extra) -> subprocess.CompletedProcess:
    """`pangoloop run` with the stub (tests/driver.py) in a process of its own; `args` are plan, repository, run name."""
    environment = support.clean_environment(STUB_MODE=mode, **(env or {}))
    return subprocess.run([sys.executable, DRIVER, *args], capture_output=True, text=True, env=environment,
                          cwd=cwd or support.ROOT, timeout=120, **extra)


def read_json(path: str):
    with open(path) as handle:
        return json.load(handle)
