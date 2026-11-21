"""An engine with the model replaced by a stub, in this process: for tests of nodes, requests and the viewer's API.

`engine(test, home, **env)` loads pangoloop/graph.py afresh with the given SG_* environment (the constants are read at
import), points its runs folder at `home/runs` and makes it quick. `stub_class(sg, behave)` is a Run whose workers are
`behave(node_id, prompt, cwd, calls) -> answer or None` (None: `done`); every call is kept in `calls`.
"""
import importlib.util
import itertools
import os
import time

import support

_COUNTER = itertools.count(1)


def engine(test, home: str, **env):
    for key in [k for k in os.environ if k.startswith('SG_')] + ['PANGOLOOP_RUNNER', 'PANGOLOOP_RUNS']:
        os.environ.pop(key, None)
    os.environ.update(env)
    test.addCleanup(lambda: [os.environ.pop(k, None) for k in [k for k in os.environ if k.startswith('SG_')]])
    spec = importlib.util.spec_from_file_location(f'graph_stub_{next(_COUNTER)}',
                                                  os.path.join(support.ROOT, 'pangoloop', 'graph.py'))
    sg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sg)
    sg.RUNS = f'{home}/runs'
    sg.START_GAP_SECONDS = 0
    sg.HUMAN_POLL_SECONDS = 0.05
    return sg


def stub_class(sg, behave=None):
    calls: list = []

    class StubRun(sg.Run):
        def worker(self, node_id, category, prompt, cwd=None):
            calls.append({'node': node_id, 'prompt': prompt, 'cwd': cwd, 'at': time.time()})
            answer = (behave(node_id, prompt, cwd, calls) if behave else None) or 'done'
            record = {'worker': f'{len(calls):02d}-{node_id}', 'node': node_id, 'category': category, 'model': 'stub',
                      'reported_model': None, 'started': 0, 'seconds': 0, 'exit': 0, 'limit_waits': 0, 'ok': True,
                      'answer': answer}
            self.records.append({k: v for k, v in record.items() if k != 'answer'})
            calls[-1]['ended'] = time.time()
            return record

    StubRun.calls = calls
    return StubRun


def tree(folder: str) -> dict:
    """Every file under `folder` (not .git) as {relative path: text or link target}, for before/after comparisons."""
    found = {}
    for here, folders, files in os.walk(folder):
        folders[:] = [name for name in folders if name != '.git']
        for name in [n for n in files if n != '.git'] + [n for n in folders if os.path.islink(os.path.join(here, n))]:
            path = os.path.join(here, name)
            rel = os.path.relpath(path, folder)
            found[rel] = os.readlink(path) if os.path.islink(path) else open(path, errors='replace').read()
    return found
