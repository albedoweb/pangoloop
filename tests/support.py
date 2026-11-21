"""Shared helpers of the tests: the repository root on sys.path, a clean environment, a fake run folder."""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# A run's routes are placeholders until a file names them, and a run with a placeholder route stops before it starts. The
# suite runs engines with stand-in runners, so every test process starts with a routes file of stand-in model names; a test
# that wants another file (or `none`) sets PANGOLOOP_CONFIG itself.
os.environ['PANGOLOOP_CONFIG'] = os.path.join(ROOT, 'tests', 'fixtures', 'routes-hermes.toml')

# Variables that change what the engine, the viewer or the runner do; a test never inherits them.
CONTROL_VARIABLES = ('PANGOLOOP_RUNS', 'PANGOLOOP_RUNNER', 'PANGOLOOP_CONFIG', 'PANGOLOOP_CLAUDE_COMMAND',
                     'PANGOLOOP_MAX_COST_USD', 'PANGOLOOP_COORDINATOR_WAIT_SECONDS', 'PANGOLOOP_RULINGS_CHARS',
                     'RUN_VIEW_RUNS', 'RUN_VIEW_REPORTS', 'RUN_VIEW_NOTIFY', 'SG_HUMAN_NOTIFY')


def clean_environment(**extra: str) -> dict:
    env = {key: value for key, value in os.environ.items()
           if key not in CONTROL_VARIABLES and not key.startswith('SG_')}
    env['PYTHONPATH'] = ROOT + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    env.update(extra)
    # the routes of a run are placeholders until a file names them: give the stand-in runs one, unless the test brings its own
    fixture = 'routes-claude.toml' if env.get('PANGOLOOP_RUNNER') == 'claude-code' else 'routes-hermes.toml'
    if 'PANGOLOOP_CONFIG' not in env:
        env['PANGOLOOP_CONFIG'] = os.path.join(ROOT, 'tests', 'fixtures', fixture)
    return env


def make_run(root: str, name: str = 'fake-run', requests: dict | None = None, over: bool = False) -> str:
    """A fake run folder: plan.md, started_at and, for each `requests[stem] = info`, human/<stem>.json."""
    folder = os.path.join(root, name)
    os.makedirs(os.path.join(folder, 'human'))
    os.makedirs(os.path.join(folder, 'workers'))
    plan = ('# Plan\n\n## Nodes\n\n### a\n- label: A\n- category: deep\n- role: build\n- depends_on: none\n'
            '- write_scope: x\n- TASK: t\n- DELIVERABLE: d\n- SCOPE: s\n- VERIFY: v\n- STOP WHEN: w\n\n'
            '## Task statement\n\nnothing\n')
    with open(os.path.join(folder, 'plan.md'), 'w') as handle:
        handle.write(plan)
    with open(os.path.join(folder, 'started_at'), 'w') as handle:
        handle.write('2026-11-18T10:00:00Z\n')
    for stem, info in (requests or {}).items():
        with open(os.path.join(folder, 'human', stem + '.json'), 'w') as handle:
            json.dump(info, handle)
    if over:
        with open(os.path.join(folder, 'finished_at'), 'w') as handle:
            handle.write('2026-11-18T11:00:00Z\n')
        with open(os.path.join(folder, 'result.json'), 'w') as handle:
            json.dump({'verdict': 'verified', 'nodes': {'a': 'accepted'}, 'workers': []}, handle)
    return folder


def request_info(node: str = 'a', question: str = 'which region?\nsecond line', state: str = 'open',
                 asked_at: str = '2026-11-18T10:05:00Z') -> dict:
    return {'node': node, 'worker': f'01-{node}', 'asked_at': asked_at, 'request': question,
            'answer_tail': 'the end of the answer', 'state': state}


def temp_dir(test) -> str:
    handle = tempfile.TemporaryDirectory(prefix='pangoloop-test-')
    test.addCleanup(handle.cleanup)
    return os.path.realpath(handle.name)


def quiet_resource_warnings() -> None:
    """For setUpModule: the engine reads small files with a bare open(); its ResourceWarnings are noise in a test run."""
    import warnings
    warnings.simplefilter('ignore', ResourceWarning)
