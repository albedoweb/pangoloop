"""Fault 2 (November 20, 2026): with node worktrees, a build node whose write_scope is not `nothing` and whose work is an
empty change is not done: it fails with the reason `no change`, which result.json, state.json and the page show.
`may_be_empty: yes` allows it. Stubs stand in for the model."""
import json
import os
import unittest

import engine_stub
import helpers
import support
from pangoloop import plan, view

WORKTREES = {'SG_NODE_WORKTREES': '1'}


def write(cwd, name='a.txt', text='x'):
    with open(os.path.join(cwd, name), 'w') as handle:
        handle.write(text)


class EmptyChange(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.repo = helpers.make_repo(self.home)

    def run_plan(self, *nodes, env=WORKTREES, behave=None):
        sg = engine_stub.engine(self, self.home, **env)
        cls = engine_stub.stub_class(sg, behave)
        run = cls(helpers.write_plan(self.home, *nodes), self.repo, 'r')
        sg.CURRENT = run
        verdict = run.execute()
        sg.write_result(run, verdict, run.status)
        return run, verdict, cls.calls

    def result(self):
        return helpers.read_json(f'{self.home}/runs/r/result.json')

    def test_a_build_node_that_changed_nothing_fails(self):
        run, verdict, calls = self.run_plan(helpers.node_text('a'), helpers.node_text('b', deps='a'),
                                            behave=lambda node, prompt, cwd, calls: None)
        self.assertEqual(verdict, 'not_verified')
        self.assertEqual(run.status, {'a': 'failed', 'b': 'waiting'})
        self.assertEqual([c['node'] for c in calls], ['a'], 'one worker: an empty change is not retried')
        self.assertEqual(self.result()['node_reasons'], {'a': 'no change'})
        state = helpers.read_json(f'{self.home}/runs/r/state.json')
        self.assertEqual(state['nodes']['a']['status'], 'failed')
        self.assertEqual(state['nodes']['a']['reason'], 'no change')
        self.assertTrue(os.path.isdir(f'{self.home}/runs/r/wt/a'), 'its copy stays for a look')

    def test_the_page_shows_the_reason(self):
        self.run_plan(helpers.node_text('a'), behave=lambda node, prompt, cwd, calls: None)
        saved = view.RUNS
        view.RUNS = f'{self.home}/runs'
        self.addCleanup(setattr, view, 'RUNS', saved)
        node = view.build_run('r')['nodes'][0]
        self.assertEqual((node['state'], node['state_text']), ('failed', 'no change'))

    def test_a_change_is_accepted(self):
        run, verdict, _ = self.run_plan(helpers.node_text('a'), behave=lambda node, prompt, cwd, calls: write(cwd))
        self.assertEqual((verdict, run.status), ('verified', {'a': 'accepted'}))
        self.assertNotIn('node_reasons', self.result())

    def test_may_be_empty_yes_allows_it(self):
        run, verdict, _ = self.run_plan(helpers.node_text('a', may_be_empty='yes'))
        self.assertEqual((verdict, run.status), ('verified', {'a': 'accepted'}))

    def test_a_node_that_writes_nothing_is_not_asked_for_a_change(self):
        for scope in ('nothing', 'none', 'Nothing.'):
            with self.subTest(scope=scope):
                self.home = support.temp_dir(self)
                self.repo = helpers.make_repo(self.home)
                run, verdict, _ = self.run_plan(helpers.node_text('a', write_scope=scope))
                self.assertEqual(verdict, 'verified')

    def test_other_roles_are_not_affected(self):
        run, verdict, _ = self.run_plan(helpers.node_text('base', role='baseline'),
                                        helpers.script_node('s', 'run `true`', deps='base'),
                                        helpers.node_text('rev', role='review', deps='s', write_scope='x'),
                                        behave=lambda node, prompt, cwd, calls: 'VERDICT: PASS' if node == 'rev' else None)
        self.assertEqual(verdict, 'verified', run.status)

    def test_without_node_worktrees_it_is_as_before(self):
        run, verdict, _ = self.run_plan(helpers.node_text('a'), env={})
        self.assertEqual((verdict, run.status), ('verified', {'a': 'accepted'}))

    def test_a_resume_runs_the_node_again_and_clears_the_reason(self):
        self.run_plan(helpers.node_text('a'), behave=lambda node, prompt, cwd, calls: None)
        sg = engine_stub.engine(self, self.home, **WORKTREES)
        cls = engine_stub.stub_class(sg, lambda node, prompt, cwd, calls: write(cwd))
        run = cls(helpers.write_plan(self.home, helpers.node_text('a')), self.repo, 'r', {'reset_rounds': False,
                                                                                       'accept_plan_changes': False})
        sg.CURRENT = run
        self.assertEqual(run.execute(), 'verified')
        sg.write_result(run, 'verified', run.status)
        self.assertNotIn('node_reasons', self.result())

    def test_the_field_is_yes_or_no(self):
        text = open(helpers.write_plan(self.home, helpers.node_text('a', may_be_empty='maybe'))).read()
        with self.assertRaises(SystemExit) as stop:
            plan.parse_nodes(text)
        self.assertIn('may_be_empty', str(stop.exception.code))
        self.assertEqual(plan.parse_nodes(text.replace('maybe', 'yes'))[0]['may_be_empty'], 'yes')
        self.assertTrue(plan.must_change({'role': 'build', 'write_scope': 'x'}))
        self.assertFalse(plan.must_change({'role': 'build', 'write_scope': 'x', 'may_be_empty': 'yes'}))
        self.assertFalse(plan.must_change({'role': 'script', 'write_scope': 'x'}))


if __name__ == '__main__':
    unittest.main()
