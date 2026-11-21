"""Checks the SG_HUMAN protocol of script_graph.py with no model (added 2026-11-17): the Hermes process is replaced by a
function, and a thread plays the human by writing human/<node>-<k>.answer.txt.

    python3 human_stub_test.py <script_graph.py> <backup copy of the script before the change, as a .py file>

Expected, in order:
  1. a node asks (twice) and gets answers, continues with a new worker that is given request and answer, and is
     accepted; the second request is numbered -2; its dependent waited; an independent node ran meanwhile, with only
     ONE worker slot (SG_MAX_PARALLEL=1: a node that waits gives its slot back); a `role: human` gate waits and is
     accepted by an answer, which is kept as its handoff; the node after the gate runs after it.
  2. an answer starting with the line ABORT ends the run as stopped_by_human; nothing starts afterwards.
  3. SG_HUMAN_WAIT_SECONDS: an open request with no answer ends the run as no_answer_from_human.
  4. a reviewer may ask instead of giving a verdict; that is not an unreadable verdict.
  5. the same inside node worktrees: the build node's worker asks, and its work is still integrated.
  6. without SG_HUMAN a worker answer with a NEEDS HUMAN line changes nothing: node accepted, no human folder, no
     `human` key in result.json, prompts byte-for-byte those of the backup script; with it, a prompt is the backup's
     plus the one paragraph.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

SCRIPT, BACKUP = sys.argv[1], sys.argv[2]
HOME = tempfile.mkdtemp(prefix='sg-human-')
COUNTER = [0]


def load(path, **env):
    """The script as a fresh module with the given environment."""
    for key in ('SG_HUMAN', 'SG_HUMAN_WAIT_SECONDS', 'SG_NODE_WORKTREES', 'SG_MAX_PARALLEL', 'SG_NODE_SCRATCH'):
        os.environ.pop(key, None)
    os.environ.update(env)
    COUNTER[0] += 1
    spec = importlib.util.spec_from_file_location(f'sg{COUNTER[0]}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.START_GAP_SECONDS = 0
    module.HUMAN_POLL_SECONDS = 0.05
    COUNTER[0] += 1
    module.PILOT = f'{HOME}/pilot{COUNTER[0]}'
    module.RUNS = f'{module.PILOT}/runs'
    os.makedirs(f'{module.PILOT}/runs')
    return module


def make_repo():
    COUNTER[0] += 1
    repo = f'{HOME}/repo{COUNTER[0]}'
    os.makedirs(repo)
    subprocess.run(['git', 'init', '-q', '-b', 'work'], cwd=repo, check=True)
    open(f'{repo}/base.txt', 'w').write('base\n')
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'base'], cwd=repo, check=True)
    return repo


def node(node_id, role='build', deps='none', task=None):
    category = 'review' if role == 'review' else 'deep'
    return (f'### {node_id}\n- label: {node_id}\n- category: {category}\n- role: {role}\n- depends_on: {deps}\n'
            f'- write_scope: x\n- TASK: {task or "do " + node_id}\n- DELIVERABLE: d\n- SCOPE: s\n- VERIFY: v\n'
            f'- STOP WHEN: w\n')


def write_plan(*nodes):
    COUNTER[0] += 1
    path = f'{HOME}/plan{COUNTER[0]}.md'
    open(path, 'w').write('# Plan\n\nRepository: `{{REPO}}`.\n\nAll nodes work in the same folder.\n\n## Nodes\n\n'
                          + '\n'.join(nodes) + '\n## Task statement\n\nnothing\n')
    return path


def stub_class(sg, behave, calls):
    class StubRun(sg.Run):
        def worker(self, node_id, category, prompt, cwd=None):
            calls.append({'node': node_id, 'prompt': prompt, 'cwd': cwd, 'at': time.time()})
            mine = calls[-1]
            answer = behave(node_id, prompt, cwd, calls) or 'done'
            record = {'worker': f'{len(calls):02d}-{node_id}', 'node': node_id, 'category': category, 'model': 'stub',
                      'reported_model': None, 'started': 0, 'seconds': 0, 'exit': 0, 'limit_waits': 0, 'ok': True,
                      'answer': answer}
            self.records.append({k: v for k, v in record.items() if k != 'answer'})
            mine['ended'] = time.time()
            return record
    return StubRun


def play(sg, plan, repo, behave, answers, name='stub'):
    """Runs the plan. `answers(stem, request, calls)` is the human: the text to answer with, or None for 'not yet'."""
    calls, written = [], []
    run = stub_class(sg, behave, calls)(plan, repo, name)
    sg.CURRENT = run
    stop = threading.Event()

    def human():
        folder = f'{run.out}/human'
        while not stop.is_set():
            for entry in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                stem = entry[:-5]
                if not entry.endswith('.json') or os.path.exists(f'{folder}/{stem}.answer.txt'):
                    continue
                try:
                    info = json.load(open(f'{folder}/{entry}'))
                except ValueError:
                    continue
                if info['state'] != 'open':
                    continue
                text = answers(stem, info, calls)
                if text is not None:
                    open(f'{folder}/{stem}.tmp', 'w').write(text)
                    os.replace(f'{folder}/{stem}.tmp', f'{folder}/{stem}.answer.txt')
                    written.append(stem)
            time.sleep(0.03)
    thread = threading.Thread(target=human, daemon=True)
    thread.start()
    try:
        verdict = run.execute()
    finally:
        stop.set()
        thread.join()
    sg.write_result(run, verdict, run.status)
    return run, verdict, calls, written, json.load(open(f'{run.out}/result.json'))


def seen(calls, node_id):
    return [c for c in calls if c['node'] == node_id]


# ---------------------------------------------------------------- 1. ask, answer, continue; second ask; gate; slots
sg = load(SCRIPT, SG_HUMAN='1', SG_MAX_PARALLEL='1')
repo = make_repo()
plan = write_plan(node('a'), node('b', deps='a'), node('c'),
                  node('gate', role='human', deps='b', task='Run `npm run deploy` in the repository and confirm'),
                  node('d', deps='gate'))


def behave1(node_id, prompt, cwd, calls):
    if node_id == 'a':
        n = len(seen(calls, 'a'))
        if n == 1:
            return 'Started.\nNEEDS HUMAN: the registry login expired; please log in and say when done.'
        if n == 2:
            return 'Logged in, thanks.\nNEEDS HUMAN: which region should I publish to?'


def answers1(stem, info, calls):
    if stem == 'a-1':
        # the independent node c must have run to its end while a waits, though there is only one worker slot
        if not any(c['node'] == 'c' and 'ended' in c for c in calls):
            return None
        return 'Done: logged in.\nUse the work account.'
    if stem == 'a-2':
        return 'eu-north-1'
    if stem == 'gate-1':
        assert seen(calls, 'b') and not seen(calls, 'd'), [c['node'] for c in calls]   # the gate waits for b, d for it
        return 'deployed, url is https://example.test/x'


run, verdict, calls1, written, result = play(sg, plan, repo, behave1, answers1, 'main')
order = [c['node'] for c in calls1]
print('1. verdict:', verdict, '| worker order:', order, '| answers written:', written)
assert verdict == 'verified', (verdict, run.status)
assert all(value == 'accepted' for value in run.status.values()), run.status
a_calls = seen(calls1, 'a')
assert len(a_calls) == 3, order
assert seen(calls1, 'b')[0]['at'] >= a_calls[-1]['ended'], 'b must wait for a'
assert seen(calls1, 'c')[0]['ended'] <= a_calls[1]['at'], 'c ran while a waited'
assert order.index('c') < order.index('b') and order.index('d') > order.index('b')
base_prompt = a_calls[0]['prompt']
assert 'NEEDS HUMAN: <what and why>' in base_prompt
second = a_calls[1]['prompt']
head, _, tail = base_prompt.partition('\n\n')   # step 5: the rulings of the run go after the first paragraph
assert second.startswith(head) and tail in second, 'the usual prompt, with the rulings section after its first paragraph'
assert 'the registry login expired; please log in and say when done.' in second
assert "in the human's own words" in second and '<<<HUMAN\nDone: logged in.\nUse the work account.\nHUMAN>>>' in second
third = a_calls[2]['prompt']
assert tail in third and '<<<HUMAN\nDone: logged in.\nUse the work account.\nHUMAN>>>' in third and 'which region should I publish to?' in third and 'eu-north-1' in third
one = json.load(open(f'{run.out}/human/a-1.json'))
two = json.load(open(f'{run.out}/human/a-2.json'))
assert one['node'] == 'a' and one['worker'] == '01-a' and one['state'] == 'answered' and one['answered_at'], one
assert one['request'].startswith('the registry login expired') and one['answer_tail'].endswith('say when done.'), one
assert two['request'] == 'which region should I publish to?' and not os.path.exists(f'{run.out}/human/a-3.json')
gate = json.load(open(f'{run.out}/human/gate-1.json'))
assert gate['request'] == 'Run `npm run deploy` in the repository and confirm' and gate['worker'] is None, gate
assert gate['state'] == 'answered' and not seen(calls1, 'gate'), 'a gate runs no model'
assert run.handoffs == {'gate': 'deployed, url is https://example.test/x'}, run.handoffs
assert sorted((h['node'], h['k'], h['state']) for h in result['human']) == \
    [('a', 1, 'answered'), ('a', 2, 'answered'), ('gate', 1, 'answered')], result['human']

# ---------------------------------------------------------------- 2. ABORT
sg = load(SCRIPT, SG_HUMAN='1')
repo = make_repo()
plan = write_plan(node('x'), node('y', deps='x'), node('z'))
run, verdict, calls, written, result = play(
    sg, plan, repo, lambda n, p, c, calls: 'NEEDS HUMAN: delete the production table?' if n == 'x' else None,
    lambda stem, info, calls: 'ABORT\nnot today' if stem == 'x-1' else None, 'abort')
print('2. verdict:', verdict, '| nodes:', run.status)
assert verdict == 'stopped_by_human' and result['verdict'] == 'stopped_by_human', verdict
assert run.status['y'] == 'waiting' and not seen(calls, 'y'), run.status
assert run.status['x'] == 'failed', run.status
assert json.load(open(f'{run.out}/human/x-1.json'))['state'] == 'answered'
assert len(seen(calls, 'x')) == 1

# ---------------------------------------------------------------- 3. the time limit
sg = load(SCRIPT, SG_HUMAN='1')
sg.HUMAN_WAIT_SECONDS = 0.4
repo = make_repo()
plan = write_plan(node('x'), node('y', deps='x'))
began = time.time()
run, verdict, calls, written, result = play(
    sg, plan, repo, lambda n, p, c, calls: 'NEEDS HUMAN: which one?' if n == 'x' else None, lambda *a: None, 'limit')
print('3. verdict:', verdict, f'after {time.time() - began:.1f}s')
assert verdict == 'no_answer_from_human' and not seen(calls, 'y'), (verdict, run.status)
assert json.load(open(f'{run.out}/human/x-1.json'))['state'] == 'no answer'
assert time.time() - began < 10

# ---------------------------------------------------------------- 4. a reviewer may ask
sg = load(SCRIPT, SG_HUMAN='1')
repo = make_repo()
plan = write_plan(node('b1'), node('rev', role='review', deps='b1'))


def behave4(node_id, prompt, cwd, calls):
    if node_id == 'rev':
        return 'VERDICT: PASS' if 'HUMAN>>>' in prompt else 'Unsure about the rename.\nNEEDS HUMAN: is the rename of load() intended?'


run, verdict, calls, written, result = play(sg, plan, repo, behave4, lambda s, i, c: 'yes, intended' if s == 'rev-1' else None, 'review')
print('4. verdict:', verdict, '| review verdicts:', run.verdicts, '| reviewer sessions:', len(seen(calls, 'rev')))
assert verdict == 'verified' and run.verdicts == ['PASS'], (verdict, run.verdicts)
assert len(seen(calls, 'rev')) == 2

# ---------------------------------------------------------------- 5. inside node worktrees
sg = load(SCRIPT, SG_HUMAN='1', SG_NODE_WORKTREES='1', SG_MAX_PARALLEL='2')
repo = make_repo()
plan = write_plan(node('wa'), node('wb'))


def behave5(node_id, prompt, cwd, calls):
    if node_id == 'wa' and 'HUMAN>>>' not in prompt:
        open(f'{cwd}/wa.txt', 'w').write('half')
        return 'NEEDS HUMAN: which name for the file?'
    if node_id == 'wa':
        open(f'{cwd}/wa.txt', 'w').write('whole')
    if node_id == 'wb':
        open(f'{cwd}/wb.txt', 'w').write('b')


run, verdict, calls, written, result = play(sg, plan, repo, behave5, lambda s, i, c: 'wa.txt' if s == 'wa-1' else None, 'wt')
print('5. verdict:', verdict, '| sessions:', [c['node'] for c in calls])
assert verdict == 'verified', (verdict, run.status)
assert open(f'{repo}/wa.txt').read() == 'whole' and open(f'{repo}/wb.txt').read() == 'b'
assert seen(calls, 'wa')[1]['cwd'] == seen(calls, 'wa')[0]['cwd'], 'the same own copy continues'

# ---------------------------------------------------------------- 6. without SG_HUMAN nothing changes
plan = write_plan(node('p'), node('q', deps='p'), node('rev', role='review', deps='q'))
prompts = {}
for tag, mod in (('old', load(BACKUP)), ('off', load(SCRIPT))):
    repo = make_repo()
    got = []
    # every worker answers with a NEEDS HUMAN line, and the reviewer also with a verdict
    stub = stub_class(mod, lambda n, p, c, calls: ('VERDICT: PASS\n' if n == 'rev' else '') + 'NEEDS HUMAN: please decide', got)
    run = stub(plan, repo, 'off')
    mod.CURRENT = run
    verdict = run.execute()
    mod.write_result(run, verdict, run.status)
    assert verdict == 'verified', (tag, verdict, run.status)
    assert len(got) == 3 and not os.path.exists(f'{run.out}/human'), (tag, [c['node'] for c in got])
    assert 'human' not in json.load(open(f'{run.out}/result.json')), tag
    assert all(value == 'accepted' for value in run.status.values())
    prompts[tag] = {c['node']: c['prompt'].replace(mod.PILOT, '<PILOT>').replace(repo, '<REPO>') for c in got}
assert prompts['off'] == prompts['old'] and len(prompts['old']) == 3, 'prompts differ from the backup script'
on = load(SCRIPT, SG_HUMAN='1')
repo = make_repo()
run = on.Run(plan, repo, 'off')
paragraph = on.HUMAN_PARAGRAPH
assert 'NEEDS HUMAN: <what and why>' in paragraph and 'do not work around it and do not guess' in paragraph
for item in run.nodes:
    text = run.prompt(item).replace(on.PILOT, '<PILOT>').replace(repo, '<REPO>')
    assert text == prompts['old'][item['id']] + '\n' + paragraph + '\n', item['id']
print('6. without the switch: accepted, no human folder, prompts equal the backup script\'s; with it: only the paragraph is added')
# ---------------------------------------------------------------- 7. SG_HUMAN_OPEN (nothing real is opened or notified)
sg = load(SCRIPT, SG_HUMAN='1', SG_HUMAN_OPEN='1', SG_VIEW_PORT='5499')
repo = make_repo()
plan = write_plan(node('o1'), node('og', role='human', deps='o1', task='confirm'))
opened = []
calls7 = []
cls = stub_class(sg, lambda n, p, c, calls: 'NEEDS HUMAN: pick one' if n == 'o1' and 'HUMAN>>>' not in p else None, calls7)
cls.open_page = lambda self, node_id, request: opened.append((node_id, request))   # replaced: no browser
run = cls(plan, repo, 'openrun')
sg.CURRENT = run
stop7 = threading.Event()


def human7():
    folder = f'{run.out}/human'
    while not stop7.is_set():
        for entry in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if entry.endswith('.json') and not os.path.exists(f'{folder}/{entry[:-5]}.answer.txt'):
                open(f'{folder}/{entry[:-5]}.answer.txt', 'w').write('ok')
        time.sleep(0.03)


t7 = threading.Thread(target=human7, daemon=True)
t7.start()
verdict = run.execute()
stop7.set()
print('7. SG_HUMAN_OPEN: verdict', verdict, '| page opened for:', opened)
assert verdict == 'verified' and opened == [('o1', 'pick one'), ('og', 'confirm')], opened
# the real open_page: nothing listens on the port -> silent, no command started; a listener -> `open <url>`
ran = []
plain = sg.Run(plan, repo, 'openrun2')
sg.subprocess.run = lambda argv, **kw: ran.append(argv)
sg.shutil.which = lambda name: None
plain.open_page('o1', 'pick one')
assert ran == [], ran
import socket
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(8)
sg.VIEW_PORT = listener.getsockname()[1]
plain.open_page('o1', 'pick "one"; touch x')
assert ran == [['open', f'http://127.0.0.1:{sg.VIEW_PORT}/?run=openrun2']], ran
ran.clear()
sg.shutil.which = lambda name: '/usr/local/bin/terminal-notifier'
plain.open_page('o1', 'pick "one"; touch x')
assert ran[0][0] == '/usr/local/bin/terminal-notifier' and ran[0][-2:] == ['-open', f'http://127.0.0.1:{sg.VIEW_PORT}/?run=openrun2'] \
    and ran[0][ran[0].index('-title') + 1] == 'openrun2: o1 needs you' and 'pick "one"; touch x Answer at http://' in ran[0][ran[0].index('-message') + 1], ran
listener.close()
print('7. open_page: silent with no listener; `open <url>` with one; terminal-notifier -open when installed')
print('ALL CHECKS PASSED')
shutil.rmtree(HOME)
