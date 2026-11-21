"""The Hermes runner and its place in the engine: the command line is the one the script built, the events are read as
the engine read them, the engine takes its runner from PANGOLOOP_RUNNER, and a stub that intercepts `spawn` still sees
the command."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

import support
from pangoloop.runners import RunResult, get_runner
from pangoloop.runners.hermes import HermesRunner


def load_engine(**env):
    for key in [k for k in os.environ if k.startswith('SG_')] + ['PANGOLOOP_RUNNER', 'PANGOLOOP_RUNS']:
        os.environ.pop(key, None)
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location('graph_under_test', os.path.join(support.ROOT, 'pangoloop', 'graph.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Command(unittest.TestCase):
    def test_command_line_is_the_scripts(self):
        command = HermesRunner(profile='lab').command(session='sg-run-1a-01-a', model='cheap-model', provider='cheap-provider',
                                         prompt_file='/p/prompt.txt', toolsets='file,terminal,omh-lsp', max_turns='200')
        self.assertEqual(command, ['hermes', '-p', 'lab', 'chat', '--query-file', '/p/prompt.txt', '--oneshot',
                                   '--format', 'stream-json', '-m', 'cheap-model', '--provider', 'cheap-provider',
                                   '-t', 'file,terminal,omh-lsp', '--max-turns', '200', '-c', 'sg-run-1a-01-a',
                                   '--create-if-missing'])

    def test_registry(self):
        self.assertIs(get_runner('hermes'), get_runner('hermes'))
        with self.assertRaises(SystemExit):
            get_runner('nonesuch')
        with self.assertRaises(SystemExit):
            get_runner('../x')


class ReadEvents(unittest.TestCase):
    def read(self, lines, code=0):
        with tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False) as handle:
            handle.write('\n'.join(lines) + '\n')
        self.addCleanup(os.unlink, handle.name)
        return HermesRunner().read_events(handle.name, code)

    def test_answer_model_and_bad_lines(self):
        result = self.read(['not json', json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm1'}),
                            json.dumps({'type': 'text'}),
                            json.dumps({'type': 'result', 'text': 'first', 'exit_code': 0}),
                            json.dumps({'type': 'result', 'text': 'last', 'exit_code': None})])
        self.assertEqual(result, RunResult(0, 'last', 'm1'))

    def test_failed_result_turns_zero_into_one_but_keeps_other_codes(self):
        self.assertEqual(self.read([json.dumps({'type': 'result', 'text': 'x', 'exit_code': 2})], 0).exit_code, 1)
        self.assertEqual(self.read([json.dumps({'type': 'result', 'text': 'x', 'exit_code': 2})], 124).exit_code, 124)

    def test_no_result_is_an_empty_answer(self):
        self.assertEqual(self.read([json.dumps({'type': 'system', 'subtype': 'init'})]), RunResult(0, '', None))


class InTheEngine(unittest.TestCase):
    def test_default_runner_is_hermes_and_env_selects_another(self):
        self.assertEqual(load_engine().RUNNER.name, 'hermes')
        with self.assertRaises(SystemExit):
            load_engine(PANGOLOOP_RUNNER='nonesuch')
        load_engine()   # leave the environment clean for the next test

    def test_stub_intercepting_spawn_sees_the_runners_command(self):
        sg = load_engine()
        home = support.temp_dir(self)
        sg.PILOT = f'{home}/pilot'
        sg.RUNS = f'{sg.PILOT}/runs'
        sg.START_GAP_SECONDS = 0
        os.makedirs(f'{sg.PILOT}/runs')
        repo = f'{home}/repo'
        os.makedirs(repo)
        subprocess.run(['git', 'init', '-q', '-b', 'work'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'b'],
                       cwd=repo, check=True)
        with open(f'{home}/plan.md', 'w') as handle:
            handle.write('# Plan\n\nAll nodes work in the same folder.\n\n## Nodes\n\n### a\n- label: A\n- category: ui\n'
                         '- role: build\n- depends_on: none\n- write_scope: x\n- TASK: t\n- DELIVERABLE: d\n- SCOPE: s\n'
                         '- VERIFY: v\n- STOP WHEN: w\n\n## Task statement\n\nnothing\n')
        seen = []

        class StubRun(sg.Run):
            def spawn(self, command, events, errors, cwd=None):
                seen.append(list(command))
                events.write(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'reported'}) + '\n')
                events.write(json.dumps({'type': 'result', 'text': 'done', 'exit_code': 0}) + '\n')
                events.flush()
                return subprocess.Popen(['true'])

        run = StubRun(f'{home}/plan.md', repo, 'r1')
        self.assertEqual(run.execute(), 'verified')
        self.assertEqual(len(seen), 1)
        command = seen[0]
        self.assertEqual(command[:3], ['hermes', 'chat', '--query-file'])
        self.assertEqual((command[command.index('-m') + 1], command[command.index('--provider') + 1],
                          command[command.index('-t') + 1], command[command.index('--max-turns') + 1]),
                         ('ui-model-stub', 'stub-provider', 'file,terminal,omh-lsp,vision', '200'))
        self.assertTrue(command[command.index('-c') + 1].startswith('sg-r1-'))
        self.assertEqual(run.records[0]['reported_model'], 'reported')
        self.assertEqual(run.records[0]['exit'], 0)

    def test_runs_folder_from_pangoloop_runs(self):
        home = support.temp_dir(self)
        sg = load_engine(PANGOLOOP_RUNS=f'{home}/elsewhere')
        os.makedirs(f'{home}/elsewhere')
        repo = f'{home}/repo'
        os.makedirs(repo)
        subprocess.run(['git', 'init', '-q', '-b', 'work'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'b'],
                       cwd=repo, check=True)
        with open(f'{home}/plan.md', 'w') as handle:
            handle.write('# Plan\n\n## Nodes\n\n### a\n- label: A\n- category: deep\n- role: build\n- depends_on: none\n'
                         '- write_scope: x\n- TASK: t\n- DELIVERABLE: d\n- SCOPE: s\n- VERIFY: v\n- STOP WHEN: w\n\n'
                         '## Task statement\n\nnothing\n')
        run = sg.Run(f'{home}/plan.md', repo, 'r2')
        self.assertEqual(run.out, f'{home}/elsewhere/r2')
        self.assertTrue(os.path.isdir(f'{home}/elsewhere/r2/workers'))
        load_engine()


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
