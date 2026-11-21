"""Checks the worktree mechanics of script_graph.py with no model: the worker is replaced by a function that edits files.

Plan: baseline, then `a` and `b` side by side (both rewrite the same line of shared.txt, so the second to be
integrated conflicts), `c` beside them (own file, no conflict, but with `b` it breaks the integration check), then
review and verify in the repository folder. Expected: every node accepted; one conflict, resolved by the node that
was rebased; one failed check, repaired by the rebased node; the repository ends with the work as an uncommitted
change on the starting commit; no worktree left.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

os.environ['SG_NODE_WORKTREES'] = '1'
os.environ['SG_MAX_PARALLEL'] = '3'
os.environ['SG_INTEGRATION_CHECK'] = 'sh check.sh'
spec = importlib.util.spec_from_file_location('sg', sys.argv[1])
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)

home = tempfile.mkdtemp(prefix='sg-stub-')
sg.PILOT = f'{home}/pilot'
sg.RUNS = f'{sg.PILOT}/runs'
sg.START_GAP_SECONDS = 0
os.makedirs(f'{sg.PILOT}/runs')
repo = f'{home}/repo'
os.makedirs(repo)


def sh(*args: str, cwd: str = repo) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


sh('git', 'init', '-q', '-b', 'work')
open(f'{repo}/shared.txt', 'w').write('line one\nvalue = 0\nline three\n')
# The check fails when both b.txt and c.txt exist and c.txt has not been "fitted".
open(f'{repo}/check.sh', 'w').write('if [ -f b.txt ] && [ -f c.txt ] && ! grep -q fitted c.txt; then echo "b and c do not fit"; exit 1; fi\n')
open(f'{repo}/.gitignore', 'w').write('node_modules/\n')
os.makedirs(f'{repo}/node_modules')
open(f'{repo}/node_modules/marker', 'w').write('x')
sh('git', 'add', '-A')
sh('git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'base')
base = sh('git', 'rev-parse', 'HEAD').strip()


def node(node_id: str, role: str, deps: str) -> str:
    return (f'### {node_id}\n- label: {node_id}\n- category: {"review" if role == "review" else "deep"}\n- role: {role}\n'
            f'- depends_on: {deps}\n- write_scope: x\n- TASK: do {node_id} in {{{{REPO}}}}\n- DELIVERABLE: d\n- SCOPE: s\n'
            f'- VERIFY: v\n- STOP WHEN: w\n')


plan = ('# Plan\n\nRepository: `{{REPO}}`, branch `{{BRANCH}}`.\n\nAll nodes work in the same folder. Up to five.\n\n## Nodes\n\n'
        + '\n'.join([node('baseline', 'baseline', 'none'), node('a', 'build', 'baseline'), node('b', 'build', 'baseline'),
                     node('c', 'build', 'baseline'), node('review', 'review', 'a, b, c'), node('verify', 'verify', 'review')])
        + '\n## Task statement\n\nnothing\n')
open(f'{home}/plan.md', 'w').write(plan)

seen: list[tuple[str, str]] = []
order = {'a': 0.2, 'c': 0.6, 'b': 1.0}   # a is integrated first, then c, then b


class StubRun(sg.Run):
    def worker(self, node_id, category, prompt, cwd=None):
        import time
        folder = cwd or self.repo
        seen.append((node_id, folder))
        plan_file = re.search(r'read (\S+) in full', prompt).group(1)
        assert os.path.exists(plan_file), plan_file
        plan_read = open(plan_file).read()
        answer = 'done'
        if node_id in ('a', 'b'):
            assert folder != self.repo and folder in plan_read and 'own copy' in plan_read, (node_id, folder)
            assert os.path.exists(f'{folder}/node_modules/marker')
            assert f'do {node_id} in {folder}' in prompt, prompt
            text = open(f'{folder}/shared.txt').read()
            assert 'value = 0' in text, f'{node_id} must start from the base, got {text!r}'
            open(f'{folder}/shared.txt', 'w').write(text.replace('value = 0', f'value = {node_id}'))
            open(f'{folder}/{node_id}.txt', 'w').write(node_id)
            time.sleep(order[node_id])
        elif node_id == 'c':
            assert not os.path.exists(f'{folder}/a.txt'), 'c must not see a neighbour in mid-work'
            open(f'{folder}/c.txt', 'w').write('c')
            time.sleep(order[node_id])
        elif node_id.startswith('b-rebase'):
            text = open(f'{folder}/shared.txt').read()
            if '<<<<<<<' in text:
                assert 'value = a' in text and 'value = b' in text and 'conflict markers' in prompt, text
                assert text.index('value = a') < text.index('=======') < text.index('value = b'), 'accepted work is above'
                open(f'{folder}/shared.txt', 'w').write('line one\nvalue = a+b\nline three\n')
            else:
                assert 'do not fit' in prompt, prompt
                open(f'{folder}/b.txt', 'w').write('b, reworked')
                open(f'{folder}/c.txt', 'a').write(' fitted')   # stands for a repair that makes the two fit
        elif node_id == 'review':
            assert folder == self.repo
            status = sh('git', 'status', '--porcelain')
            assert sh('git', 'rev-parse', 'HEAD').strip() == base, 'review must see the work as uncommitted on the base'
            for name in ('a.txt', 'b.txt', 'c.txt', 'shared.txt'):
                assert name in status, (name, status)
            answer = 'VERDICT: PASS'
        elif node_id == 'verify':
            assert 'gathered in this folder' in plan_read
        record = {'worker': node_id, 'node': node_id, 'category': category, 'model': 'stub', 'reported_model': None,
                  'started': 0, 'seconds': 0, 'exit': 0, 'limit_waits': 0, 'ok': True, 'answer': answer}
        self.records.append({k: v for k, v in record.items() if k != 'answer'})
        return record


run = StubRun(f'{home}/plan.md', repo, 'stub-1')
sg.CURRENT = run
try:
    verdict = run.execute()
finally:
    run.leave_as_uncommitted()
sg.write_result(run, verdict, run.status)

print('verdict:', verdict)
print('nodes:', run.status)
for fact in run.copies:
    print('copy:', fact)
print('sessions:', [name for name, _ in seen])
print('shared.txt:', open(f'{repo}/shared.txt').read().splitlines())
print('HEAD is base:', sh('git', 'rev-parse', 'HEAD').strip() == base)
print('status:', sh('git', 'status', '--porcelain').split())
print('worktrees:', len(sh('git', 'worktree', 'list').strip().splitlines()), '| wt folders left:', os.listdir(f'{run.out}/wt'))
facts = {fact['node']: fact for fact in run.copies}
assert verdict == 'verified', verdict
assert facts['a'] == dict(facts['a'], base_moved=False, conflict_files=0, repair_sessions=0, integrated=True)
assert facts['c'] == dict(facts['c'], base_moved=True, conflict_files=0, check_failures=0, integrated=True)
assert facts['b'] == dict(facts['b'], base_moved=True, conflict_files=1, check_failures=1, repair_sessions=2, integrated=True)
assert open(f'{repo}/shared.txt').read() == 'line one\nvalue = a+b\nline three\n'
assert 'fitted' in open(f'{repo}/c.txt').read()
assert sh('git', 'rev-parse', 'HEAD').strip() == base and os.listdir(f'{run.out}/wt') == []
print('ALL CHECKS PASSED')
shutil.rmtree(home)
