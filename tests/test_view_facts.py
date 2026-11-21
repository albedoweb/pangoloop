"""The viewer's three facts about a run (run, grade, regrade) and the line of what its acceptance counted."""
import json
import os
import shutil
import threading
import unittest
import urllib.request

import support

FIXTURES = os.path.join(support.ROOT, 'tests', 'fixtures', 'view_acceptance')

# Output ends in the shape real runs print (the numbers are invented).
VITEST_FAILED = ' Test Files  1 failed | 5 passed (6)\n      Tests  1 failed | 78 passed (79)\n   Start at  15:27:50\n'
VITEST_OK = ' Test Files  128 passed (128)\n      Tests  1299 passed (1299)\n   Start at  15:27:50\n'
PLAYWRIGHT_OK = '[26/26] [chromium] › tests/golden/level.spec.ts:121:1 › level l\n  26 passed (1.2m)\n'


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return json.load(handle)


class Patterns(unittest.TestCase):
    def test_vitest(self):
        from pangoloop import view
        self.assertEqual(view.parse_vitest(VITEST_OK), {'passed': 1299, 'failed': 0})
        self.assertEqual(view.parse_vitest(VITEST_FAILED), {'passed': 78, 'failed': 1})
        self.assertIsNone(view.parse_vitest(PLAYWRIGHT_OK))
        self.assertIsNone(view.parse_vitest(None))

    def test_playwright(self):
        from pangoloop import view
        self.assertEqual(view.parse_playwright(PLAYWRIGHT_OK), {'passed': 26, 'failed': 0, 'flaky': 0})
        tail = fixture('acceptance-final-0.json')['failures'][0][2]
        self.assertEqual(view.parse_playwright(tail), {'passed': 12, 'failed': 1, 'flaky': 0})
        self.assertEqual(view.parse_playwright('  2 flaky\n  9 passed (1m)\n'), {'passed': 9, 'failed': 0, 'flaky': 2})
        self.assertIsNone(view.parse_playwright('nothing counted here'))

    def test_generic(self):
        from pangoloop import view
        self.assertEqual(view.parse_generic('ran 5 passed, 2 failed'), {'passed': 5, 'failed': 2})
        self.assertIsNone(view.parse_generic('all fine'))

    def test_labels_are_derived_from_the_command(self):
        from pangoloop import view
        for command, expected in (('npm test', ('tests', 'runner')), ('npx playwright test tests/smoke', ('smoke', 'runner')),
                                  ('npx playwright test -c playwright.golden.config.ts', ('golden', 'runner')),
                                  ('npx playwright test', ('browser', 'runner')), ('npm run lint', ('lint', 'check')),
                                  ('node scripts/check-boundaries.mjs', ('check-boundaries.mjs', 'check')),
                                  ('python3 -m unittest discover', ('tests', 'runner'))):
            self.assertEqual(view.command_label(command), expected, command)


