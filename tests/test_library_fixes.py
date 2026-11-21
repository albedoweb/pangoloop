"""Step 2, part C: four oddities of the first step fixed. One default runs folder for engine, viewer and requests; the
viewer reads the plan format from the engine's module; `pangoloop run` has meaningful exit codes; Ctrl-C stops a run as
cleanly as SIGTERM."""
import os
import signal
import subprocess
import sys
import time
import unittest

import helpers
import support


def probe(code: str, cwd: str, **env) -> str:
    done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=cwd,
                          env=support.clean_environment(**env))
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


SHOW = ('from pangoloop import graph, view, requests as rq, paths; '
        'print(graph.RUNS); print(view.RUNS); print(rq.runs_folder()); print(paths.runs_folder())')


class RunsFolder(unittest.TestCase):
    def test_default_is_under_the_current_folder_for_all_three(self):
        home = support.temp_dir(self)
        lines = probe(SHOW, home).splitlines()
        self.assertEqual(lines, [f'{home}/.pangoloop/runs'] * 4)

    def test_pangoloop_runs_overrides_and_viewer_variable_wins_for_the_viewer(self):
        home = support.temp_dir(self)
        lines = probe(SHOW, home, PANGOLOOP_RUNS=f'{home}/a').splitlines()
        self.assertEqual(lines, [f'{home}/a'] * 4)
        lines = probe(SHOW, home, PANGOLOOP_RUNS=f'{home}/a', RUN_VIEW_RUNS=f'{home}/b').splitlines()
        self.assertEqual(lines, [f'{home}/a', f'{home}/b', f'{home}/a', f'{home}/a'])

    def test_a_relative_override_is_made_absolute(self):
        home = support.temp_dir(self)
        self.assertEqual(probe(SHOW, home, PANGOLOOP_RUNS='rel/x').splitlines()[0], f'{home}/rel/x')

    def test_a_run_creates_the_default_folder_on_demand_and_the_readers_cope_without_it(self):
        home = support.temp_dir(self)
        out = probe('from pangoloop import view, requests as rq; print(view.list_runs()); '
                    'print(rq.run_is_over(rq.run_folder("nonesuch")))', home)
        self.assertEqual(out, '[]\nFalse')
        self.assertFalse(os.path.exists(f'{home}/.pangoloop'))
        repo = helpers.make_repo(home)
        plan = helpers.write_plan(home, helpers.node_text('a'))
        done = helpers.drive([plan, repo, 'r1'], cwd=home)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(os.path.isfile(f'{home}/.pangoloop/runs/r1/result.json'))


class SharedPlanModule(unittest.TestCase):
    def test_engine_and_viewer_use_the_same_objects(self):
        from pangoloop import graph, plan, view
        for name in ('FIELDS', 'FINDING_LINE', 'VERDICT', 'ASK_LINE', 'parse_nodes'):
            self.assertIs(getattr(graph, name), getattr(plan, name), name)
        for name in ('FINDING_LINE', 'VERDICT', 'ASK_LINE', 'parse_nodes'):
            self.assertIs(getattr(view, name), getattr(plan, name), name)
        # the routes come from the configuration (the suite's routes file names stand-in models): both read the same one
        self.assertEqual((graph.ROUTES, graph.DEFAULT_ROUTE), (view.ROUTES, view.DEFAULT_ROUTE))
        self.assertEqual(view.route_model('review'), 'reviewer-model')
        self.assertEqual(view.route_model('anything'), 'coder-model')

    def test_engine_default_fix_rounds_is_the_shared_default(self):
        out = probe('from pangoloop import graph, plan; print(graph.MAX_FIX_ROUNDS, plan.DEFAULT_MAX_FIX_ROUNDS)',
                    support.ROOT)
        self.assertEqual(out, '2 2')

    def test_strict_and_tolerant_parsing(self):
        from pangoloop import plan
        text = helpers.write_plan(support.temp_dir(self), helpers.node_text('a', TASK=None))
        body = open(text).read()
        with self.assertRaises(SystemExit) as stop:
            plan.parse_nodes(body)
        self.assertIn("node a lacks ['TASK']", stop.exception.code)
        nodes = plan.parse_nodes(body, strict=False)
        self.assertEqual((nodes[0]['id'], nodes[0]['TASK']), ('a', ''))
        self.assertEqual(plan.parse_nodes('no nodes here', strict=False), [])
        with self.assertRaises(SystemExit):
            plan.parse_nodes('no nodes here')

    def test_viewer_judges_fix_rounds_by_the_engines_default_when_a_run_has_no_result(self):
        """Two fix rounds are all the engine allows: with three review answers asking for changes and two fix workers
        and no result.json yet, the review node has failed (the old assumption of 3 rounds said 'repairing')."""
        from pangoloop import view
        home = support.temp_dir(self)
        folder = support.make_run(home, 'rv')
        with open(f'{folder}/plan.md', 'w') as handle:
            handle.write(open(helpers.write_plan(home, helpers.node_text('rev', role='review', category='review'))).read())
        for tag, text in (('01-rev', 'VERDICT: CHANGES REQUESTED'), ('02-rev-fix1', 'fixed'),
                          ('03-rev', 'VERDICT: CHANGES REQUESTED'), ('04-rev-fix2', 'fixed'),
                          ('05-rev', 'VERDICT: CHANGES REQUESTED')):
            os.makedirs(f'{folder}/workers/{tag}')
            with open(f'{folder}/workers/{tag}/answer.md', 'w') as handle:
                handle.write(text)
        saved = view.RUNS
        view.RUNS = home
        self.addCleanup(setattr, view, 'RUNS', saved)
        node = view.build_run('rv')['nodes'][0]
        self.assertEqual((node['state'], node['state_text']), ('failed', 'changes still requested'))


