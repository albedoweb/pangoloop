"""Checks the 'ui' route and the SG_NODE_SCRATCH switch of script_graph.py with no model (added 2026-11-17).

    python3 ui_stub_test.py <script_graph.py> <backup copy of the script before the change, as a .py file>

Plan: nodes `ui1` (category ui), `deep1` (category deep), `rev` (category review), run in one folder. The Hermes
process is replaced by a function that records the command line. Expected: ui is started on ui-model-stub /
stub-provider (the test routes file); the other categories on their old routes, with the same command line as the backup script; without
SG_NODE_SCRATCH the prompts equal what the backup script writes (paths of the two runs normalised); with it each
prompt names an own existing empty folder, differs from the old prompt by that one sentence only, and a second
prompt of the same node gets a fresh folder with a suffix.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile


def load(path: str, scratch: bool, tag: str):
    if scratch:
        os.environ['SG_NODE_SCRATCH'] = '1'
    else:
        os.environ.pop('SG_NODE_SCRATCH', None)
    spec = importlib.util.spec_from_file_location(tag, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


home = tempfile.mkdtemp(prefix='sg-ui-')
repo = f'{home}/repo'
os.makedirs(repo)
subprocess.run(['git', 'init', '-q', '-b', 'work'], cwd=repo, check=True)
subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base'],
               cwd=repo, check=True)


def node(node_id: str, category: str, role: str, deps: str) -> str:
    return (f'### {node_id}\n- label: {node_id}\n- category: {category}\n- role: {role}\n- depends_on: {deps}\n'
            f'- write_scope: x\n- TASK: do {node_id} in {{{{REPO}}}}\n- DELIVERABLE: d\n- SCOPE: s\n- VERIFY: v\n'
            f'- STOP WHEN: w\n')


plan = ('# Plan\n\nRepository: `{{REPO}}`.\n\nAll nodes work in the same folder.\n\n## Nodes\n\n'
        + '\n'.join([node('ui1', 'ui', 'build', 'none'), node('deep1', 'deep', 'build', 'ui1'),
                     node('rev', 'review', 'review', 'deep1')]) + '\n## Task statement\n\nnothing\n')
open(f'{home}/plan.md', 'w').write(plan)


def play(path: str, scratch: bool, tag: str):
    sg = load(path, scratch, tag)
    sg.PILOT = f'{home}/{tag}/pilot'
    sg.RUNS = f'{sg.PILOT}/runs'
    sg.START_GAP_SECONDS = 0
    os.makedirs(f'{sg.PILOT}/runs')
    commands = []

    class StubRun(sg.Run):
        def spawn(self, command, events, errors, cwd=None):   # no Hermes: the command line is only recorded
            commands.append(list(command))
            events.write('{"type": "result", "text": "VERDICT: PASS", "exit_code": 0}\n')
            events.flush()
            return subprocess.Popen(['true'])

    run = StubRun(f'{home}/plan.md', repo, 'ui-run')
    verdict = run.execute()
    prompts = {}
    for name in sorted(os.listdir(f'{run.out}/workers')):
        prompts[name] = open(f'{run.out}/workers/{name}/prompt.txt').read()
    return sg, run, verdict, commands, prompts


def route(command):
    return command[command.index('-m') + 1], command[command.index('--provider') + 1]


def norm_text(text, sg):
    return text.replace(sg.PILOT, '<PILOT>')


def norm_cmd(command, sg):
    return [re.sub(r'-[0-9a-f]{6,}-', '-X-', norm_text(part, sg)) for part in command]


old, old_run, old_verdict, old_commands, old_prompts = play(sys.argv[2], False, 'old')
new, new_run, new_verdict, new_commands, new_prompts = play(sys.argv[1], False, 'off')
assert old_verdict == new_verdict == 'verified', (old_verdict, new_verdict)
# 1. routes: ui on its own route; the others exactly as before
routes = {c[c.index('-c') + 1].split('-')[-1]: route(c) for c in new_commands}
assert routes == {'ui1': ('ui-model-stub', 'stub-provider'), 'deep1': ('coder-model', 'stub-provider'),
                  'rev': ('reviewer-model', 'stub-provider')}, routes   # the routes of tests/fixtures/routes-hermes.toml
assert route(old_commands[0]) == ('coder-model', 'stub-provider')   # the old script knew no 'ui'
old_by = {c[c.index('-c') + 1].split('-')[-1]: norm_cmd(c, old) for c in old_commands}
new_by = {c[c.index('-c') + 1].split('-')[-1]: norm_cmd(c, new) for c in new_commands}
assert old_by['deep1'] == new_by['deep1'] and old_by['rev'] == new_by['rev'], (old_by, new_by)
# the ui node differs from the old command only in the model and the tool sets
diff = [(a, b) for a, b in zip(old_by['ui1'], new_by['ui1']) if a != b]
assert diff == [('coder-model', 'ui-model-stub'),   # (the provider is the same for every category)
                ('file,terminal,omh-lsp', 'file,terminal,omh-lsp,vision')], diff
tools = {c[c.index('-c') + 1].split('-')[-1]: c[c.index('-t') + 1] for c in new_commands}
assert tools == {'ui1': 'file,terminal,omh-lsp,vision', 'deep1': 'file,terminal,omh-lsp',
                 'rev': 'file,terminal,omh-lsp'}, tools
# 2. switch off: the prompts are the backup's, byte for byte, and no scratch folder exists
old_norm = {k: norm_text(v, old) for k, v in old_prompts.items()}
new_norm = {k: norm_text(v, new) for k, v in new_prompts.items()}
assert old_norm == new_norm and len(new_norm) == 3, (old_norm, new_norm)
assert not any('scratch' in text for text in new_prompts.values())
assert not os.path.exists(f'{new_run.out}/scratch')
# 3. switch on
sg, run, verdict, commands, prompts = play(sys.argv[1], True, 'on')
assert verdict == 'verified'
sentence = re.compile(r'Your scratch folder is (\S+), empty and yours alone; do not read or write any other folder '
                      r'outside the repository\.\n')
folders = []
for name, text in prompts.items():
    node_id = name.split('-', 1)[1]
    found = sentence.search(text)
    assert found, text
    folder = found.group(1)
    assert os.path.isabs(folder) and folder.startswith(f'{run.out}/scratch/') and os.path.basename(folder) == node_id, folder
    assert os.path.isdir(folder) and os.listdir(folder) == [], folder
    assert norm_text(sentence.sub('', text), sg) == old_norm[name], 'only the one sentence is added'
    folders.append(folder)
assert len(set(folders)) == 3
# a second try of the same node: a fresh folder with a suffix, the first one stays
open(f'{folders[0]}/left', 'w').write('x')
again = sentence.search(run.prompt(run.nodes[0])).group(1)
assert again == folders[0] + '-2' and os.listdir(again) == [] and os.path.exists(f'{folders[0]}/left'), again
print('routes:', routes)
print('scratch folders:', [os.path.relpath(f, run.out) for f in folders], 'retry:', os.path.relpath(again, run.out))
print('ALL CHECKS PASSED')
shutil.rmtree(home)
