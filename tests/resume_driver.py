"""Test helper, not a test: `pangoloop run` (with `--resume` when given) with the model replaced by a scripted stub, in a
process of its own so that it can be killed with `kill -9`.

    STUB_DIR=<folder> [STUB_BEHAVIOR=<json file>] python3 tests/resume_driver.py <plan> <repository> <run name> [flags]

Every worker call is appended to $STUB_DIR/calls.jsonl (node, tag, cwd, prompt, action). The action of a call is picked
from STUB_BEHAVIOR, a JSON object {node id: [action, ...]}: the n-th call of that node (counted over all processes, so a
resumed run goes on in the list) takes the n-th action, the last one repeating. A node that is not listed does `ok` (a
reviewer: `pass`). Actions:
  ok            writes <node>.txt in the node's folder and says done
  fail          exits with an error and says nothing
  sleep         touches $STUB_DIR/<node>.started and sleeps a minute (to be killed)
  delay:<s>     waits s seconds, then as ok
  ask           ends with a NEEDS HUMAN line (a request for the person)
  changes, pass the reviewer's verdicts
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pangoloop import graph  # noqa: E402

graph.START_GAP_SECONDS = 0
graph.HUMAN_POLL_SECONDS = 0.05
DIR = os.environ['STUB_DIR']
CALLS = os.path.join(DIR, 'calls.jsonl')
BEHAVIOR = json.load(open(os.environ['STUB_BEHAVIOR'])) if os.environ.get('STUB_BEHAVIOR') else {}


class StubRun(graph.Run):
    def worker(self, node_id, category, prompt, cwd=None):
        before = [json.loads(line) for line in open(CALLS)] if os.path.exists(CALLS) else []
        number = sum(1 for call in before if call['node'] == node_id)
        tag = self.next_tag(node_id)
        actions = BEHAVIOR.get(node_id)
        action = actions[min(number, len(actions) - 1)] if actions else ('pass' if category == 'review' else 'ok')
        with open(CALLS, 'a') as handle:
            handle.write(json.dumps({'node': node_id, 'tag': tag, 'cwd': cwd, 'prompt': prompt, 'action': action}) + '\n')
        folder = f'{self.out}/workers/{tag}'
        os.makedirs(folder)
        with open(f'{folder}/prompt.txt', 'w') as handle:
            handle.write(prompt)
        ok, answer = True, 'done'
        if action == 'fail':
            ok, answer = False, ''
        elif action == 'sleep':
            open(os.path.join(DIR, f'{node_id}.started'), 'w').close()
            time.sleep(60)
        elif action == 'ask':
            answer = 'NEEDS HUMAN: which one?'
        elif action == 'changes':
            answer = 'VERDICT: CHANGES REQUESTED\nFINDING 1: something'
        elif action == 'pass':
            answer = 'VERDICT: PASS'
        else:
            if action.startswith('delay:'):
                time.sleep(float(action.split(':', 1)[1]))
            with open(os.path.join(cwd or self.repo, f'{node_id}.txt'), 'w') as handle:
                handle.write(node_id)
        with open(f'{folder}/answer.md', 'w') as handle:
            handle.write(answer)
        record = {'worker': tag, 'node': node_id, 'category': category, 'model': 'stub', 'reported_model': None,
                  'started': 0, 'seconds': 1, 'exit': 0 if ok else 1, 'limit_waits': 0, 'ok': ok,
                  'usage': {'input': 10, 'output': 5, 'cost_usd': 0.01}, 'answer': answer}
        self.records.append({key: value for key, value in record.items() if key != 'answer'})
        self.save_state()
        return record


graph.Run = StubRun
sys.exit(graph.main(sys.argv[1:]))
