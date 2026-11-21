"""Test helper, not a test: `pangoloop run` with the model replaced by a stub, so that the command line, the signals and
the exit codes can be tried in a process of their own.

    STUB_MODE=<mode> python3 tests/driver.py <plan> <repository> <run name>

Modes: ok (every worker says done; a reviewer says VERDICT: PASS), fail (every worker fails), changes (a reviewer
always requests changes), ask (a worker asks the person; a thread answers ABORT), sleep (the first worker touches
$STUB_MARK and sleeps a minute). The runs folder is PANGOLOOP_RUNS or the default, as in the real command.
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pangoloop import graph  # noqa: E402

MODE = os.environ.get('STUB_MODE', 'ok')
graph.START_GAP_SECONDS = 0
graph.HUMAN_POLL_SECONDS = 0.05


class StubRun(graph.Run):
    count = 0

    def worker(self, node_id, category, prompt, cwd=None):
        StubRun.count += 1
        ok, answer = True, 'done'
        if MODE == 'fail':
            ok, answer = False, ''
        elif category == 'review':
            answer = 'VERDICT: CHANGES REQUESTED\nFINDING 1: no' if MODE == 'changes' else 'VERDICT: PASS'
        elif MODE == 'ask':
            answer = 'NEEDS HUMAN: which one?'
        elif MODE == 'sleep':
            open(os.environ['STUB_MARK'], 'w').close()
            time.sleep(60)
        return {'worker': f'{StubRun.count:02d}-{node_id}', 'node': node_id, 'category': category, 'model': 'stub',
                'reported_model': None, 'started': 0, 'seconds': 0, 'exit': 0 if ok else 1, 'limit_waits': 0, 'ok': ok,
                'answer': answer}


def answer_abort(run):
    folder = f'{run.out}/human'
    while True:
        for entry in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if entry.endswith('.json'):
                with open(f'{folder}/{entry[:-5]}.answer.txt', 'w') as handle:
                    handle.write('ABORT\nnot today\n')
                return
        time.sleep(0.03)


if MODE == 'ask':
    original = StubRun.__init__

    def init(self, *args):
        original(self, *args)
        threading.Thread(target=answer_abort, args=(self,), daemon=True).start()
    StubRun.__init__ = init

graph.Run = StubRun
sys.exit(graph.main(sys.argv[1:]))
