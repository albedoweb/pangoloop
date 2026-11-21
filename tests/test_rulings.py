"""Step 5 (November 18, 2026), the first items of the plan: rulings travel (A), a closing line that is not a request
(B), a request names who asked (C), preflight warns about what a reader cannot read (D), small things (E).

Everything runs on stubs and fake run folders; the regression test of A reads copies of the request files of an invented
run (tests/fixtures/staged_run)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import engine_stub
import helpers
import support
from pangoloop import plan as planmod
from pangoloop import preflight, rulings
from pangoloop import requests as rq
from pangoloop.runners import RunResult

STAGED_RUN = os.path.join(support.ROOT, 'tests', 'fixtures', 'staged_run')   # an invented run with the shape of a real one
CLOSING_LINE = 'NEEDS COORDINATOR: none — part C is complete and verified.'


def quiet(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **kwargs)


def ruling(ident='a-1', node='a', worker='01-a', by='coordinator', at='2026-11-18T10:00:00Z', question='which file?',
           answer='use b.txt'):
    return {'id': ident, 'node': node, 'worker': worker, 'by': by, 'at': at, 'question': question, 'answer': answer}


class Flow:
    """A run on the stub with SG_HUMAN=1: node `a` asks two questions of the coordinator and a helper thread answers them;
    then the review node `r` asks for changes once (so there is a repair worker `r-fix1` and a second review)."""

    def __init__(self, test, answers=('use b.txt', 'move it to lib/'), limit=None):
        self.home = support.temp_dir(test)
        self.sg = engine_stub.engine(test, self.home, SG_HUMAN='1')
        if limit is not None:
            self.sg.RULINGS_CHARS = limit
        self.repo = helpers.make_repo(self.home)
        self.answers = list(answers)

    def play(self, nodes=None, behave=None, helper=None):
        plan = helpers.write_plan(self.home, *(nodes or [
            helpers.node_text('a'), helpers.node_text('r', role='review', category='review', deps='a')]))
        questions = ['NEEDS COORDINATOR: which file?', 'NEEDS COORDINATOR: where to move it?']

        def default(node, prompt, cwd, calls):
            if node == 'a':
                count = prompt.count('COORDINATOR>>>')
                return questions[count] if count < len(self.answers) else None
            if node == 'r':
                return 'VERDICT: CHANGES REQUESTED\nFINDING 1: a thing' if sum(c['node'] == 'r' for c in calls) == 1 \
                    else 'VERDICT: PASS'
            return None

        cls = engine_stub.stub_class(self.sg, behave or default)
        cls.open_page = lambda inner, node, request: None
        run = cls(plan, self.repo, 'flow')
        self.sg.CURRENT = run
        thread = threading.Thread(target=helper or self.answer_all, args=(run,), daemon=True)
        thread.start()
        verdict = run.execute()
        thread.join(10)
        self.sg.write_result(run, verdict, run.status)
        self.calls = cls.calls
        self.run = run
        return run, verdict

    def answer_all(self, run):
        given, end = 0, time.time() + 20
        while given < len(self.answers) and time.time() < end:
            waiting = rq.open_requests(run.out)
            if waiting:
                quiet(rq.cmd_answer, run.out, waiting[0]['id'], self.answers[given])
                given += 1
            time.sleep(0.02)

    def prompts(self, node):
        return [call['prompt'] for call in self.calls if call['node'] == node]


class RulingsTravel(unittest.TestCase):
    def test_the_file_after_two_answers(self):
        flow = Flow(self)
        run, verdict = flow.play()
        self.assertEqual(verdict, 'verified')
        found = rulings.load(run.out)
        self.assertEqual([(r['id'], r['node'], r['worker'], r['by'], r['question'], r['answer']) for r in found],
                         [('a-1', 'a', '01-a', 'coordinator', 'which file?', 'use b.txt'),
                          ('a-2', 'a', '02-a', 'coordinator', 'where to move it?', 'move it to lib/')])
        self.assertTrue(all(r['at'] for r in found))
        with open(f'{run.out}/rulings.md') as handle:
            text = handle.read()
        self.assertTrue(text.startswith(rulings.TITLE))
        self.assertEqual(text, rulings.TITLE + ''.join(rulings.render(r) for r in found), 'appended in order, nothing else')
        self.assertIn('Question:\n\nwhich file?\n\nAnswer:\n\nuse b.txt\n', text)

    def test_the_engine_file_equals_the_one_built_from_the_request_files(self):
        run, _ = Flow(self).play()
        written = open(f'{run.out}/rulings.md').read()
        self.assertEqual([r['id'] for r in rulings.from_requests(run.out)], ['a-1', 'a-2'])
        rulings.rebuild(run.out)
        self.assertEqual(open(f'{run.out}/rulings.md').read(), written)

    def test_a_later_node_a_repair_worker_and_a_second_review_all_get_the_earlier_ruling(self):
        flow = Flow(self)
        run, verdict = flow.play()
        title = rulings.SECTION_TITLE
        first_a = flow.prompts('a')[0]
        self.assertNotIn('Rulings made during this run', first_a, 'no ruling yet: the prompt is the old one')
        second_a, third_a = flow.prompts('a')[1:3]   # the node that asked is called again
        self.assertIn(title, second_a)
        self.assertIn('use b.txt', second_a)
        self.assertNotIn('move it to lib/', second_a, 'not made yet')
        self.assertIn('move it to lib/', third_a)
        first_review, second_review = flow.prompts('r')
        fix = flow.prompts('r-fix1')[0]
        for prompt in (first_review, fix, second_review):
            self.assertIn(title, prompt)
            self.assertIn('use b.txt', prompt)
            self.assertIn('move it to lib/', prompt)
            self.assertLess(prompt.index(title), prompt.index('TASK:'), 'near the top, before the node text')
            self.assertTrue(prompt.startswith('Before anything else read '), 'the plan pointer stays first')
        for prompt in (first_review, second_review):
            self.assertIn(rulings.REVIEWER_LINE, prompt)
        self.assertEqual(rulings.REVIEWER_LINE, 'Do not report as a fault something a ruling required.')
        self.assertNotIn(rulings.REVIEWER_LINE, fix)
        self.assertNotIn(rulings.REVIEWER_LINE, third_a)

    def test_a_rebase_repair_and_an_acceptance_repair_get_the_rulings_too(self):
        flow = Flow(self)
        run, _ = flow.play()
        # the two other repair prompts go through the same `ask`; call it as the engine does for each
        for node_id, category in (('a-rebase1', 'deep'), ('acc-fix1', 'deep')):
            seen = flow.sg.Run.with_rulings(run, 'Before anything else read x.\n\nYour node is `n`.\n', category)
            self.assertIn('use b.txt', seen, node_id)

    def test_the_section_is_cut_over_the_limit_the_newest_in_full(self):
        folder = support.make_run(support.temp_dir(self))
        long_answer = 'x' * 3000 + 'TAIL-OF-THE-OLD-ONE'
        rulings.append(folder, ruling('a-1', answer=long_answer, at='2026-11-18T10:00:00Z'))
        rulings.append(folder, ruling('a-2', answer='the newest answer', question='second?', at='2026-11-18T10:05:00Z'))
        small = rulings.section(folder, limit=100000)
        self.assertIn('TAIL-OF-THE-OLD-ONE', small)
        self.assertNotIn(rulings.path_of(folder), small, 'under the limit: all in full, no path')
        cut = rulings.section(folder, limit=2000)
        self.assertIn('the newest answer', cut)
        self.assertIn('second?', cut, 'the newest in full, question and answer')
        self.assertNotIn('TAIL-OF-THE-OLD-ONE', cut)
        self.assertIn('x' * rulings.OLDER_CHARS, cut)
        self.assertNotIn('x' * (rulings.OLDER_CHARS + 1), cut, 'the older one keeps its first 600 characters')
        self.assertIn(rulings.path_of(folder), cut, 'the path of rulings.md to read the rest')
        self.assertEqual(rulings.DEFAULT_LIMIT, 12000)
        self.assertEqual(rulings.section(support.make_run(support.temp_dir(self), name='empty')), '')

    def test_the_limit_is_a_setting_and_the_engine_uses_it(self):
        from pangoloop import config
        self.assertEqual(config.load({}, support.temp_dir(self)).limits['rulings_chars'], 12000)
        self.assertEqual(config.load({'PANGOLOOP_RULINGS_CHARS': '50'}, support.temp_dir(self)).limits['rulings_chars'], 50)
        flow = Flow(self, answers=('A' * 900, 'move it to lib/'), limit=500)
        run, _ = flow.play()
        third = flow.prompts('a')[2]
        self.assertIn('move it to lib/', third)
        section = third.split('Your node is')[0]   # the node's own answer below it stays in full, as before
        self.assertNotIn('A' * 601, section)
        self.assertIn(rulings.path_of(run.out), section)

    def test_an_abort_is_not_a_ruling_and_neither_is_a_blank_answer(self):
        flow = Flow(self, answers=('ABORT\nnot today',))
        run, verdict = flow.play(helper=lambda run: (time.sleep(0.3), quiet(rq.cmd_answer, run.out, 'a-1', 'ABORT\nnot today')))
        self.assertEqual(verdict, 'stopped_by_human')
        self.assertFalse(os.path.exists(f'{run.out}/rulings.md'))
        self.assertFalse(rulings.is_ruling({'request': 'which?'}, 'ABORT\nno'))
        self.assertFalse(rulings.is_ruling({'request': 'which?'}, '  \n'))
        self.assertTrue(rulings.is_ruling({'request': 'which?'}, 'use b'))

    def test_a_ruling_with_awkward_text_round_trips(self):
        folder = support.make_run(support.temp_dir(self))
        tricky = ruling('a-1', question='## ruling x | node: y\n\nAnswer:\n\nnot a heading', answer='\ttabs\r\nand\n\n\nblank lines\n## ruling z')
        rulings.append(folder, tricky)
        rulings.append(folder, ruling('a-2'))
        found = rulings.load(folder)
        self.assertEqual([r['id'] for r in found], ['a-1', 'a-2'])
        self.assertEqual(found[0]['question'], tricky['question'])
        self.assertEqual(found[0]['answer'], tricky['answer'])


class RulingsOnThePage(unittest.TestCase):
    def setUp(self):
        from pangoloop import view
        self.view = view
        self.home = support.temp_dir(self)
        saved = view.RUNS
        view.RUNS = self.home
        self.addCleanup(setattr, view, 'RUNS', saved)
        self.folder = support.make_run(self.home, requests={
            'a-1': dict(support.request_info('a', 'which file?', 'answered', '2026-11-18T10:01:00Z'), **{'for': 'coordinator'}),
            'a-2': dict(support.request_info('a', 'where?', 'answered', '2026-11-18T10:05:00Z'), **{'for': 'coordinator'})})
        for stem, text in (('a-1', 'use b.txt'), ('a-2', 'lib/')):
            with open(f'{self.folder}/human/{stem}.answer.txt', 'w') as handle:
                handle.write(text)
        rulings.append(self.folder, ruling('a-1', at='2026-11-18T10:02:00Z'))
        rulings.append(self.folder, ruling('a-2', question='where?', answer='lib/', at='2026-11-18T10:06:00Z'))
        rulings.append(self.folder, ruling('ruling-1', node=None, worker=None, by='person', question='', answer='no new deps',
                                           at='2026-11-18T10:07:00Z'))

    def test_the_api_lists_the_rulings_newest_first(self):
        data = self.view.build_run('fake-run')
        self.assertEqual([r['id'] for r in data['rulings']], ['ruling-1', 'a-2', 'a-1'])
        newest = data['rulings'][0]
        self.assertEqual((newest['by'], newest['node'], newest['question'], newest['answer']), ('person', None, '', 'no new deps'))
        self.assertIsInstance(newest['at'], int)
        self.assertEqual(data['rulings'][2]['question'], 'which file?')
        ids = {r['id'] for r in data['rulings']}
        self.assertTrue(all(r['name'] in ids for r in data['human']), 'each answered request has its ruling to link to')

    def test_the_page_has_the_panel_and_the_links(self):
        page = self.view.PAGE
        for needle in ('id="rulings"', 'function renderRulings', 'class="ruling"', 'a.rulink', 'worker_kind'):
            self.assertIn(needle, page)

    def test_a_run_without_rulings_has_an_empty_list_and_an_old_run_is_read_from_its_requests(self):
        os.remove(f'{self.folder}/rulings.md')
        data = self.view.build_run('fake-run')
        self.assertEqual([r['id'] for r in data['rulings']], ['a-2', 'a-1'], 'built from the request files')
        empty = support.make_run(self.home, name='empty-run')
        self.assertEqual(self.view.build_run('empty-run')['rulings'], [])


class RulingsCommands(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.folder = support.make_run(self.home)

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', *args, '--runs', self.home], capture_output=True,
                              text=True, env=support.clean_environment(), cwd=support.ROOT, timeout=60)

    def test_rulings_prints_them_in_order_and_says_when_there_are_none(self):
        self.assertEqual(self.command('rulings', 'fake-run').stdout, '(no rulings)\n')
        rulings.append(self.folder, ruling('a-1'))
        rulings.append(self.folder, ruling('a-2', question='second?', answer='second answer', at='2026-11-18T10:05:00Z'))
        done = self.command('rulings', 'fake-run')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, rulings.render(ruling('a-1')) + rulings.render(
            ruling('a-2', question='second?', answer='second answer', at='2026-11-18T10:05:00Z')))
        self.assertLess(done.stdout.index('use b.txt'), done.stdout.index('second answer'))

    def test_rule_adds_a_ruling_without_a_question(self):
        done = self.command('rule', 'fake-run', '--text', 'no new dependencies anywhere')
        self.assertEqual((done.returncode, done.stdout), (0, 'added ruling-1\n'), done.stderr)
        note = os.path.join(self.home, 'note.txt')
        with open(note, 'w') as handle:
            handle.write('second ruling\nfrom a file\n')
        done = self.command('rule', 'fake-run', '--file', note, '--by', 'person')
        self.assertEqual((done.returncode, done.stdout), (0, 'added ruling-2\n'), done.stderr)
        found = rulings.load(self.folder)
        self.assertEqual([(r['id'], r['by'], r['node'], r['question']) for r in found],
                         [('ruling-1', 'coordinator', None, ''), ('ruling-2', 'person', None, '')])
        self.assertEqual(found[1]['answer'], 'second ruling\nfrom a file')
        self.assertIn('no new dependencies anywhere', rulings.section(self.folder))
        self.assertIn('answered by the person', rulings.section(self.folder))

    def test_rule_refuses_an_empty_text_and_a_run_that_is_over(self):
        self.assertEqual(self.command('rule', 'fake-run', '--text', '  ').returncode, 1)
        over = support.make_run(self.home, name='over-run', over=True)
        done = self.command('rule', 'over-run', '--text', 'x')
        self.assertEqual(done.returncode, 1)
        self.assertIn('the run is over', done.stderr)
        self.assertFalse(os.path.exists(f'{over}/rulings.md'))
        self.assertEqual(self.command('rule', 'fake-run').returncode, 2, 'one of --text and --file is needed')

    def test_the_help_says_running_nodes_do_not_see_a_new_ruling_at_once(self):
        done = self.command('rule', '--help')
        self.assertIn('until their next prompt', ' '.join(done.stdout.split()))
        self.assertEqual(self.command('rulings', '--help').returncode, 0)

    def test_two_writers_at_once_do_not_tear_the_file(self):
        def many(prefix):
            for k in range(30):
                rulings.append(self.folder, ruling(f'{prefix}-{k}', answer=('line\n' * 20) + prefix))
        threads = [threading.Thread(target=many, args=(name,)) for name in ('p', 'q', 'r')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(rulings.load(self.folder)), 90)


class StagedRun(unittest.TestCase):
    """An invented run shaped like a staged one (tests/fixtures/staged_run): seven requests;
    the first is `none` with an explanation and is no ruling, the other six are. Works on copies of the fixture."""

    def setUp(self):
        self.folder = os.path.join(support.temp_dir(self), 'r2')
        os.makedirs(self.folder)
        shutil.copytree(f'{STAGED_RUN}/human', f'{self.folder}/human')
        shutil.copy(f'{STAGED_RUN}/plan.md', f'{self.folder}/plan.md')

    def test_seven_requests_six_rulings(self):
        self.assertEqual(len([n for n in os.listdir(f'{self.folder}/human') if n.endswith('.json')]), 7)
        found = rulings.from_requests(self.folder)
        ids = [item['id'] for item in found]
        self.assertNotIn('styles-1', ids, 'its question was `none — part C is complete and verified.`')
        self.assertEqual(sorted(ids), sorted(['fixture-1', 'play-1', 'exports-1', 'review-1', 'review-2', 'acceptance-final-1']))
        self.assertEqual(ids, ['fixture-1', 'play-1', 'exports-1', 'review-1', 'review-2', 'acceptance-final-1'],
                         'in the order they were answered')
        written = rulings.rebuild(self.folder)
        self.assertEqual([item['id'] for item in written], ids)
        again = rulings.load(self.folder)
        self.assertEqual([(r['id'], r['answer']) for r in again], [(r['id'], r['answer']) for r in found])
        for item in again:
            with open(f"{self.folder}/human/{item['id']}.answer.txt") as handle:
                self.assertEqual(item['answer'], handle.read().rstrip('\n'), 'the answer in full')
        self.assertEqual(again[0]['by'], 'coordinator')
        section = rulings.section(self.folder, reviewer=True)
        self.assertIn(rulings.REVIEWER_LINE, section)

    def test_the_requests_of_repair_workers_say_who_asked(self):
        shown = {item['id']: item for item in rq.load_requests(self.folder)}
        self.assertEqual((shown['review-2']['node'], shown['review-2']['worker'], shown['review-2']['worker_kind']),
                         ('review', '15-review-fix1', 'repair'))
        self.assertEqual(shown['review-1']['worker_kind'], 'review')
        self.assertEqual(shown['acceptance-final-1']['worker_kind'], 'acceptance-repair')
        self.assertEqual(shown['exports-1']['worker_kind'], 'node')


class ClosingLineThatIsNotARequest(unittest.TestCase):
    def test_the_exact_line_of_the_real_run_is_ignored(self):
        answer = 'All done.\n\n' + CLOSING_LINE
        self.assertIsNone(planmod.find_request(answer))
        self.assertEqual(planmod.ignored_request(answer), '—'.join(['none ', ' part C is complete and verified.']))
        self.assertIsNone(planmod.ignored_request('no asking line here'))

    def test_empty_and_none_like_questions_are_not_requests(self):
        for question in ('', ' ', '.', '—', 'none', 'None.', 'NONE — all fine', 'nothing', 'Nothing to ask',
                         'n/a', 'N/A: not applicable', 'not needed', 'Not needed, thanks', 'no question', 'no questions',
                         'No question at this time', '**none**', '(none)', '- none'):
            for kind in ('COORDINATOR', 'HUMAN'):
                self.assertIsNone(planmod.find_request(f'text\nNEEDS {kind}: {question}'), (kind, question))
                self.assertTrue(planmod.is_empty_question(question), question)

    def test_real_questions_still_are_requests(self):
        self.assertIsNotNone(planmod.find_request('NEEDS COORDINATOR: no rule covers a node that has to delete a folder'))
        self.assertIsNotNone(planmod.find_request('NEEDS HUMAN: No. 3 of the commands needs your approval'))
        self.assertIsNotNone(planmod.find_request('NEEDS HUMAN: no space left on device: may I clear the cache?'))
        self.assertIsNone(planmod.find_request('NEEDS COORDINATOR: no question'))
        self.assertIsNone(planmod.find_request('NEEDS COORDINATOR: None.'))
        for question in ('nonexistent path?', 'which region?', 'notes.txt is missing; what now?', 'nothingness',
                         'normal text', 'nobody answered', 'now what', 'n/ab', 'noted: which one?'):
            self.assertIsNotNone(planmod.find_request(f'NEEDS COORDINATOR: {question}'), question)

    def test_an_earlier_real_request_does_not_survive_a_closing_none(self):
        self.assertIsNone(planmod.find_request('NEEDS COORDINATOR: which one?\nNEEDS COORDINATOR: none'))

    def test_the_engine_does_not_stop_the_node_and_the_worker_record_says_so(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home)
        wrapper = os.path.join(home, 'claude')
        with open(wrapper, 'w') as handle:
            handle.write(f'#!/bin/sh\nexec {sys.executable} {os.path.join(support.ROOT, "tests", "claude_stub.py")} "$@"\n')
        os.chmod(wrapper, 0o755)
        plan = helpers.write_plan(home, helpers.node_text('a'))
        env = support.clean_environment(PANGOLOOP_RUNNER='claude-code', PANGOLOOP_CLAUDE_COMMAND=wrapper, STUB_CLAUDE='none',
                                        STUB_LOG=os.path.join(home, 'stub.log'), PANGOLOOP_RUNS=f'{home}/runs', SG_HUMAN='1')
        done = subprocess.run([sys.executable, os.path.join(support.ROOT, 'tests', 'driver_spawn.py'), plan, repo, 'r1'],
                              capture_output=True, text=True, env=env, cwd=home, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        run = f'{home}/runs/r1'
        result = helpers.read_json(f'{run}/result.json')
        self.assertEqual(result['verdict'], 'verified')
        self.assertEqual(len(result['workers']), 1, 'the node was not called again')
        self.assertTrue(result['workers'][0]['ignored_ask'].startswith('none'))
        self.assertFalse(os.path.exists(f'{run}/human') and os.listdir(f'{run}/human'), 'no request was made')
        self.assertFalse(os.path.exists(f'{run}/rulings.md'))

    def test_the_asking_paragraph_cannot_be_read_as_always_end_with_such_a_line(self):
        from pangoloop import graph
        text = ' '.join(graph.HUMAN_PARAGRAPH.split())
        self.assertIn('When you are blocked, and only then, finish your answer with one last line', text)
        self.assertIn('A node that is not blocked writes no such line at all', text)
        self.assertIn('NEEDS COORDINATOR: none', text)   # named as what not to write
        for kept in ('never look for a way around a refusal', 'BLOCKED', 'NEEDS COORDINATOR: <what and why>',
                     'NEEDS HUMAN: <what and why>', 'If you are not sure which of the two it is, ask the person.',
                     'do not work around it and do not guess'):
            self.assertIn(kept, text)


class RequestNamesWhoAsked(unittest.TestCase):
    def test_a_repair_worker_request_keeps_the_parent_node_and_names_the_worker(self):
        flow = Flow(self, answers=('keep the fix',))

        def behave(node, prompt, cwd, calls):
            if node == 'r':
                return 'VERDICT: CHANGES REQUESTED\nFINDING 1: a' if sum(c['node'] == 'r' for c in calls) == 1 else 'VERDICT: PASS'
            if node == 'r-fix1':
                return 'NEEDS COORDINATOR: keep the fix or undo it?' if 'COORDINATOR>>>' not in prompt else 'fixed'
            return None

        run, verdict = flow.play(nodes=[helpers.node_text('r', role='review', category='review')], behave=behave)
        self.assertEqual(verdict, 'verified')
        stored = helpers.read_json(f'{run.out}/human/r-1.json')
        self.assertEqual((stored['node'], stored['worker'], stored['worker_kind']), ('r', '02-r-fix1', 'repair'))
        listed = rq.load_requests(run.out)[0]
        self.assertEqual((listed['node'], listed['worker'], listed['worker_kind']), ('r', '02-r-fix1', 'repair'))
        folder_arg = run.out
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'requests', 'list', folder_arg], capture_output=True, text=True,
                              env=support.clean_environment(), cwd=support.ROOT)
        self.assertIn('02-r-fix1 (repair)', done.stdout)
        self.assertEqual(rulings.load(run.out)[0]['worker'], '02-r-fix1')

    def test_every_worker_kind(self):
        nodes = [{'id': 'b', 'role': 'build'}, {'id': 'rev', 'role': 'review'}, {'id': 'acc', 'role': 'acceptance'}]
        self.assertEqual([planmod.worker_kind(tag, nodes) for tag in
                          ('03-b', '04-b-rebase1', '05-rev', '06-rev-fix2', '07-acc-fix1', None, '08-nosuch')],
                         ['node', 'rebase-repair', 'review', 'repair', 'acceptance-repair', None, 'node'])
        self.assertEqual(planmod.WORKER_KINDS, ('node', 'repair', 'rebase-repair', 'review', 'acceptance-repair'))

    def test_old_request_files_without_the_fields_still_load(self):
        home = support.temp_dir(self)
        old = {'node': 'a', 'asked_at': '2026-11-18T10:05:00Z', 'request': 'q?', 'state': 'open'}   # no worker, no kind
        folder = support.make_run(home, requests={'a-1': old, 'a-2': dict(old, worker='05-a')})
        shown = {item['id']: item for item in rq.load_requests(folder)}
        self.assertEqual((shown['a-1']['worker'], shown['a-1']['worker_kind']), (None, None))
        self.assertEqual((shown['a-2']['worker'], shown['a-2']['worker_kind']), ('05-a', 'node'), 'derived from the plan')
        quiet(rq.cmd_list, folder)
        from pangoloop import view
        saved = view.RUNS
        view.RUNS = home
        self.addCleanup(setattr, view, 'RUNS', saved)
        page = {item['name']: item for item in view.build_run('fake-run')['human']}
        self.assertEqual((page['a-1']['worker'], page['a-1']['worker_kind']), (None, None))
        self.assertEqual(page['a-2']['worker_kind'], 'node')

    def test_the_wait_output_names_the_worker_and_its_kind(self):
        folder = support.make_run(support.temp_dir(self), requests={
            'a-1': dict(support.request_info(), worker='02-a-rebase1', worker_kind='rebase-repair')})
        code, text = (lambda out: (rq.cmd_wait(folder, 'fake-run', 0.01, out=out), out.getvalue()))(io.StringIO())
        self.assertEqual(code, 0)
        self.assertIn('worker=02-a-rebase1', text)
        self.assertIn('worker_kind=rebase-repair', text)


class PreflightWarnings(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)

    def file(self, text, name='in.md'):
        path = os.path.join(self.home, name)
        with open(path, 'w') as handle:
            handle.write(text)
        return path

    def test_a_long_line_is_reported_with_its_number_and_length(self):
        path = self.file('short\n' + 'x' * 1200 + '\n' + 'y' * 2600 + '\nend\n')
        found = preflight.check_file(path)
        self.assertEqual(len(found), 1, found)
        self.assertIn('line 3: 2600 characters', found[0])
        self.assertEqual(preflight.check_file(self.file('x' * 1200 + '\n', 'ok.md')), [], '1,200 is allowed')
        self.assertEqual(len(preflight.check_file(self.file('x' * 1201, 'long.md'))), 1)

    def test_unfilled_placeholders(self):
        found = preflight.check_file(self.file('a {{NAME}} b\nc @@TASK_TEXT@@ d\nnothing here\n'))
        self.assertEqual(len(found), 2, found)
        self.assertIn('line 1', found[0] + '')
        self.assertIn('{{NAME}}', found[0])
        self.assertIn('line 2', found[1])
        self.assertIn('@@TASK_TEXT@@', found[1])

    def test_a_plans_own_placeholders_are_the_engines_to_fill(self):
        plan = helpers.write_plan(self.home, helpers.node_text('a'))
        self.assertEqual(preflight.check_file(plan), [], 'a good plan with {{REPO}} has no warning')
        self.assertTrue(preflight.check_file(self.file('Repository `{{REPO}}`.\n')), 'in a file that is no plan it is unfilled')

    def test_a_node_field_the_parser_rejects(self):
        bad = helpers.write_plan(self.home, helpers.node_text('a', VERIFY=None), name='bad.md')
        found = preflight.check_file(bad)
        self.assertEqual(len(found), 1, found)
        self.assertIn("node a lacks ['VERIFY']", found[0])
        scripted = helpers.write_plan(self.home, helpers.script_node('s', 'frobnicate x'), name='bad2.md')
        self.assertIn('step 1 of STEPS', preflight.check_file(scripted)[0])

    def test_a_file_without_a_nodes_section_skips_the_parser_silently(self):
        self.assertEqual(preflight.check_file(self.file('# Rules\n\n- TASK: x\n')), [])
        self.assertEqual(preflight.check_file(os.path.join(self.home, 'missing.md')), [])

    def test_the_warnings_head_summary_md_and_the_exit_code_is_the_readers(self):
        path = self.file('a {{X}}\n' + 'z' * 1500 + '\n')
        runner = type('R', (), {'name': 'stub'})()
        runner.run = lambda **kwargs: RunResult(0, 'COUNT: 0\nnone\n', 'm')
        done = preflight.preflight(path, ['m@p'], f'{self.home}/out', runner=runner, table={})
        self.assertEqual(len(done['warnings']), 2)
        summary = open(f'{self.home}/out/summary.md').read().split('\n')
        self.assertTrue(summary[0].startswith('# Preflight: '))
        self.assertTrue(summary[2].startswith('## Warnings (2)'), summary[:4])
        self.assertIn('line 1', summary[4])
        self.assertIn('line 2: 1500 characters', summary[5])
        self.assertLess(open(f'{self.home}/out/summary.md').read().index('## Warnings'),
                        open(f'{self.home}/out/summary.md').read().index('## m'))
        clean = preflight.preflight(self.file('fine\n', 'fine.md'), ['m@p'], f'{self.home}/out2', runner=runner, table={})
        self.assertNotIn('Warnings', open(f'{self.home}/out2/summary.md').read())

    def test_main_prints_warnings_and_keeps_the_readers_exit_code(self):
        path = self.file('a {{X}}\n')
        runner = type('R', (), {'name': 'stub'})()
        runner.run = lambda **kwargs: RunResult(0, 'COUNT: 0\n', 'm')
        out = io.StringIO()
        with mock.patch.object(preflight, 'get_runner', return_value=runner), contextlib.redirect_stdout(out):
            code = preflight.main([path, '--out', f'{self.home}/pf', '--reader', 'm@p'])
        self.assertEqual(code, 0)
        self.assertIn('warning: line 1: placeholder left unfilled: {{X}}', out.getvalue())

    def test_strict_turns_warnings_into_exit_2_before_any_reader_runs(self):
        path = self.file('a {{X}}\n')
        calls = []
        runner = type('R', (), {'name': 'stub'})()
        runner.run = lambda **kwargs: (calls.append(kwargs), RunResult(0, 'COUNT: 0\n', 'm'))[1]
        out = io.StringIO()
        with mock.patch.object(preflight, 'get_runner', return_value=runner), contextlib.redirect_stdout(out):
            code = preflight.main([path, '--out', f'{self.home}/pf', '--strict', '--reader', 'm@p'])
            clean = preflight.main([self.file('fine\n', 'fine.md'), '--out', f'{self.home}/pf2', '--strict', '--reader', 'm@p'])
        self.assertEqual((code, len(calls)), (2, 1), 'only the clean file ran a reader')
        self.assertEqual(clean, 0, 'the readers\' exit code')
        self.assertFalse(os.path.exists(f'{self.home}/pf/summary.md'))
        self.assertIn('no reader was run', out.getvalue())


class SmallThings(unittest.TestCase):
    def run_claude(self, home, plan, **env):
        wrapper = os.path.join(home, 'claude')
        with open(wrapper, 'w') as handle:
            handle.write(f'#!/bin/sh\nexec {sys.executable} {os.path.join(support.ROOT, "tests", "claude_stub.py")} "$@"\n')
        os.chmod(wrapper, 0o755)
        repo = helpers.make_repo(home)
        environment = support.clean_environment(PANGOLOOP_RUNNER='claude-code', PANGOLOOP_CLAUDE_COMMAND=wrapper,
                                                STUB_LOG=os.path.join(home, 'stub.log'), PANGOLOOP_RUNS=f'{home}/runs', **env)
        done = subprocess.run([sys.executable, os.path.join(support.ROOT, 'tests', 'driver_spawn.py'), plan, repo, 'r1'],
                              capture_output=True, text=True, env=environment, cwd=home, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with open(os.path.join(home, 'stub.log')) as handle:
            return [json.loads(line) for line in handle], f'{home}/runs/r1'

    def test_tmpdir_points_at_the_node_scratch_folder_when_scratch_is_on(self):
        home = support.temp_dir(self)
        plan = helpers.write_plan(home, helpers.node_text('a'), helpers.node_text('b', deps='a'))
        calls, run = self.run_claude(home, plan, SG_NODE_SCRATCH='1')
        self.assertEqual(len(calls), 2)
        scratch = os.path.realpath(f'{run}/scratch')
        for call, node in zip(calls, 'ab'):
            self.assertEqual(os.path.realpath(call['tmpdir']), os.path.join(scratch, node))
            self.assertIn(f"Your scratch folder is {call['tmpdir']}", call['argv'][1])
            self.assertIn('Temporary files go there (TMPDIR points at it), never into /tmp.', call['argv'][1])

    def test_tmpdir_is_left_alone_with_scratch_off_and_with_worktrees(self):
        home = support.temp_dir(self)
        plan = helpers.write_plan(home, helpers.node_text('a'))
        calls, _ = self.run_claude(home, plan)
        self.assertNotIn('scratch', calls[0]['tmpdir'] or '')
        self.assertNotIn('never into /tmp', calls[0]['argv'][1])
        other = support.temp_dir(self)
        calls, run = self.run_claude(other, helpers.write_plan(other, helpers.node_text('a')), SG_NODE_SCRATCH='1',
                                     SG_NODE_WORKTREES='1')
        self.assertIn('/scratch/a', calls[0]['tmpdir'], 'the copy of a build node gets it too')

    def test_result_json_has_per_worker_facts_and_totals_from_the_records(self):
        home = support.temp_dir(self)
        plan = helpers.write_plan(home, helpers.node_text('a'), helpers.script_node('s', 'run `true`', deps='a'),
                                  helpers.node_text('rev', role='review', category='review', deps='s',
                                                    TASK='Review. End with VERDICT: PASS or VERDICT: CHANGES REQUESTED.'))
        _, run = self.run_claude(home, plan)
        result = helpers.read_json(f'{run}/result.json')
        workers = {w['node']: w for w in result['workers']}
        for node in ('a', 'rev'):
            worker = workers[node]
            self.assertEqual((worker['worker_kind'], worker['provider'], worker['runner']),
                             ('node' if node == 'a' else 'review', 'anthropic', 'claude-code'))
            self.assertIn('seconds', worker)
            self.assertIn('cost_usd', worker['usage'])
        totals = result['usage_totals']
        self.assertEqual((totals['workers'], totals['workers_with_usage'], totals['workers_with_cost']), (3, 2, 2))
        self.assertEqual(totals['tokens']['output'], 2 * 166)
        self.assertAlmostEqual(totals['cost_usd'], 2 * 0.00046735, places=6)

    def test_totals_without_usage_are_none_not_zero(self):
        from pangoloop import graph
        totals = graph.usage_totals([{'seconds': 4}, {'seconds': 6, 'usage': {'input': 5, 'output': 1}}])
        self.assertEqual((totals['seconds'], totals['workers_with_usage'], totals['cost_usd']), (10, 1, None))


if __name__ == '__main__':
    unittest.main()
