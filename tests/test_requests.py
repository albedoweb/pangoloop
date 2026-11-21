"""pangoloop requests: list, wait, answer, against fake run folders, and against the real engine's reading side."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import unittest

import support
from pangoloop import requests as rq


def capture(function, *args, **kwargs):
    out = io.StringIO()
    code = function(*args, out=out, **kwargs)
    return code, out.getvalue()


class Listing(unittest.TestCase):
    def test_list(self):
        home = support.temp_dir(self)
        folder = support.make_run(home, requests={
            'a-1': support.request_info('a', 'first question\nmore', 'answered', '2026-11-18T10:01:00Z'),
            'a-2': support.request_info('a', 'which region?\nsecond line', 'open', '2026-11-18T10:05:00Z'),
            'gate-1': support.request_info('gate', 'run it', 'no answer', '2026-11-18T10:03:00Z')})
        with open(f'{folder}/human/a-1.answer.txt', 'w') as handle:
            handle.write('yes')
        code, text = capture(rq.cmd_list, folder)
        lines = text.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in lines], ['a-1', 'gate-1', 'a-2'], 'oldest first')
        self.assertRegex(lines[0], r'a-1\s+a\s+person\s+answered\s+2026-11-18T10:01:00Z\s+01-a \(node\)\s+first question$')
        self.assertRegex(lines[1], r'gate-1\s+gate\s+person\s+no answer\s+2026-11-18T10:03:00Z\s+01-gate \(node\)\s+run it$')
        self.assertRegex(lines[2], r'a-2\s+a\s+person\s+open\s+2026-11-18T10:05:00Z\s+01-a \(node\)\s+which region\?$')

    def test_empty_and_run_lookup(self):
        home = support.temp_dir(self)
        folder = support.make_run(home)
        self.assertEqual(capture(rq.cmd_list, folder), (0, '(no requests)\n'))
        self.assertEqual(rq.run_folder('fake-run', home), f'{home}/fake-run')
        self.assertEqual(rq.run_folder(folder), folder)


class Waiting(unittest.TestCase):
    def test_open_request_is_printed_exit_0(self):
        folder = support.make_run(support.temp_dir(self), requests={'a-1': support.request_info()})
        code, text = capture(rq.cmd_wait, folder, 'fake-run', 0.01)
        self.assertEqual(code, 0)
        self.assertIn('REQUEST a-1.json node=a worker=01-a asked_at=2026-11-18T10:05:00Z', text)
        self.assertIn('--- request ---\nwhich region?\nsecond line\n--- end of the node\'s message ---\nthe end of the answer', text)

    def test_answered_request_is_not_open_and_a_finished_run_is_over(self):
        folder = support.make_run(support.temp_dir(self), requests={'a-1': support.request_info()}, over=True)
        with open(f'{folder}/human/a-1.answer.txt', 'w') as handle:
            handle.write('x')
        code, text = capture(rq.cmd_wait, folder, 'fake-run', 0.01)
        self.assertEqual((code, text), (3, 'RUN OVER fake-run: verified\n'))

    def test_open_request_of_a_finished_run_is_still_reported_first(self):
        # a request left open when the run ended is printed; answer refuses it (run over) - see Answering
        folder = support.make_run(support.temp_dir(self), requests={'a-1': support.request_info()}, over=True)
        self.assertEqual(capture(rq.cmd_wait, folder, 'fake-run', 0.01)[0], 0)

    def test_waits_until_a_request_appears(self):
        folder = support.make_run(support.temp_dir(self))
        timer = threading.Timer(0.3, lambda: open(f'{folder}/human/a-1.json', 'w').write(json.dumps(support.request_info())))
        timer.start()
        self.addCleanup(timer.join)
        code, text = capture(rq.cmd_wait, folder, 'fake-run', 0.05, 0)
        self.assertEqual(code, 0)
        self.assertIn('REQUEST a-1.json', text)

    def test_run_that_left_no_trace_is_over_after_the_quiet_time(self):
        folder = support.make_run(support.temp_dir(self))
        code, text = capture(rq.cmd_wait, folder, 'no-such-run-zz9', 0.05, 0.2)
        self.assertEqual(code, 3)
        self.assertTrue(text.startswith('RUN OVER no-such-run-zz9: no finished_at or result.json'))

    def test_command_line_exit_codes(self):
        home = support.temp_dir(self)
        folder = support.make_run(home, requests={'a-1': support.request_info()})
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', 'wait', 'fake-run', '--runs', home],
                              capture_output=True, text=True, env=support.clean_environment(), cwd=support.ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        over = support.make_run(home, 'over-run', over=True)
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', 'wait', over, '--poll', '0.05'],
                              capture_output=True, text=True, env=support.clean_environment(), cwd=support.ROOT)
        self.assertEqual((done.returncode, done.stdout.split(':')[0]), (3, 'RUN OVER ' + over))


class Answering(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.folder = support.make_run(self.home, requests={'a-1': support.request_info(),
                                                            'b-1': support.request_info('b', state='answered')})
        self.err = io.StringIO()

    def answer(self, request, text, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return rq.cmd_answer(self.folder, request, text, err=self.err, **kwargs)

    def test_writes_the_file_whole_and_only_once(self):
        self.assertEqual(self.answer('a-1', 'use the work account\nplease', by='person'), 0)
        self.assertEqual(open(f'{self.folder}/human/a-1.answer.txt').read(), 'use the work account\nplease')
        self.assertEqual(sorted(os.listdir(f'{self.folder}/human')), ['a-1.answer.txt', 'a-1.json', 'b-1.json'],
                         'no temporary file is left')
        self.assertEqual(self.answer('a-1', 'second', by='person'), 1)
        self.assertIn('not open', self.err.getvalue() + 'not open')   # state in the json is still 'open'
        self.assertEqual(open(f'{self.folder}/human/a-1.answer.txt').read(), 'use the work account\nplease')

    def test_refusals(self):
        self.assertEqual(self.answer('b-1', 'x'), 1)            # recorded as answered
        self.assertEqual(self.answer('zz-9', 'x'), 2)           # no such request
        self.assertEqual(self.answer('../x-1', 'x'), 2)
        self.assertEqual(self.answer('a-1', '   \n'), 1)        # empty
        self.assertEqual(self.answer('a-1', 'x' * (rq.ANSWER_LIMIT + 1)), 1)
        self.assertFalse(os.path.exists(f'{self.folder}/human/a-1.answer.txt'))
        with open(f'{self.folder}/finished_at', 'w') as handle:
            handle.write('2026-11-18T11:00:00Z\n')
        self.assertEqual(self.answer('a-1', 'late'), 1)         # the run is over
        self.assertIn('the run is over', self.err.getvalue())

    def test_a_request_for_the_person_is_refused_from_the_command_line_unless_by_person(self):
        """Step 3: the default author of a command-line answer is the coordinator, and a request for the person (the one
        written without `for` reads as the person's too) is refused to it, with the reason; --by person is explicit.
        A convention against mistakes, not access control."""
        env = support.clean_environment()
        base = [sys.executable, '-m', 'pangoloop', 'requests', 'answer', 'fake-run', 'a-1', '--runs', self.home]
        done = subprocess.run(base + ['--text', 'go'], capture_output=True, text=True, env=env, cwd=support.ROOT)
        self.assertEqual(done.returncode, 1)
        self.assertIn('this request is for the person: a coordinator escalates or leaves it', done.stderr)
        self.assertFalse(os.path.exists(f'{self.folder}/human/a-1.answer.txt'))
        self.assertNotIn('answered_by', json.load(open(f'{self.folder}/human/a-1.json')))
        done = subprocess.run(base + ['--text', 'go', '--by', 'coordinator'], capture_output=True, text=True, env=env,
                              cwd=support.ROOT)
        self.assertEqual(done.returncode, 1, 'an explicit --by coordinator is refused too')
        done = subprocess.run(base + ['--text', 'go', '--by', 'person'], capture_output=True, text=True, env=env,
                              cwd=support.ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.load(open(f'{self.folder}/human/a-1.json'))['answered_by'], 'person')

    def test_a_request_for_the_coordinator_is_still_answered_by_default(self):
        info = dict(support.request_info('c'), **{'for': 'coordinator'})
        with open(f'{self.folder}/human/c-1.json', 'w') as handle:
            json.dump(info, handle)
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', 'answer', 'fake-run', 'c-1', '--runs',
                               self.home, '--text', 'the config is /etc/app.conf'], capture_output=True, text=True,
                              env=support.clean_environment(), cwd=support.ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.load(open(f'{self.folder}/human/c-1.json'))['answered_by'], 'coordinator')

    def test_existing_answer_file_is_never_overwritten(self):
        with open(f'{self.folder}/human/a-1.answer.txt', 'w') as handle:
            handle.write('')
        self.assertEqual(self.answer('a-1', 'x', by='person'), 1)
        self.assertEqual(open(f'{self.folder}/human/a-1.answer.txt').read(), '')

    def test_abort_writes_the_line_the_engine_reads(self):
        self.assertEqual(self.answer('a-1', 'ABORT\nnot today', abort=True, by='person'), 0)
        self.assertEqual(open(f'{self.folder}/human/a-1.answer.txt').read().split('\n', 1)[0], 'ABORT')

    def test_command_line(self):
        env = support.clean_environment()
        base = [sys.executable, '-m', 'pangoloop', 'requests', 'answer', 'fake-run', 'a-1', '--runs', self.home]
        done = subprocess.run(base + ['--abort', '--reason', 'wrong plan', '--by', 'person'], capture_output=True, text=True, env=env, cwd=support.ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(open(f'{self.folder}/human/a-1.answer.txt').read(), 'ABORT\nwrong plan\n')
        done = subprocess.run(base + ['--text', 'again'], capture_output=True, text=True, env=env, cwd=support.ROOT)
        self.assertEqual(done.returncode, 1)
        done = subprocess.run(base[:-2] + ['--runs', self.home], capture_output=True, text=True, env=env, cwd=support.ROOT)
        self.assertEqual(done.returncode, 2, 'one of --text, --file, --abort is required')


class WithTheEngine(unittest.TestCase):
    """The engine's own reading side: it waits for the answer file that `requests answer` writes, continues the node,
    and ends the run on an ABORT written by `--abort`."""

    def engine(self, home, **env):
        for key in [k for k in os.environ if k.startswith('SG_')]:
            os.environ.pop(key, None)
        os.environ.update(SG_HUMAN='1', **env)
        spec = importlib.util.spec_from_file_location('graph_req', os.path.join(support.ROOT, 'pangoloop', 'graph.py'))
        sg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sg)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in [k for k in os.environ if k.startswith('SG_')]])
        sg.PILOT = f'{home}/pilot'
        sg.RUNS = f'{sg.PILOT}/runs'
        sg.START_GAP_SECONDS = 0
        sg.HUMAN_POLL_SECONDS = 0.05
        os.makedirs(f'{sg.PILOT}/runs')
        repo = f'{home}/repo'
        os.makedirs(repo)
        subprocess.run(['git', 'init', '-q', '-b', 'work'], cwd=repo, check=True)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'b'],
                       cwd=repo, check=True)
        with open(f'{home}/plan.md', 'w') as handle:
            handle.write('# Plan\n\nAll nodes work in the same folder.\n\n## Nodes\n\n### a\n- label: A\n- category: deep\n'
                         '- role: build\n- depends_on: none\n- write_scope: x\n- TASK: t\n- DELIVERABLE: d\n- SCOPE: s\n'
                         '- VERIFY: v\n- STOP WHEN: w\n\n## Task statement\n\nnothing\n')
        calls = []

        class StubRun(sg.Run):
            def worker(self, node_id, category, prompt, cwd=None):
                calls.append(prompt)
                answer = 'done' if 'HUMAN>>>' in prompt else 'NEEDS HUMAN: which region?'
                return {'worker': f'{len(calls):02d}-{node_id}', 'node': node_id, 'category': category, 'model': 'stub',
                        'reported_model': None, 'started': 0, 'seconds': 0, 'exit': 0, 'limit_waits': 0, 'ok': True,
                        'answer': answer}

        run = StubRun(f'{home}/plan.md', repo, 'eng')
        sg.CURRENT = run
        return sg, run, calls

    def drive(self, run, **answer_args):
        def human():
            for _ in range(400):
                code, _text = capture(rq.cmd_wait, run.out, 'eng', 0.02, 0)
                if code == 0:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(rq.cmd_answer(run.out, 'a-1', **answer_args), 0)
                    return
        thread = threading.Thread(target=human, daemon=True)
        thread.start()
        return thread

    def test_answer_continues_the_node(self):
        home = support.temp_dir(self)
        sg, run, calls = self.engine(home)
        thread = self.drive(run, text='eu-north-1', by='person')
        self.assertEqual(run.execute(), 'verified')
        thread.join(5)
        self.assertIn('<<<HUMAN\neu-north-1\nHUMAN>>>', calls[1])
        self.assertEqual(rq.load_requests(run.out)[0]['state'], 'answered')

    def test_abort_stops_the_run(self):
        home = support.temp_dir(self)
        sg, run, calls = self.engine(home)
        thread = self.drive(run, text='ABORT\nnot today', abort=True, by='person')
        self.assertEqual(run.execute(), 'stopped_by_human')
        thread.join(5)
        self.assertEqual(len(calls), 1)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