class Facts(unittest.TestCase):
    def setUp(self):
        from pangoloop import view
        self.view = view
        self.runs = support.temp_dir(self)
        self.reports = support.temp_dir(self)
        saved = (view.RUNS, view.REPORTS, view.NOTIFY)
        view.RUNS, view.REPORTS, view.NOTIFY = self.runs, self.reports, False
        view.GRADE_CACHE.clear()
        view.INDEX_CACHE.clear()
        self.addCleanup(lambda: (setattr(view, 'RUNS', saved[0]), setattr(view, 'REPORTS', saved[1]), setattr(view, 'NOTIFY', saved[2])))

    def make(self, name='t9-script-p1-1', graded=None, hidden='3 failed | 83 passed (86)'):
        """A finished verified run; `graded` is True/False to write a results.jsonl row and a grade record."""
        support.make_run(self.runs, name, over=True)
        if graded is not None:
            task_dir = os.path.join(self.reports, 'T9')
            os.makedirs(os.path.join(task_dir, name, 'grade'), exist_ok=True)
            with open(os.path.join(task_dir, 'results.jsonl'), 'a') as handle:
                handle.write(json.dumps({'task': 'T9', 'name': name, 'code_grader_passed': graded,
                                         'failed_checks': [] if graded else ['hidden tests']}) + '\n')
            with open(os.path.join(task_dir, name, 'grade', 'grade.json'), 'w') as handle:
                json.dump({'task': 'T9', 'checks': [{'check': 'command tests', 'ok': True, 'detail': 'exit 0 9 passed (9)'},
                                                    {'check': 'hidden tests', 'ok': graded, 'detail': 'exit 1 in 4.4s ' + hidden}]}, handle)
        return name

    def regrade(self, name, **files):
        folder = os.path.join(self.reports, 'T9', name, files.pop('folder', 'regrade-2026-11-18'))
        os.makedirs(folder, exist_ok=True)
        for fname, text in files.items():
            with open(os.path.join(folder, fname), 'w') as handle:
                handle.write(text)

    def facts(self, name):
        listed = [r for r in self.view.list_runs() if r['name'] == name][0]['facts']
        built = self.view.build_run(name)['facts']
        self.assertEqual(listed['label'], built['label'])
        return built

    def test_verified_without_a_grade_shows_only_the_run(self):
        f = self.facts(self.make())
        self.assertEqual(f['label'], 'run verified')
        self.assertIsNone(f['grade'])
        self.assertIsNone(f['regrade'])
        self.assertFalse(f['judged'])

    def test_failed_grade_and_no_regrade_names_the_grader(self):
        f = self.facts(self.make(graded=False))
        self.assertEqual(f['label'], 'run verified · grader failed 83/86 hidden (its own run: 9 tests)')
        self.assertEqual(f['grade'], {'result': 'failed', 'tests': '83/86', 'failed_checks': ['hidden tests'],
                                      'commands': [{'label': 'tests', 'passed': 9, 'total': 9}]})
        self.assertEqual(f['tone'], 'bad')
        self.assertEqual(f['run'], 'verified')

    def test_failed_grade_without_a_count_names_the_failed_check(self):
        f = self.facts(self.make(graded=False, hidden='exit 1'))
        self.assertEqual(f['label'], 'run verified · grader failed: hidden tests (its own run: 9 tests)')

    def test_passing_regrade_wins_the_badge(self):
        name = self.make(graded=False)
        summary = 'x' * 300
        self.regrade(name, **{'regrade.json': json.dumps({'date': '2026-11-18', 'passed': True, 'hidden_tests': '86/86',
                                                          'original': '83/86', 'summary': summary})})
        f = self.facts(name)
        self.assertEqual(f['label'], 'run verified · grader failed 83/86 hidden (its own run: 9 tests) · regraded 86/86 passed')
        self.assertEqual(f['tone'], 'ok')
        self.assertEqual(f['regrade']['summary'], summary)
        self.assertEqual(f['regrade']['date'], '2026-11-18')

    def test_newest_regrade_folder_wins(self):
        name = self.make(graded=False)
        self.regrade(name, folder='regrade-2026-11-18', **{'regrade.json': json.dumps({'passed': True, 'hidden_tests': '86/86'})})
        self.regrade(name, folder='regrade-2026-11-20', **{'regrade.json': json.dumps({'passed': False, 'hidden_tests': '80/86'})})
        self.assertTrue(self.facts(name)['label'].endswith('regraded 80/86 failed'))
        self.assertEqual(self.facts(name)['tone'], 'bad')

    def test_only_regrade_md(self):
        name = self.make(graded=False)
        self.regrade(name, **{'REGRADE.md': '# Regrade\n\nWritten by a person; two tests were wrong.\n'})
        f = self.facts(name)
        self.assertEqual(f['label'], 'run verified · grader failed 83/86 hidden (its own run: 9 tests) · regraded: see REGRADE.md')
        self.assertEqual(f['regrade']['summary'], 'Written by a person; two tests were wrong.')
        self.assertEqual(f['tone'], 'warn')

    def test_unreadable_or_malformed_regrade_json(self):
        for i, text in enumerate(('{not json', '[1, 2]', '{"passed": "yes"}')):
            name = self.make(f't9-bad-{i}', graded=False)
            self.regrade(name, **{'regrade.json': text})
            f = self.facts(name)
            self.assertEqual(f['label'], 'run verified · grader failed 83/86 hidden (its own run: 9 tests) · regrade record unreadable', text)
            self.assertEqual(f['regrade']['status'], 'unreadable')

    def test_missing_reports_folder_is_no_grade(self):
        self.view.REPORTS = os.path.join(self.reports, 'nonesuch')
        f = self.facts(self.make())
        self.assertEqual(f['label'], 'run verified')

    def test_no_reports_folder_set_is_no_grade_and_reads_nothing_from_the_current_folder(self):
        name = self.make(graded=False)
        self.view.REPORTS = ''
        here = os.getcwd()
        os.chdir(self.reports)   # the folder holds T9/results.jsonl: an empty setting must not make it relative
        try:
            f = self.facts(name)
        finally:
            os.chdir(here)
        self.assertEqual(f['label'], 'run verified')
        self.assertIsNone(self.view.hidden_count('T9', name))
        self.assertEqual(self.view.grader_commands('T9', name), [])
        self.assertIsNone(self.view.find_regrade('T9', name))
        self.assertEqual(self.view.results_index(), {})

    def test_default_reports_folder_is_unset(self):
        import subprocess, sys
        env = {k: v for k, v in os.environ.items() if k != 'RUN_VIEW_REPORTS'}
        out = subprocess.run([sys.executable, '-c', 'from pangoloop import view; print(repr(view.REPORTS))'],
                             cwd=support.ROOT, env=env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "''")

    def test_acceptance_two_rounds_review_returns_and_a_failed_command(self):
        name = self.make()
        for fname in ('acceptance-final-0.json', 'acceptance-final-1.json'):
            shutil.copy(os.path.join(FIXTURES, fname), os.path.join(self.runs, name, fname.replace('final-', 'acceptance-final-')))
        with open(os.path.join(self.runs, name, 'result.json'), 'w') as handle:
            json.dump({'verdict': 'verified', 'nodes': {}, 'workers': [], 'review_verdicts': ['CHANGES REQUESTED', 'CHANGES REQUESTED', 'PASS']}, handle)
        tests = self.facts(name)['tests']
        self.assertEqual(tests['text'], 'tests ✓ · smoke ✓ · golden ✓ · 7 checks ✓ · 2nd round · review: 2 returns, then pass')
        self.assertEqual(tests['rounds'], 2)
        self.assertTrue(tests['ok'])
        # the first round alone: the failed command first, with its counts from the stored output end
        os.remove(os.path.join(self.runs, name, 'acceptance-acceptance-final-1.json'))
        first = self.view.acceptance_summary(os.path.join(self.runs, name), ['PASS'])
        self.assertEqual(first['text'], 'smoke ✗ 1 failed, 12 passed · tests ✓ · golden ✓ · 7 checks ✓ · review: pass')
        self.assertFalse(first['ok'])

    def test_nothing_stored_shows_nothing(self):
        self.assertIsNone(self.facts(self.make())['tests'])

    def test_counts_when_the_output_is_stored_for_a_passing_command(self):
        name = self.make()
        entry = {'entry': {'node': 'acc', 'round': 0, 'commands': [{'command': 'npm test', 'exit': 0, 'seconds': 1, 'tail': '\x1b[1m' + VITEST_OK},
                                                                   {'command': 'npx playwright test -c playwright.golden.config.ts', 'exit': 0, 'seconds': 1, 'output': PLAYWRIGHT_OK},
                                                                   {'command': 'npm run lint', 'exit': 0, 'seconds': 1}]}, 'failures': []}
        with open(os.path.join(self.runs, name, 'acceptance-acc-0.json'), 'w') as handle:
            json.dump(entry, handle)
        self.assertEqual(self.facts(name)['tests']['text'], 'tests 1,299 ✓ · golden 26 ✓ · 1 check ✓')

    def test_a_failed_command_without_a_count(self):
        name = self.make()
        entry = {'entry': {'node': 'acc', 'round': 0, 'commands': [{'command': 'npm run build', 'exit': 2, 'seconds': 1},
                                                                   {'command': 'npm run lint', 'exit': 0, 'seconds': 1}]}, 'failures': [['npm run build', 2, 'boom']]}
        with open(os.path.join(self.runs, name, 'acceptance-acc-0.json'), 'w') as handle:
            json.dump(entry, handle)
        self.assertEqual(self.facts(name)['tests']['text'], 'build ✗ exit 2 · 1 check ✓')

    def test_api_fields(self):
        name = self.make(graded=False)
        self.regrade(name, **{'regrade.json': json.dumps({'passed': True, 'hidden_tests': '86/86', 'date': '2026-11-18', 'summary': 'fixed'})})
        server = self.view.Server(('127.0.0.1', 0), self.view.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        listed = json.load(urllib.request.urlopen(base + '/api/runs'))['runs'][0]['facts']
        shown = json.load(urllib.request.urlopen(base + '/api/run/' + name))['facts']
        for facts in (listed, shown):
            self.assertEqual(facts['run'], 'verified')
            self.assertEqual(facts['grade']['result'], 'failed')
            self.assertEqual(facts['grade']['tests'], '83/86')
            self.assertEqual(facts['regrade']['status'], 'passed')
            self.assertEqual(facts['regrade']['tests'], '86/86')
            self.assertEqual(facts['regrade']['summary'], 'fixed')
            self.assertEqual(facts['label'], 'run verified · grader failed 83/86 hidden (its own run: 9 tests) · regraded 86/86 passed')


class Engine(unittest.TestCase):
    def test_every_acceptance_command_stores_the_end_of_its_output(self):
        import engine_stub
        import helpers
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files={'a.txt': '1'})
        sg = engine_stub.engine(self, home)
        big = "python3 -c \"print('x' * 5000); print('      Tests  1299 passed (1299)')\""
        path = helpers.write_plan(home, helpers.node_text('a'),
                                  helpers.node_text('acc', 'acceptance', 'a', VERIFY=f"run `{big}` and `echo fine` and `false`"))
        cls = engine_stub.stub_class(sg, None)
        run = cls(path, repo, 'tails')
        sg.CURRENT = run
        run.execute()
        with open(os.path.join(run.out, 'acceptance-acc-0.json')) as handle:
            record = json.load(handle)
        commands = record['entry']['commands']
        self.assertEqual([c['exit'] for c in commands], [0, 0, 1])
        self.assertTrue(commands[0]['tail'].endswith('Tests  1299 passed (1299)\n'))
        self.assertEqual(len(commands[0]['tail']), 2000)
        self.assertEqual(commands[1]['tail'], 'fine\n')
        self.assertEqual(record['failures'][0][0], 'false')   # the repair prompts keep reading `failures`
        from pangoloop import view
        self.assertEqual(view.command_counts(commands[0]['command'], commands[0]['tail']), {'passed': 1299, 'failed': 0})
        self.assertLess(len(json.dumps(record['entry'])), 20000)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
