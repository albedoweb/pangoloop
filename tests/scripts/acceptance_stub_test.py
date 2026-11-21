"""Checks the scripted acceptance node of script_graph.py with no model.

Plan: two build nodes side by side in own copies, then `acceptance` (role acceptance, two commands), then review,
then `acceptance-final`. The first acceptance fails on its second command until a fix worker repairs a file; one
command hangs past the limit once. Expected: one fix round, the hung command stopped and reported, all accepted.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

os.environ.update(SG_NODE_WORKTREES='1', SG_MAX_PARALLEL='2', SG_ACCEPTANCE_SECONDS='4', SG_MAX_FIX_ROUNDS='3')
spec = importlib.util.spec_from_file_location('sg', sys.argv[1])
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)

home = tempfile.mkdtemp(prefix='sg-acc-')
sg.PILOT = f'{home}/pilot'
sg.RUNS = f'{sg.PILOT}/runs'
sg.START_GAP_SECONDS = 0
os.makedirs(f'{sg.PILOT}/runs')
repo = f'{home}/repo'
os.makedirs(repo)


def sh(*args: str) -> str:
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, check=True).stdout


sh('git', 'init', '-q', '-b', 'work')
open(f'{repo}/base.txt', 'w').write('base\n')
sh('git', 'add', '-A')
sh('git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'base')
base = sh('git', 'rev-parse', 'HEAD').strip()


def node(node_id: str, role: str, deps: str, verify: str = 'v') -> str:
    return (f'### {node_id}\n- label: {node_id}\n- category: {"review" if role == "review" else "deep"}\n- role: {role}\n'
            f'- depends_on: {deps}\n- write_scope: x\n- TASK: do {node_id}\n- DELIVERABLE: d\n- SCOPE: s\n'
            f'- VERIFY: {verify}\n- STOP WHEN: w\n')


checks = 'the commands `test -f a.txt && test -f b.txt` and `sh -c "if [ -f slow.flag ]; then sleep 30; fi; grep -q good b.txt"` exit 0.'
plan = ('# Plan\n\nRepository: `{{REPO}}`.\n\nAll nodes work in the same folder. Two.\n\n## Nodes\n\n'
        + '\n'.join([node('a', 'build', 'none'), node('b', 'build', 'none'), node('acceptance', 'acceptance', 'a, b', checks),
                     node('review', 'review', 'acceptance'), node('acceptance-final', 'acceptance', 'review', checks)])
        + '\n## Task statement\n\nnothing\n')
open(f'{home}/plan.md', 'w').write(plan)
seen = []


class StubRun(sg.Run):
    def worker(self, node_id, category, prompt, cwd=None):
        folder = cwd or self.repo
        seen.append(node_id)
        answer = 'done'
        if node_id == 'a':
            open(f'{folder}/a.txt', 'w').write('a')
        elif node_id == 'b':
            open(f'{folder}/b.txt', 'w').write('bad')
            open(f'{folder}/slow.flag', 'w').write('x')
        elif node_id == 'acceptance-fix1':
            assert folder == self.repo and 'EXIT CODE: 124' in prompt and 'the command was stopped' in prompt, prompt
            assert 'grep -q good b.txt' in prompt and 'test -f a.txt' not in prompt.split('What failed:')[1], prompt
            os.remove(f'{folder}/slow.flag')          # first repair: the hang
        elif node_id == 'acceptance-fix2':
            assert 'EXIT CODE: 1' in prompt, prompt
            open(f'{folder}/b.txt', 'w').write('good')   # second repair: the content
        elif node_id == 'review':
            assert sh('git', 'rev-parse', 'HEAD').strip() == base
            answer = 'VERDICT: PASS'
        record = {'worker': node_id, 'node': node_id, 'category': category, 'model': 'stub', 'reported_model': None,
                  'started': 0, 'seconds': 0, 'exit': 0, 'limit_waits': 0, 'ok': True, 'answer': answer}
        self.records.append({k: v for k, v in record.items() if k != 'answer'})
        return record


run = StubRun(f'{home}/plan.md', repo, 'stub-acc')
sg.CURRENT = run
try:
    verdict = run.execute()
finally:
    run.leave_as_uncommitted()
sg.write_result(run, verdict, run.status)
result = json.load(open(f'{run.out}/result.json'))
print('verdict:', verdict, '| nodes:', run.status)
print('sessions:', seen)
for entry in result['acceptance']:
    print('acceptance:', entry['node'], 'round', entry['round'], [(c['exit'], c['seconds']) for c in entry['commands']])
left = subprocess.run(['pgrep', '-f', 'sleep 30'], capture_output=True, text=True).stdout.split()
print('hung command left behind:', left or 'none')
assert verdict == 'verified', verdict
assert seen == ['a', 'b', 'acceptance-fix1', 'acceptance-fix2', 'review'] or seen == ['b', 'a', 'acceptance-fix1', 'acceptance-fix2', 'review'], seen
rounds = [(e['node'], e['round'], [c['exit'] for c in e['commands']]) for e in result['acceptance']]
assert rounds == [('acceptance', 0, [0, 124]), ('acceptance', 1, [0, 1]), ('acceptance', 2, [0, 0]), ('acceptance-final', 0, [0, 0])], rounds
assert not left and sh('git', 'rev-parse', 'HEAD').strip() == base
print('ALL CHECKS PASSED')
shutil.rmtree(home)
