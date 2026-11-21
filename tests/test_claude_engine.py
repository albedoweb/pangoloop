"""The engine and the viewer with the Claude Code runner, against a stub command that prints the RECORDED stream format
(tests/claude_stub.py): the engine is proven against both formats (the Hermes stub tests are in the other files)."""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.request

import helpers
import support

STUB = os.path.join(support.ROOT, 'tests', 'claude_stub.py')
DRIVER = os.path.join(support.ROOT, 'tests', 'driver_spawn.py')


class Engine(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.repo = helpers.make_repo(self.home)
        self.wrapper = os.path.join(self.home, 'claude')
        with open(self.wrapper, 'w') as handle:
            handle.write(f'#!/bin/sh\nexec {sys.executable} {STUB} "$@"\n')
        os.chmod(self.wrapper, 0o755)
        self.log = os.path.join(self.home, 'stub.log')

    def run_plan(self, *nodes, name='r1', **env):
        plan = helpers.write_plan(self.home, *nodes)
        environment = support.clean_environment(PANGOLOOP_RUNNER='claude-code', PANGOLOOP_CLAUDE_COMMAND=self.wrapper,
                                                STUB_LOG=self.log, PANGOLOOP_RUNS=f'{self.home}/runs', **env)
        return subprocess.run([sys.executable, DRIVER, plan, self.repo, name], capture_output=True, text=True,
                              env=environment, cwd=self.home, timeout=120)

    def calls(self):
        with open(self.log) as handle:
            return [json.loads(line) for line in handle]

    def test_the_whole_graph(self):
        done = self.run_plan(
            helpers.node_text('base', role='baseline'),
            helpers.node_text('make', role='build', deps='base'),
            helpers.script_node('check', 'run `true`', deps='make'),
            helpers.node_text('accept', role='acceptance', deps='check', VERIFY='`true`'),
            helpers.node_text('rev', role='review', category='review', deps='accept',
                              TASK='Review the change. End with VERDICT: PASS or VERDICT: CHANGES REQUESTED.'),
            SG_NODE_WORKTREES='1')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        run = f'{self.home}/runs/r1'
        result = helpers.read_json(f'{run}/result.json')
        self.assertEqual((result['verdict'], result['runner']), ('verified', 'claude-code'))
        with open(f'{run}/runner') as handle:
            self.assertEqual(handle.read(), 'claude-code\n')
        workers = [w for w in result['workers'] if w['model'] != 'script']
        self.assertEqual({w['node'] for w in workers}, {'base', 'make', 'rev'})
        for worker in workers:
            self.assertEqual((worker['runner'], worker['exit'], worker['ok']), ('claude-code', 0, True))
            self.assertEqual(worker['reported_model'], 'small-model-5-5')
            self.assertEqual(worker['usage']['turns'], 2)
            self.assertIn('cost_usd', worker['usage'])
        self.assertTrue(os.path.exists(f'{self.repo}/make.txt'), 'the build node worked in a copy that was integrated')
        # the routes come from the configuration file of the stand-in runs (tests/fixtures/routes-claude.toml)
        models = {w['node']: w['model'] for w in workers}
        self.assertEqual((models['rev'], models['base'], models['make']), ('reviewer-model', 'coder-model', 'coder-model'))
        for call in self.calls():
            argv = call['argv']
            self.assertEqual(argv[0], '-p')
            self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
            self.assertIn('--disallowedTools', argv)
            self.assertEqual(argv[argv.index('--add-dir') + 1], os.path.realpath(run))
            self.assertFalse(call['stdin_tty'])
        from pangoloop import view   # a script node's own stream is read with the script reader in a Claude Code run
        saved = view.RUNS
        view.RUNS = f'{self.home}/runs'
        self.addCleanup(lambda: setattr(view, 'RUNS', saved))
        shown = {w['node']: w for w in view.build_run('r1')['workers']}
        self.assertTrue(shown['check']['tool_calls'] >= 1 and shown['check']['model'] == 'script', shown['check'])
        self.assertEqual(shown['make']['tools'], {'Bash': 1})
        self.assertTrue(view.build_worker('r1', shown['check']['tag'])['tail'])
        copies = [c['cwd'] for c in self.calls() if '`make`' in c['argv'][1]]
        self.assertTrue(copies and '/wt/make' in copies[0], copies)

    def test_the_viewer_reads_the_recorded_format(self):
        self.assertEqual(self.run_plan(helpers.node_text('a')).returncode, 0)
        from pangoloop import view
        saved = view.RUNS
        view.RUNS = f'{self.home}/runs'
        self.addCleanup(lambda: setattr(view, 'RUNS', saved))
        data = view.build_run('r1')
        self.assertEqual(data['runner'], 'claude-code')
        worker = data['workers'][0]
        self.assertEqual((worker['tool_calls'], worker['tools'], worker['tool_errors']), (1, {'Bash': 1}, 0))
        self.assertEqual(worker['model'], 'small-model-5-5')
        self.assertEqual(worker['tokens'], {'input': 4, 'output': 166, 'cache_read': 16315, 'cache_write': 1104})
        self.assertEqual(worker['exit'], 0)
        tail = view.build_worker('r1', worker['tag'])['tail']
        self.assertEqual([item['kind'] for item in tail], ['start', 'call', 'result', 'says', 'end'])
        self.assertEqual(tail[1]['name'], 'Bash')
        self.assertIn('git status', tail[1]['text'])
        self.assertEqual(tail[2]['text'], 'M notes.txt')

    def test_the_page_shows_the_runner(self):
        self.assertEqual(self.run_plan(helpers.node_text('a')).returncode, 0)
        from pangoloop import view
        saved = (view.RUNS, view.NOTIFY)
        view.RUNS, view.NOTIFY = f'{self.home}/runs', False
        self.addCleanup(lambda: (setattr(view, 'RUNS', saved[0]), setattr(view, 'NOTIFY', saved[1])))
        server = view.Server(('127.0.0.1', 0), view.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        self.assertIn("d.runner ? 'runner ' + d.runner", urllib.request.urlopen(base + '/').read().decode())
        self.assertEqual(json.load(urllib.request.urlopen(base + '/api/run/r1'))['runner'], 'claude-code')

    def test_a_failed_session_is_a_failed_node_and_the_turn_limit_is_noted(self):
        done = self.run_plan(helpers.node_text('a'), STUB_CLAUDE='fail')
        self.assertEqual(done.returncode, 1, done.stdout)
        result = helpers.read_json(f'{self.home}/runs/r1/result.json')
        self.assertEqual(result['nodes'], {'a': 'failed'})
        self.assertEqual(len(result['workers']), 3, 'one try and two repairs, as with every runner')
        self.assertEqual(result['workers'][0]['notes'], ['turn limit'])
        self.assertEqual(result['workers'][0]['exit'], 1)

    def test_a_refused_session_waits_and_starts_again(self):
        done = self.run_plan(helpers.node_text('a'), STUB_CLAUDE='refuse', STUB_COUNT=f'{self.home}/count',
                             STUB_FAST_REFUSAL='1')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        worker = helpers.read_json(f'{self.home}/runs/r1/result.json')['workers'][0]
        self.assertEqual((worker['limit_waits'], worker['ok'], len(self.calls())), (2, True, 3))

    def test_a_node_that_asks_is_called_again_as_a_new_session_with_the_answer(self):
        """Hermes does not continue a session for a node called again either (every worker has its own session name): the
        engine resends the prompt with the whole exchange. The same here."""
        plan = helpers.write_plan(self.home, helpers.node_text('a'))
        environment = support.clean_environment(
            PANGOLOOP_RUNNER='claude-code', PANGOLOOP_CLAUDE_COMMAND=self.wrapper, STUB_LOG=self.log,
            PANGOLOOP_RUNS=f'{self.home}/runs', STUB_CLAUDE='ask', SG_HUMAN='1', SG_HUMAN_WAIT_SECONDS='1')
        done = subprocess.run([sys.executable, DRIVER, plan, self.repo, 'r1'], capture_output=True, text=True,
                              env=environment, cwd=self.home, timeout=120)
        self.assertEqual(done.returncode, 1, done.stdout)   # nobody answered in a second: no_answer_from_human
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--resume', calls[0]['argv'])
        request = helpers.read_json(f'{self.home}/runs/r1/human/a-1.json')
        self.assertEqual((request['for'], request['request']), ('person', 'which one?'))
        self.assertIn('permission was denied', open(f'{support.ROOT}/pangoloop/graph.py').read())


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
