"""The page for someone who is not the project it was built for: no `task ?`, nothing about a grader unless a reports folder is
set, a worker coloured and named by the part it played, a cost line that says there is no price table."""
import json
import os
import shutil
import unittest

import project_helpers as h
import support
from test_runs_and_pages import start_view

from pangoloop import view

DEMO_PLAN = os.path.join(support.ROOT, 'examples', 'demo', 'plan.md')
TOY_PLAN = os.path.join(support.ROOT, 'examples', 'toy', 'plan.md')


def run_plan(test, plan_path: str, **extra):
    """A project with a plan copied from the examples, run once with the stand-in; (folder, env, the page's address, the run)."""
    folder, env = h.make_project(test)
    env = dict(env, STUB_NO_COST='1', **extra)
    shutil.copy(plan_path, os.path.join(folder, '.pangoloop', 'plans', 'p.md'))
    done = h.pangoloop('run', 'p', '--name', 'r1', cwd=folder, env=env, timeout=180)
    test.assertEqual(done.returncode, 0, done.stdout + done.stderr)
    process, line = start_view(test, folder, env)
    data = json.loads(h.fetch(line + 'api/run/r1')[1])
    return folder, env, line, data


class Colours(unittest.TestCase):
    def test_the_class_of_a_worker_is_the_part_it_played(self):
        for category, kind, expected in (('default', 'build', 'coder'), ('simple-work', 'build', 'coder'), ('mine', 'baseline', 'coder'),
                                         ('deep', None, 'coder'), ('review', 'review', 'reviewer'), ('default', 'review', 'reviewer'),
                                         ('ui', 'build', 'interface'), ('default', 'script', 'script'), ('script', None, 'script'),
                                         ('reader', None, 'reader')):
            self.assertEqual(view.route_role(category, kind), expected, (category, kind))
        self.assertNotIn('other', {view.route_role(c, k) for c in ('a', 'b', 'simple-work') for k in ('build', 'baseline', None)})

    def test_a_run_of_the_demo_plan_reads_coder_test_writer_script_reviewer(self):
        _, _, line, data = run_plan(self, DEMO_PLAN)
        workers = {w['node']: w for w in data['workers']}
        self.assertEqual([(workers[n]['role'], workers[n]['role_label']) for n in ('parser', 'tests', 'check', 'review')],
                         [('coder', 'coder'), ('coder', 'test-writer'), ('script', 'script'), ('reviewer', 'reviewer')])
        nodes = {n['id']: n for n in data['nodes']}
        self.assertEqual([nodes[n]['color_role'] for n in ('parser', 'tests', 'check', 'accept', 'review')],
                         ['coder', 'coder', 'script', 'coder', 'reviewer'])
        self.assertEqual(data['by_model']['small-model-5-5']['role'], 'other', 'one model in several roles has no one colour (the page calls it several roles)')

    def test_a_plan_of_the_old_form_with_a_category_of_its_own_is_not_other(self):
        _, _, _, data = run_plan(self, TOY_PLAN)
        by_node = {w['node']: w for w in data['workers']}
        self.assertEqual(by_node['add-double']['role'], 'coder', 'the toy plan says category simple-work, which the preset routes')
        self.assertEqual(by_node['add-double']['role_label'], 'build', 'no role name in a plan of the old form: the kind')
        self.assertNotIn('other', {w['role'] for w in data['workers']})

    def test_the_legend_is_made_of_role_names(self):
        _, _, line, _ = run_plan(self, DEMO_PLAN)
        page = h.fetch(line)[1]
        self.assertIn('rname:w.role_label', page)
        self.assertIn('roles.map(([label, r])', page)


class NoLeftovers(unittest.TestCase):
    def test_no_task_line_and_no_grader_words_in_a_plain_run(self):
        _, _, line, data = run_plan(self, DEMO_PLAN)
        self.assertIsNone(data['task'])
        self.assertIs(data['reports'], False)
        page = h.fetch(line)[1]
        self.assertNotIn("task ${d.task||'?'}", page)
        self.assertIn("d.task ? 'task ' + d.task : ''", page)
        # the words about grading are inside conditions on a reports folder, not in what a plain run says
        self.assertIn("d.reports ? 'model calls are not recorded until graded; tokens are of finished workers'", page)
        self.assertIn("d.reports ? `<td class=\"n faint\">model calls n/a</td>`", page)
        self.assertIn("(data.reports ? 'model calls (until graded), ' : '')", page)
        self.assertIsNone(data['facts']['grade'])
        self.assertIsNone(data['facts']['regrade'])
        self.assertEqual(data['facts']['run'], 'verified')
        self.assertIn('tests', data['facts']['tests']['text'])
        self.assertEqual(data['review_verdicts'], ['PASS'])

    def test_with_a_reports_folder_the_page_says_so(self):
        folder = support.temp_dir(self)
        old = view.REPORTS
        view.REPORTS = folder
        self.addCleanup(setattr, view, 'REPORTS', old)
        _, _, _, data = run_plan(self, DEMO_PLAN, RUN_VIEW_REPORTS=folder)
        self.assertIs(data['reports'], True)


class Cost(unittest.TestCase):
    def test_a_stand_in_has_no_cost_and_the_page_says_no_price_table(self):
        _, _, line, data = run_plan(self, DEMO_PLAN)
        self.assertIsNone(data['cost']['usd'])
        self.assertIs(data['has_prices'], False)
        page = h.fetch(line)[1]
        self.assertIn("noTable ? 'no price table' : 'unknown'", page)

    def test_with_prices_in_the_configuration_the_page_knows_it(self):
        folder, env = h.make_project(self)
        with open(os.path.join(folder, 'pangoloop.toml'), 'a') as handle:
            handle.write('\n[prices]\n"small-model" = { input = 1.0, output = 5.0, cache_read = 0.1, cache_write = 1.25 }\n')
        shutil.copy(DEMO_PLAN, os.path.join(folder, '.pangoloop', 'plans', 'p.md'))
        env = dict(env, STUB_NO_COST='1')
        self.assertEqual(h.pangoloop('run', 'p', '--name', 'r1', cwd=folder, env=env, timeout=180).returncode, 0)
        process, line = start_view(self, folder, env)
        data = json.loads(h.fetch(line + 'api/run/r1')[1])
        self.assertIs(data['has_prices'], True)


class Stub(unittest.TestCase):
    def test_the_stand_in_can_leave_out_the_cost_and_report_the_model_it_was_asked_for(self):
        import subprocess
        import sys
        env = dict(support.clean_environment(), STUB_LOG=os.path.join(support.temp_dir(self), 'log'), STUB_NO_COST='1',
                   STUB_ECHO_MODEL='1')
        done = subprocess.run([sys.executable, os.path.join(support.ROOT, 'tests', 'claude_stub.py'), '-p', 'x', '--model', 'haiku'],
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0)
        self.assertNotIn('total_cost_usd', done.stdout)
        self.assertNotIn('costUSD', done.stdout)
        self.assertIn('"haiku"', done.stdout)
        self.assertNotIn('small-model-5-5', done.stdout)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