class ExitCodes(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.repo = helpers.make_repo(self.home)

    def run_plan(self, *nodes, mode='ok', env=None, name='r'):
        plan = helpers.write_plan(self.home, *nodes)
        return helpers.drive([plan, self.repo, name], mode=mode, cwd=self.home, env=env)

    def test_verified_is_0_and_the_printed_line_is_unchanged(self):
        done = self.run_plan(helpers.node_text('a'), helpers.node_text('b', deps='a'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines()[-1], "verdict=verified reviews=[] nodes={'a': 'accepted', 'b': 'accepted'}")

    def test_a_failed_node_is_1(self):
        done = self.run_plan(helpers.node_text('a'), mode='fail')
        self.assertEqual(done.returncode, 1)
        self.assertIn('verdict=not_verified', done.stdout)

    def test_a_review_that_does_not_pass_is_1(self):
        done = self.run_plan(helpers.node_text('a'), helpers.node_text('rev', 'review', 'a', 'review'), mode='changes')
        self.assertEqual(done.returncode, 1)
        self.assertIn("reviews=['CHANGES REQUESTED'", done.stdout)

    def test_a_failed_acceptance_is_1(self):
        done = self.run_plan(helpers.node_text('a'),
                             helpers.node_text('acc', 'acceptance', 'a', VERIFY='run `false`'))
        self.assertEqual(done.returncode, 1)
        self.assertIn("'acc': 'failed'", done.stdout)

    def test_a_person_s_abort_is_3(self):
        done = self.run_plan(helpers.node_text('a'), mode='ask', env={'SG_HUMAN': '1'})
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn('verdict=stopped_by_human', done.stdout)

    def test_plan_and_configuration_errors_are_2_and_nothing_ran(self):
        runs = f'{self.home}/.pangoloop/runs'
        for label, plan in (('a missing field', helpers.write_plan(self.home, helpers.node_text('a', VERIFY=None))),
                            ('an unknown dependency', helpers.write_plan(self.home, helpers.node_text('a', deps='zz'))),
                            ('no Nodes section', self.write_raw('# Plan\n\nnothing\n'))):
            done = helpers.drive([plan, self.repo, 'bad'], cwd=self.home)
            self.assertEqual(done.returncode, 2, (label, done.stderr))
            self.assertTrue(done.stderr.strip(), label)
            self.assertFalse(os.path.exists(f'{runs}/bad'), f'{label}: the run folder must not exist')
        done = helpers.drive([f'{self.home}/nonesuch.md', self.repo, 'bad'], cwd=self.home)
        self.assertEqual((done.returncode, 'no such plan file' in done.stderr), (2, True))
        plan = helpers.write_plan(self.home, helpers.node_text('a'))
        done = helpers.drive([plan, f'{self.home}/nonesuch', 'bad'], cwd=self.home)
        self.assertEqual((done.returncode, 'no such repository folder' in done.stderr), (2, True))
        self.assertEqual(helpers.drive([plan, self.repo, 'once'], cwd=self.home).returncode, 0)
        again = helpers.drive([plan, self.repo, 'once'], cwd=self.home)
        self.assertEqual(again.returncode, 2, 'a run name cannot be reused')

    def write_raw(self, text):
        path = f'{self.home}/raw.md'
        with open(path, 'w') as handle:
            handle.write(text)
        return path

    def test_the_real_command_reports_a_wrong_number_of_arguments_as_2_and_documents_the_codes(self):
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'run', '--help'], capture_output=True, text=True,
                              env=support.clean_environment(), cwd=support.ROOT)
        for text in ('exit codes', 'verified', 'not verified', 'configuration error', 'ABORT', '130'):
            self.assertIn(text, done.stdout)
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'run', f'{self.home}/nonesuch.md', self.repo, 'x'],
                              capture_output=True, text=True, env=support.clean_environment(), cwd=self.home)
        self.assertEqual(done.returncode, 2)


class Interrupts(unittest.TestCase):
    def interrupted(self, sig):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home)
        plan = helpers.write_plan(home, helpers.node_text('a'), helpers.node_text('b', deps='a'))
        mark = f'{home}/started'
        env = support.clean_environment(STUB_MODE='sleep', STUB_MARK=mark)
        process = subprocess.Popen([sys.executable, helpers.DRIVER, plan, repo, 'r'], cwd=home, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        for _ in range(300):
            if os.path.exists(mark):
                break
            time.sleep(0.05)
        else:
            self.fail('the stub worker never started')
        process.send_signal(sig)
        process.communicate(timeout=60)
        return process.returncode, f'{home}/.pangoloop/runs/r'

    def test_sigint_stops_cleanly_with_130(self):
        code, run = self.interrupted(signal.SIGINT)
        self.assertEqual(code, 130)
        self.assertTrue(os.path.isfile(f'{run}/finished_at'))
        result = helpers.read_json(f'{run}/result.json')
        self.assertEqual((result['verdict'], result['nodes']), ('stopped', {'a': 'running', 'b': 'waiting'}))

    def test_sigterm_still_stops_with_124(self):
        code, run = self.interrupted(signal.SIGTERM)
        self.assertEqual(code, 124)
        self.assertEqual(helpers.read_json(f'{run}/result.json')['verdict'], 'stopped')


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
