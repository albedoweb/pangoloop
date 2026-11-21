"""Roles in place of model names: the stop on a placeholder route, the role a worker played (what the page colours by),
and old runs that record only a model."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import support
import helpers
from pangoloop import plan


def parse(*nodes):
    with tempfile.TemporaryDirectory() as home:
        with open(helpers.write_plan(home, *nodes)) as handle:
            return plan.parse_nodes(handle.read())


class PlaceholderRoutes(unittest.TestCase):
    def test_the_built_in_routes_are_placeholders_and_a_real_one_is_not(self):
        for route in list(plan.ROUTES.values()) + [plan.DEFAULT_ROUTE]:
            self.assertTrue(plan.is_placeholder(route), route)
        self.assertFalse(plan.is_placeholder(('coder-model', 'stub-provider')))

    def test_the_routes_a_plan_needs_that_are_still_placeholders(self):
        nodes = parse(helpers.node_text('a'), helpers.node_text('r', role='review', category='review', deps='a'),
                      helpers.script_node('s', 'run `true`', deps='r'))
        self.assertEqual(plan.placeholder_routes(nodes, plan.ROUTES, plan.DEFAULT_ROUTE), ['default', 'review'])
        # the default is written, the review route is not
        self.assertEqual(plan.placeholder_routes(nodes, plan.ROUTES, ('coder-model', 'p')), ['review'])
        both = dict(plan.ROUTES, review=('reviewer-model', 'p'))
        self.assertEqual(plan.placeholder_routes(nodes, both, ('coder-model', 'p')), [])

    def test_a_script_only_plan_needs_no_model(self):
        nodes = parse(helpers.script_node('s', 'run `true`'))
        self.assertEqual(plan.placeholder_routes(nodes, plan.ROUTES, plan.DEFAULT_ROUTE), [])

    def test_a_route_a_node_names_itself_is_reported_under_its_own_name(self):
        nodes = parse(helpers.node_text('a', category='simple-work'))
        self.assertEqual(plan.placeholder_routes(nodes, dict(plan.ROUTES, **{'simple-work': ('default-model', 'p')}),
                                                 ('coder-model', 'p')), ['simple-work'])


class TheRunStopsBeforeItStarts(unittest.TestCase):
    def run_it(self, runner, *nodes, config='none'):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home)
        plan_file = helpers.write_plan(home, *nodes)
        env = support.clean_environment(PANGOLOOP_RUNNER=runner, PANGOLOOP_CONFIG=config, PANGOLOOP_RUNS=f'{home}/runs')
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'run', plan_file, repo, 'r1'], capture_output=True, text=True,
                              env=env, cwd=home, timeout=60)
        return done, home

    def test_a_placeholder_default_route_exits_2_and_names_the_route(self):
        for runner in ('hermes', 'claude-code'):
            done, home = self.run_it(runner, helpers.node_text('a'))
            self.assertEqual(done.returncode, 2, (runner, done.stdout, done.stderr))
            self.assertIn('the route "default" is still a placeholder', done.stderr)
            self.assertIn('default = "model@provider" under [routes]', done.stderr)
            self.assertFalse(os.path.exists(f'{home}/runs/r1'), 'nothing was started: no run folder')

    def test_every_missing_route_is_named(self):
        done, _ = self.run_it('hermes', helpers.node_text('a'),
                              helpers.node_text('r', role='review', category='review', deps='a'))
        self.assertEqual(done.returncode, 2)
        self.assertIn('"default", "review"', done.stderr)

    def test_a_resume_is_checked_too(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home)
        plan_file = helpers.write_plan(home, helpers.node_text('a'))
        os.makedirs(f'{home}/runs/r1')
        env = support.clean_environment(PANGOLOOP_CONFIG='none', PANGOLOOP_RUNS=f'{home}/runs')
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'run', plan_file, repo, 'r1', '--resume'],
                              capture_output=True, text=True, env=env, cwd=home, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn('placeholder', done.stderr)


class RolesOfWorkers(unittest.TestCase):
    def test_the_role_comes_from_the_category_of_the_route(self):
        routes = {'review': ('r', 'p'), 'ui': ('u', 'p'), 'simple-work': ('s', 'p')}
        for category, role in (('deep', 'coder'), ('default', 'coder'), ('review', 'reviewer'), ('ui', 'interface'),
                               ('script', 'script'), ('reader', 'reader'), ('simple-work', 'other')):
            self.assertEqual(plan.role_of(category, routes), role, category)

    def test_no_model_name_is_in_the_page_code(self):
        from pangoloop import view
        for name in ('gpt', 'claude', 'opus', 'sonnet', 'haiku', 'deepseek', 'luna', 'gemini', 'qwen'):
            self.assertNotIn(name, view.PAGE.lower() if hasattr(view, 'PAGE') else open(view.__file__).read().lower(), name)


class OldRunsStillOpen(unittest.TestCase):
    def setUp(self):
        from pangoloop import view
        self.view = view
        self.runs = support.temp_dir(self)
        saved = (view.RUNS, view.NOTIFY)
        view.RUNS, view.NOTIFY = self.runs, False
        view.GRADE_CACHE.clear()
        view.INDEX_CACHE.clear()
        self.addCleanup(lambda: (setattr(view, 'RUNS', saved[0]), setattr(view, 'NOTIFY', saved[1])))

    def make(self, records):
        folder = support.make_run(self.runs, 'r1', over=True)
        for record in records:
            os.makedirs(os.path.join(folder, 'workers', record['worker']))
            with open(os.path.join(folder, 'workers', record['worker'], 'prompt.txt'), 'w') as handle:
                handle.write('p')
        with open(os.path.join(folder, 'result.json'), 'w') as handle:
            json.dump({'verdict': 'verified', 'nodes': {'a': 'accepted'}, 'workers': records}, handle)
        return self.view.build_run('r1')

    def test_a_worker_with_a_recorded_category_gets_the_role_of_it(self):
        data = self.make([{'worker': '01-a', 'node': 'a', 'category': 'review', 'model': 'some-model', 'ok': True, 'exit': 0}])
        worker = data['workers'][0]
        self.assertEqual((worker['model'], worker['role']), ('some-model', 'reviewer'), 'the model is shown as text')
        self.assertEqual(data['nodes'][0]['color_role'], 'reviewer')
        self.assertEqual(data['by_model']['some-model']['role'], 'reviewer')

    def test_a_run_that_recorded_no_category_is_coloured_by_the_category_of_its_node(self):
        data = self.make([{'worker': '01-a', 'node': 'a', 'model': 'old-model-name', 'ok': True, 'exit': 0}])
        self.assertEqual((data['workers'][0]['model'], data['workers'][0]['role']), ('old-model-name', 'coder'))

    def test_one_model_in_two_roles_has_the_neutral_colour(self):
        data = self.make([{'worker': '01-a', 'node': 'a', 'category': 'deep', 'model': 'same', 'ok': True, 'exit': 0},
                          {'worker': '02-a', 'node': 'a', 'category': 'review', 'model': 'same', 'ok': True, 'exit': 0}])
        self.assertEqual(data['by_model']['same']['role'], 'other')

    def test_the_page_colours_by_role(self):
        page = self.view.PAGE if hasattr(self.view, 'PAGE') else ''
        self.assertIn('const roleColor', page)
        self.assertNotIn('fixedColor', page)


if __name__ == '__main__':
    unittest.main()
