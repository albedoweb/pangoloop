"""Step 2, part B: two kinds of request. `NEEDS COORDINATOR:` and `NEEDS HUMAN:` both make a request file with a field
`for`; only a request for the person calls the person; `pangoloop requests` lists, waits (--for), answers (--by) and
escalates; the viewer tells the two apart; a request nobody answers escalates by itself."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.request

import engine_stub
import helpers
import support
from pangoloop import plan as planmod
from pangoloop import requests as rq

QUESTION_C = 'NEEDS COORDINATOR: where is the config file?'
QUESTION_P = 'NEEDS HUMAN: please run `npm run deploy`'


def quiet(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **kwargs)


def info(kind=None, node='a', state='open', asked_at='2026-11-18T10:05:00Z', **extra):
    data = support.request_info(node, 'which region?\nsecond line', state, asked_at)
    if kind:
        data['for'] = kind
    data.update(extra)
    return data


class Flow:
    """A one-node run on the stub with SG_HUMAN=1; `play(question, helper)` runs it with a helper thread that is given the
    run, and returns what happened. The node asks `question` until it sees an answer in its prompt, then says done."""

    def __init__(self, test, **env):
        self.test = test
        self.home = support.temp_dir(test)
        self.sg = engine_stub.engine(test, self.home, SG_HUMAN='1', **env)
        self.repo = helpers.make_repo(self.home)
        self.opened = []

    def play(self, question, helper, nodes=None, before=None):
        plan = helpers.write_plan(self.home, *(nodes or [helpers.node_text('a')]))
        cls = engine_stub.stub_class(self.sg, lambda n, p, c, calls: 'done' if re.search(r'(HUMAN|COORDINATOR)>>>', p) else question)
        cls.open_page = lambda inner, node, request: self.opened.append((node, request))
        run = cls(plan, self.repo, 'flow')
        self.sg.CURRENT = run
        if before:
            before(run)
        thread = threading.Thread(target=helper, args=(run,), daemon=True)
        thread.start()
        verdict = run.execute()
        thread.join(10)
        self.sg.write_result(run, verdict, run.status)
        self.calls = cls.calls
        return run, verdict

    @staticmethod
    def wait_for_file(run, stem='a-1', limit=10):
        path = f'{run.out}/human/{stem}.json'
        end = time.time() + limit
        while time.time() < end:
            if os.path.exists(path):
                return path
            time.sleep(0.02)
        raise AssertionError('no request file')


class AskingBothWays(unittest.TestCase):
    def test_coordinator_request_is_written_with_for_and_answered_from_the_command_line(self):
        flow = Flow(self)

        def helper(run):
            Flow.wait_for_file(run)
            self.assertEqual(rq.load_requests(run.out)[0]['for'], 'coordinator')
            self.assertEqual(quiet(rq.cmd_answer, run.out, 'a-1', 'in /etc/app.conf'), 0)

        run, verdict = flow.play(QUESTION_C, helper)
        self.assertEqual(verdict, 'verified')
        stored = helpers.read_json(f'{run.out}/human/a-1.json')
        self.assertEqual((stored['for'], stored['answered_by'], stored['state'], stored['request']),
                         ('coordinator', 'coordinator', 'answered', 'where is the config file?'))
        self.assertIn('<<<COORDINATOR\nin /etc/app.conf\nCOORDINATOR>>>', flow.calls[1]['prompt'])
        self.assertIn('asked the coordinator', flow.calls[1]['prompt'])
        self.assertEqual(helpers.read_json(f'{run.out}/result.json')['human'][0]['for'], 'coordinator')

    def test_person_request_is_written_with_for_person(self):
        flow = Flow(self)

        def helper(run):
            Flow.wait_for_file(run)
            self.assertEqual(quiet(rq.cmd_answer, run.out, 'a-1', 'done, deployed', by='person'), 0)

        run, verdict = flow.play(QUESTION_P, helper)
        stored = helpers.read_json(f'{run.out}/human/a-1.json')
        self.assertEqual((verdict, stored['for'], stored['answered_by']), ('verified', 'person', 'person'))
        self.assertIn('<<<HUMAN\ndone, deployed\nHUMAN>>>', flow.calls[1]['prompt'])

    def test_a_person_stepping_in_from_the_page_answers_a_coordinator_request(self):
        from pangoloop import view
        flow = Flow(self)
        saved = view.RUNS
        view.RUNS = f'{flow.home}/runs'
        self.addCleanup(setattr, view, 'RUNS', saved)

        def helper(run):
            Flow.wait_for_file(run)
            self.assertEqual(view.save_answer('flow', 'a-1', 'use the staging one')[0], 200)

        run, verdict = flow.play(QUESTION_C, helper)
        stored = helpers.read_json(f'{run.out}/human/a-1.json')
        self.assertEqual((verdict, stored['for'], stored['answered_by']), ('verified', 'coordinator', 'person'))
        self.assertIn('<<<HUMAN\nuse the staging one\nHUMAN>>>', flow.calls[1]['prompt'], 'the person answered: said so')

    def test_the_last_asking_line_counts_and_bold_marks_are_read(self):
        self.assertEqual(planmod.find_request('x\nNEEDS COORDINATOR: a\n**NEEDS HUMAN:** b\nmore'), ('person', 'b\nmore'))
        self.assertEqual(planmod.find_request('- __NEEDS COORDINATOR:__ a'), ('coordinator', 'a'))
        self.assertEqual(planmod.find_request('NEEDS HUMAN: a\nNEEDS COORDINATOR: b'), ('coordinator', 'b'))
        self.assertIsNone(planmod.find_request('NEEDS COORDINATOR:'), 'an empty question is not a request')
        self.assertIsNone(planmod.find_request('we need a human: no'))
        self.assertIsNone(planmod.find_request(None))

    def test_a_file_without_the_field_reads_as_for_the_person(self):
        from pangoloop import view
        home = support.temp_dir(self)
        folder = support.make_run(home, requests={'a-1': info(), 'a-2': info('coordinator'), 'a-3': info('odd?')})
        self.assertEqual([item['for'] for item in rq.load_requests(folder)], ['person', 'coordinator', 'person'])
        self.assertEqual([item['for'] for item in view.human_requests(folder)], ['person', 'coordinator', 'person'])

    def test_without_the_switch_neither_line_means_anything(self):
        flow = Flow(self)
        for key in [k for k in os.environ if k.startswith('SG_')]:
            os.environ.pop(key)
        sg = engine_stub.engine(self, flow.home)   # SG_HUMAN not set
        plan = helpers.write_plan(flow.home, helpers.node_text('a'))
        cls = engine_stub.stub_class(sg, lambda *args: QUESTION_C)
        run = cls(plan, flow.repo, 'off')
        self.assertEqual(run.execute(), 'verified')
        self.assertFalse(os.path.exists(f'{run.out}/human'))


class PromptText(unittest.TestCase):
    def prompt(self, **kwargs):
        flow = Flow(self)
        plan = helpers.write_plan(flow.home, helpers.node_text('a'), **kwargs)
        return flow.sg.Run(plan, flow.repo, 'p').prompt({'id': 'a', 'label': 'A', 'TASK': 't', 'DELIVERABLE': 'd',
                                                        'SCOPE': 's', 'VERIFY': 'v', 'STOP WHEN': 'w'})

    def test_both_kinds_are_explained_in_plain_words(self):
        text = self.prompt()
        for phrase in ('`NEEDS COORDINATOR: <what and why>`', '`NEEDS HUMAN: <what and why>`', 'a fact you cannot find',
                       'a measurement you cannot take', 'a clarification of the plan',
                       'an ordinary operation the plan requires that your session is not allowed to do',
                       'ask the person, and only the person', "a command that the plan marks as the person's",
                       'outside this repository or on a live system', 'cannot be undone', 'never look for a way around a refusal'):
            self.assertIn(phrase, text)
        self.assertNotIn("The plan marks these as the person's commands", text)

    def test_the_persons_commands_section_goes_to_every_node_with_the_instructions(self):
        for section in ({'before': "## The person's commands\n\nnpm run deploy\nterraform apply\n"},
                        {'after': "\n## The person's commands\n\nnpm run deploy\nterraform apply\n"},
                        {'before': "## The person\u2019s commands\n\nnpm run deploy\nterraform apply\n"}):
            text = self.prompt(**section)
            tail = text.split('`NEEDS HUMAN:`:', 1)[1]
            self.assertEqual(tail.strip(), 'npm run deploy\nterraform apply', section)
            self.assertIn("The plan marks these as the person's commands", text)

    def test_the_section_is_free_text_and_does_not_disturb_the_nodes(self):
        body = planmod.parse_nodes(open(helpers.write_plan(
            support.temp_dir(self), helpers.node_text('a'), helpers.node_text('b', deps='a'),
            before="## The person's commands\n\n- TASK: not a field\n")).read())
        self.assertEqual([node['id'] for node in body], ['a', 'b'])
        self.assertEqual(planmod.person_commands('# P\n\n## Other\n\nx\n'), '')
        self.assertEqual(planmod.person_commands("## The person's commands\nA\n\n## Next\nB\n"), 'A')

    def test_a_commands_section_between_the_nodes_and_the_task_statement_is_cut_from_the_last_node(self):
        plan = helpers.write_plan(support.temp_dir(self), helpers.node_text('a'),
                                  "\n## The person's commands\n\n- label: nonsense\nrun it\n")
        nodes = planmod.parse_nodes(open(plan).read())
        self.assertEqual((len(nodes), nodes[0]['label']), (1, 'a'))
        self.assertEqual(planmod.person_commands(open(plan).read()), '- label: nonsense\nrun it')

    def test_it_is_enforced_nowhere(self):
        """A plan may carry the section; the engine only shows it. A node that answers with no asking line is accepted."""
        flow = Flow(self)
        plan = helpers.write_plan(flow.home, helpers.node_text('a'), before="## The person's commands\n\nrm -rf /\n")
        run = engine_stub.stub_class(flow.sg)(plan, flow.repo, 'e')
        self.assertEqual(run.execute(), 'verified')


class Notifications(unittest.TestCase):
    def test_a_request_for_the_coordinator_calls_nobody_and_a_request_for_the_person_calls_once(self):
        flow = Flow(self, SG_HUMAN_OPEN='1')

        def helper(run):
            Flow.wait_for_file(run)
            time.sleep(0.3)
            self.assertEqual(flow.opened, [], 'silent for the coordinator')
            quiet(rq.cmd_answer, run.out, 'a-1', 'x')

        run, verdict = flow.play(QUESTION_C, helper)
        self.assertEqual((verdict, flow.opened), ('verified', []))
        flow = Flow(self, SG_HUMAN_OPEN='1')
        run, verdict = flow.play(QUESTION_P, lambda run: (Flow.wait_for_file(run), quiet(rq.cmd_answer, run.out, 'a-1', 'x', by='person')))
        self.assertEqual((verdict, flow.opened), ('verified', [('a', 'please run `npm run deploy`')]))

    def test_the_viewers_desktop_notification_is_for_the_person_only(self):
        from pangoloop import view
        home = support.temp_dir(self)
        support.make_run(home, 'n1', requests={'a-1': info('coordinator'), 'b-1': info('person', 'b'), 'c-1': info(None, 'c')})
        saved = (view.RUNS, view.NOTIFY, view.notify, set(view.NOTIFIED))
        view.RUNS, view.NOTIFY, view.NOTIFIED = home, True, set()
        sent = []
        view.notify = lambda title, body, url: sent.append(title)
        self.addCleanup(lambda: (setattr(view, 'RUNS', saved[0]), setattr(view, 'NOTIFY', saved[1]),
                                 setattr(view, 'notify', saved[2]), setattr(view, 'NOTIFIED', saved[3])))
        view.notify_new_requests()
        self.assertEqual(sorted(sent), ['n1: b needs you', 'n1: c needs you'])
        view.notify_new_requests()
        self.assertEqual(len(sent), 2, 'once per request')
        quiet(rq.cmd_escalate, f'{home}/n1', 'a-1', 'nobody answered')   # now it is for the person
        view.notify_new_requests()
        self.assertEqual(sorted(sent)[0], 'n1: a needs you')
        self.assertEqual(len(sent), 3)


class Escalation(unittest.TestCase):
    def test_escalate_turns_a_coordinator_request_into_one_for_the_person_and_records_it(self):
        home = support.temp_dir(self)
        folder = support.make_run(home, requests={'a-1': info('coordinator')})
        out = io.StringIO()
        self.assertEqual(rq.cmd_escalate(folder, 'a-1', 'needs the prod login', out=out), 0)
        self.assertEqual(out.getvalue(), 'escalated a-1 to the person\n')
        stored = helpers.read_json(f'{folder}/human/a-1.json')
        self.assertEqual((stored['for'], stored['escalation_note'], stored['state'], stored['request']),
                         ('person', 'needs the prod login', 'open', 'which region?\nsecond line'))
        self.assertRegex(stored['escalated_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(sorted(os.listdir(f'{folder}/human')), ['a-1.json'], 'no temporary file is left')
        item = rq.load_requests(folder)[0]
        self.assertEqual((item['for'], item['escalation_note']), ('person', 'needs the prod login'))

    def test_refusals(self):
        home = support.temp_dir(self)
        folder = support.make_run(home, requests={'p-1': info('person'), 'old-1': info(None), 'c-1': info('coordinator'),
                                                  'd-1': info('coordinator', state='answered')})
        self.assertEqual(quiet(rq.cmd_escalate, folder, 'p-1'), 1)     # already for the person
        self.assertEqual(quiet(rq.cmd_escalate, folder, 'old-1'), 1)   # no field: for the person
        self.assertEqual(quiet(rq.cmd_escalate, folder, 'd-1'), 1)     # not open
        self.assertEqual(quiet(rq.cmd_escalate, folder, 'zz-9'), 2)
        self.assertEqual(quiet(rq.cmd_escalate, folder, '../x-1'), 2)
        with open(f'{folder}/human/c-1.answer.txt', 'w') as handle:
            handle.write('answered')
        self.assertEqual(quiet(rq.cmd_escalate, folder, 'c-1'), 1)     # has an answer
        self.assertEqual(helpers.read_json(f'{folder}/human/c-1.json')['for'], 'coordinator')
        os.unlink(f'{folder}/human/c-1.answer.txt')
        with open(f'{folder}/finished_at', 'w') as handle:
            handle.write('2026-11-18T11:00:00Z\n')
        self.assertEqual(quiet(rq.cmd_escalate, folder, 'c-1'), 1)     # the run is over

    def test_the_engine_notices_the_change_calls_the_person_and_keeps_it_when_the_answer_is_saved(self):
        flow = Flow(self, SG_HUMAN_OPEN='1')
        seen = {}

        def helper(run):
            Flow.wait_for_file(run)
            end = time.time() + 5
            while run.status.get('a') != 'waiting for coordinator' and time.time() < end:
                time.sleep(0.02)
            seen['before'] = (run.status.get('a'), list(flow.opened))
            self.assertEqual(quiet(rq.cmd_escalate, run.out, 'a-1', 'no one is there'), 0)
            while run.status.get('a') != 'waiting for human' and time.time() < end:
                time.sleep(0.02)
            seen['after'] = (run.status.get('a'), list(flow.opened))
            quiet(rq.cmd_answer, run.out, 'a-1', 'fine, do it', by='person')

        run, verdict = flow.play(QUESTION_C, helper)
        self.assertEqual(verdict, 'verified')
        self.assertEqual(seen['before'], ('waiting for coordinator', []))
        self.assertEqual(seen['after'], ('waiting for human', [('a', 'where is the config file?')]))
        stored = helpers.read_json(f'{run.out}/human/a-1.json')
        self.assertEqual((stored['for'], stored['escalation_note'], stored['answered_by'], stored['state']),
                         ('person', 'no one is there', 'person', 'answered'))
        logged = helpers.read_json(f'{run.out}/result.json')['human'][0]
        self.assertEqual((logged['for'], logged['answered_by']), ('person', 'person'))
        self.assertEqual(len(flow.opened), 1, 'the person was called once, at the escalation')

    def test_a_request_nobody_answers_escalates_itself_with_the_note(self):
        flow = Flow(self, SG_HUMAN_OPEN='1')
        flow.sg.COORDINATOR_WAIT_SECONDS = 0.4
        seen = {}

        def helper(run):
            path = Flow.wait_for_file(run)
            time.sleep(0.1)
            seen['early'] = helpers.read_json(path)['for']
            end = time.time() + 5
            while helpers.read_json(path)['for'] != 'person' and time.time() < end:
                time.sleep(0.02)
            seen['later'] = helpers.read_json(path)
            quiet(rq.cmd_answer, run.out, 'a-1', 'ok', by='person')

        run, verdict = flow.play(QUESTION_C, helper)
        self.assertEqual(verdict, 'verified')
        self.assertEqual(seen['early'], 'coordinator')
        self.assertEqual(seen['later']['for'], 'person')
        self.assertTrue(seen['later']['escalation_note'].startswith('no coordinator answered in '))
        self.assertTrue(seen['later']['escalation_note'].endswith(' minutes'))
        self.assertTrue(seen['later']['escalated_at'])
        self.assertEqual(flow.opened, [('a', 'where is the config file?')])

    def test_the_note_names_the_wait_in_minutes_and_the_variable_sets_it(self):
        from pangoloop import graph
        self.assertEqual(graph.escalation_note(900), 'no coordinator answered in 15 minutes')
        self.assertEqual(graph.escalation_note(60), 'no coordinator answered in 1 minutes')
        shown = []
        for env, expected in (({}, '900.0'), ({'PANGOLOOP_COORDINATOR_WAIT_SECONDS': '42'}, '42.0')):
            done = subprocess.run([sys.executable, '-c', 'from pangoloop import graph; print(graph.COORDINATOR_WAIT_SECONDS)'],
                                  capture_output=True, text=True, env=support.clean_environment(**env), cwd=support.ROOT)
            shown.append(done.stdout.strip())
        self.assertEqual(shown, ['900.0', '42.0'])


class RequestsCommand(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.folder = support.make_run(self.home, requests={
            'a-1': info('person', 'a', asked_at='2026-11-18T10:01:00Z'),
            'b-1': info('coordinator', 'b', asked_at='2026-11-18T10:02:00Z')})

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', *args, '--runs', self.home], capture_output=True,
                              text=True, env=support.clean_environment(), cwd=support.ROOT, timeout=60)

    def test_list_shows_the_for_column(self):
        done = self.command('list', 'fake-run')
        rows = [line.split() for line in done.stdout.splitlines()]
        self.assertEqual([(row[0], row[1], row[2], row[3]) for row in rows],
                         [('a-1', 'a', 'person', 'open'), ('b-1', 'b', 'coordinator', 'open')])

    def test_wait_for_chooses_whose_requests_it_reports(self):
        for choice, first in (('coordinator', 'b-1'), ('person', 'a-1'), ('any', 'a-1')):
            args = ['wait', 'fake-run', '--poll', '0.05'] + ([] if choice == 'any' else ['--for', choice])
            done = self.command(*args)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(done.stdout.startswith(f'REQUEST {first}.json'), (choice, done.stdout))
            self.assertIn(f"for={'coordinator' if first == 'b-1' else 'person'} worker_kind=node\n", done.stdout.splitlines()[0] + '\n')

    def test_wait_for_coordinator_ignores_a_request_for_the_person_and_ends_with_the_run(self):
        folder = support.make_run(self.home, 'only-person', requests={'a-1': info('person')}, over=True)
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', 'wait', folder, '--for', 'coordinator', '--poll', '0.05'],
                              capture_output=True, text=True, env=support.clean_environment(), cwd=support.ROOT, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (3, 'RUN OVER ' + folder + ': verified\n'))
        self.assertEqual(self.command('wait', 'fake-run', '--for', 'nobody').returncode, 2, 'argparse refuses it')

    def test_wait_for_coordinator_returns_a_request_that_appears_later(self):
        folder = support.make_run(self.home, 'later')
        timer = threading.Timer(0.3, lambda: open(f'{folder}/human/a-1.json', 'w').write(json.dumps(info('person'))))
        timer2 = threading.Timer(0.6, lambda: open(f'{folder}/human/b-1.json', 'w').write(json.dumps(info('coordinator', 'b'))))
        for timer_ in (timer, timer2):
            timer_.start()
            self.addCleanup(timer_.join)
        out = io.StringIO()
        self.assertEqual(rq.cmd_wait(folder, 'later', 0.05, 0, out=out, for_='coordinator'), 0)
        self.assertTrue(out.getvalue().startswith('REQUEST b-1.json'))

    def test_answer_records_who_answered(self):
        done = self.command('answer', 'fake-run', 'b-1', '--text', 'use /etc/app.conf')
        self.assertEqual(done.returncode, 0, done.stderr)
        stored = helpers.read_json(f'{self.folder}/human/b-1.json')
        self.assertEqual((stored['answered_by'], stored['for'], stored['state'], stored['request']),
                         ('coordinator', 'coordinator', 'open', 'which region?\nsecond line'))
        done = self.command('answer', 'fake-run', 'a-1', '--text', 'ok', '--by', 'person')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(helpers.read_json(f'{self.folder}/human/a-1.json')['answered_by'], 'person')
        self.assertEqual(rq.load_requests(self.folder)[0]['answered_by'], 'person')
        self.assertEqual(self.command('answer', 'fake-run', 'a-1', '--text', 'ok', '--by', 'robot').returncode, 2)

    def test_a_refused_answer_leaves_no_signature(self):
        self.assertEqual(quiet(rq.cmd_answer, self.folder, 'a-1', 'first', by='person'), 0)
        self.assertEqual(quiet(rq.cmd_answer, self.folder, 'a-1', 'second', by='coordinator'), 1)
        self.assertEqual(helpers.read_json(f'{self.folder}/human/a-1.json')['answered_by'], 'person')
        with open(f'{self.folder}/human/b-1.answer.txt', 'w') as handle:   # an answer file appeared meanwhile
            handle.write('')
        self.assertEqual(quiet(rq.cmd_answer, self.folder, 'b-1', 'x', by='person'), 1)
        self.assertNotIn('answered_by', helpers.read_json(f'{self.folder}/human/b-1.json'))

    def test_the_command_line_escalates(self):
        done = self.command('escalate', 'fake-run', 'b-1', '--note', 'needs the person')
        self.assertEqual((done.returncode, done.stdout), (0, 'escalated b-1 to the person\n'))
        self.assertEqual(self.command('escalate', 'fake-run', 'b-1').returncode, 1)
        self.assertEqual(self.command('escalate', 'fake-run', 'zz-1').returncode, 2)
        self.assertEqual(self.command('list', 'fake-run').stdout.split()[10:12], ['b', 'person'])
        for args in (['escalate', '--help'], ['wait', '--help'], ['answer', '--help']):
            self.assertIn('usage', subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', *args], capture_output=True,
                                                  text=True, env=support.clean_environment(), cwd=support.ROOT).stdout)


class Viewer(unittest.TestCase):
    def setUp(self):
        from pangoloop import view
        self.view = view
        self.home = support.temp_dir(self)
        saved = (view.RUNS, view.NOTIFY)
        view.RUNS, view.NOTIFY = self.home, False
        self.addCleanup(lambda: (setattr(view, 'RUNS', saved[0]), setattr(view, 'NOTIFY', saved[1])))

    def run_with(self, name, requests, over=False, commands=''):
        folder = support.make_run(self.home, name, requests=requests, over=over)
        text = open(f'{folder}/plan.md').read()
        if commands:
            text = text.replace('## Nodes', f"## The person's commands\n\n{commands}\n\n## Nodes", 1)
        text = text.replace('\n## Task statement', '\n### b\n- label: B\n- category: deep\n- role: build\n- depends_on: none\n'
                            '- write_scope: x\n- TASK: t\n- DELIVERABLE: d\n- SCOPE: s\n- VERIFY: v\n- STOP WHEN: w\n\n'
                            '## Task statement', 1)
        with open(f'{folder}/plan.md', 'w') as handle:
            handle.write(text)
        return folder

    def states(self, name):
        data = self.view.build_run(name)
        return data['run_state'], {node['id']: node['state'] for node in data['nodes']}, data

    def test_run_states_and_node_states(self):
        self.run_with('co', {'a-1': info('coordinator')})
        self.run_with('pe', {'a-1': info('person')})
        self.run_with('both', {'a-1': info('coordinator'), 'b-1': info('person', 'b')})
        self.run_with('old', {'a-1': info(None)})
        self.assertEqual(self.states('co')[:2], ('waiting for the coordinator', {'a': 'waiting for coordinator', 'b': 'ready'}))
        self.assertEqual(self.states('pe')[:2], ('waiting for you', {'a': 'waiting for human', 'b': 'ready'}))
        self.assertEqual(self.states('both')[:2], ('waiting for you', {'a': 'waiting for coordinator', 'b': 'waiting for human'}))
        self.assertEqual(self.states('old')[0], 'waiting for you')
        listed = {entry['name']: entry['state'] for entry in self.view.list_runs()}
        self.assertEqual(listed, {'co': 'waiting for the coordinator', 'pe': 'waiting for you', 'both': 'waiting for you',
                                  'old': 'waiting for you'})

    def test_escalation_moves_a_run_to_waiting_for_you(self):
        folder = self.run_with('esc', {'a-1': info('coordinator')})
        self.assertEqual(self.states('esc')[0], 'waiting for the coordinator')
        quiet(rq.cmd_escalate, folder, 'a-1', 'too hard')
        run_state, _, data = self.states('esc')
        self.assertEqual(run_state, 'waiting for you')
        item = data['human'][0]
        self.assertEqual((item['for'], item['escalation_note']), ('person', 'too hard'))
        self.assertTrue(item['escalated_at'])

    def test_answered_requests_say_who_answered_and_a_finished_run_waits_for_nobody(self):
        folder = self.run_with('ans', {'a-1': info('coordinator'), 'b-1': info('person', 'b')})
        quiet(rq.cmd_answer, folder, 'a-1', 'x')
        quiet(rq.cmd_answer, folder, 'b-1', 'y', by='person')
        run_state, _, data = self.states('ans')
        self.assertEqual({(item['name'], item['state'], item['answered_by']) for item in data['human']},
                         {('a-1', 'answered', 'coordinator'), ('b-1', 'answered', 'person')})
        self.assertEqual(run_state, 'running')
        self.run_with('over', {'a-1': info('coordinator')}, over=True)
        self.assertEqual(self.states('over')[0], 'finished: verified')

    def test_the_persons_commands_are_in_the_api(self):
        self.run_with('cmds', {'a-1': info('person')}, commands='npm run deploy')
        self.assertEqual(self.states('cmds')[2]['person_commands'], 'npm run deploy')
        self.run_with('nocmds', {'a-1': info('person')})
        self.assertEqual(self.states('nocmds')[2]['person_commands'], '')

    def test_the_page_answers_a_coordinator_request_as_the_person_over_http(self):
        view = self.view
        folder = self.run_with('web', {'a-1': info('coordinator')})
        server = view.Server(('127.0.0.1', 0), view.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        base = f'http://127.0.0.1:{port}'
        data = json.load(urllib.request.urlopen(f'{base}/api/run/web'))
        self.assertEqual((data['run_state'], data['human_open'][0]['for']), ('waiting for the coordinator', 'coordinator'))
        request = urllib.request.Request(f'{base}/api/run/web/human/a-1', data=json.dumps({'answer': 'stepping in'}).encode(),
                                         headers={'Content-Type': 'application/json', 'Origin': base}, method='POST')
        self.assertEqual(json.load(urllib.request.urlopen(request))['ok'], True)
        stored = helpers.read_json(f'{folder}/human/a-1.json')
        self.assertEqual((stored['answered_by'], stored['for']), ('person', 'coordinator'))
        self.assertEqual(open(f'{folder}/human/a-1.answer.txt').read(), 'stepping in')
        after = json.load(urllib.request.urlopen(f'{base}/api/run/web'))
        self.assertEqual((after['human'][0]['answered_by'], after['human_open']), ('person', []))

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_pages_script_is_valid_javascript(self):
        script = re.search(r'<script>(.*)</script>', self.view.PAGE, re.S).group(1).replace('__RUN__', '"x"')
        path = os.path.join(support.temp_dir(self), 'page.js')
        with open(path, 'w') as handle:
            handle.write(script)
        done = subprocess.run(['node', '--check', path], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_page_markup(self):
        page = self.view.PAGE
        self.assertIn('for the coordinator', page)
        self.assertIn('waiting for the coordinator', page)
        self.assertIn('is waiting for you', page)
        body = page[page.index('function askBox'):page.index('function restoreDrafts')]
        self.assertIn('copyBtn(r.name', body, 'the copy button is on both kinds')
        self.assertIn("r.for === 'coordinator'", body)
        banner = page[page.index('function renderHuman'):page.index("document.addEventListener('input'")]
        self.assertIn("r.for === 'person'", banner, 'the banner is for the person only')
        panel = page[page.index('(n.human||[]).length'):page.index("if (n.kind === 'acceptance') {\n    h += `<div class=\"sect\"><div class=\"l\">Rounds")]
        self.assertIn('askBox(r)', panel)
        self.assertIn('answered by the', panel)
        self.assertIn('copyBtn(r.name', panel)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
