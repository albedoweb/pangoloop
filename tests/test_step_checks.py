"""Fault 1 (November 20, 2026): the steps of a script node are checked before the run: in the plan loading (`run` exits
2 before anything starts), by `preflight --strict` and by `steps check`, one message per finding."""
import io
import os
import contextlib
import unittest

import helpers
import support
from pangoloop import cli, plan, preflight


def faults(steps: str) -> list[str]:
    try:
        plan.parse_steps('n', steps)
    except SystemExit as stop:
        return str(stop.code).split('\n')
    return []


class FormOfSteps(unittest.TestCase):
    def test_the_semicolon_without_the_separating_spaces(self):
        found = faults('run `a`; run `b`')
        self.assertEqual(len(found), 1, found)
        self.assertIn('node n: step 1 of STEPS (run `a`; run `b`)', found[0])
        self.assertIn('semicolon', found[0])
        self.assertEqual(len(faults('run `a` ;run `b`')), 1)

    def test_a_semicolon_with_its_spaces_separates_and_one_inside_backticks_is_legal(self):
        self.assertEqual(faults('run `a` ; run `b`'), [])
        steps = plan.parse_steps('n', 'delete x ; run `npm run lint ; npm test`')
        self.assertEqual([s['word'] for s in steps], ['delete', 'run'])
        self.assertEqual(steps[1]['command'], 'npm run lint ; npm test')
        self.assertEqual(faults('run npm test; npm lint'), [], 'no backticks: the shell command is taken whole')

    def test_empty_unknown_no_command_unbalanced(self):
        self.assertIn('step 2 of STEPS: the step is empty', faults('run a ;  ; run b')[0])
        self.assertIn("unknown step word 'frobnicate'", faults('frobnicate x')[0])
        self.assertIn('no command', faults('run')[0])
        self.assertIn('no command', faults('run ``')[0])
        self.assertIn('no argument', faults('delete')[0])
        self.assertIn('unbalanced backtick', faults('run `echo hi')[0])

    def test_every_finding_has_its_own_message(self):
        found = faults('frobnicate x ; run `a`; run `b` ; run `c ; delete')
        self.assertEqual(len(found), 3, found)   # the open backtick swallows the rest
        self.assertTrue(all(line.startswith('node n: step ') for line in found))

    def test_a_steps_file_line(self):
        _, problems = plan.parse_steps_file('run `a`; run `b`\nmove a b\nrun `x\n')
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(problems[0].startswith('line 1:') and problems[1].startswith('line 3:'))


class BeforeTheRun(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.repo = helpers.make_repo(self.home)

    def text(self):
        with open(self.bad_plan()) as handle:
            return handle.read()

    def bad_plan(self):
        return helpers.write_plan(self.home, helpers.script_node('s1', 'run `a`; run `b`'),
                                  helpers.script_node('s2', 'run `ok`', deps='s1'),
                                  helpers.script_node('s3', 'frobnicate', deps='s2'))

    def test_the_plan_loading_names_every_node_and_step(self):
        with self.assertRaises(SystemExit) as stop:
            plan.parse_nodes(self.text())
        lines = str(stop.exception.code).split('\n')
        self.assertEqual(len(lines), 2, lines)
        self.assertIn('node s1: step 1', lines[0])
        self.assertIn('node s3: step 1', lines[1])
        self.assertEqual(len(plan.parse_nodes(self.text(), strict=False)), 3, 'the viewer stays tolerant')

    def test_run_exits_2_before_anything_starts(self):
        runs = os.path.join(self.home, 'runs')
        done = helpers.drive([self.bad_plan(), self.repo, 'r1'], env={'PANGOLOOP_RUNS': runs})
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('node s1: step 1 of STEPS', done.stderr)
        self.assertFalse(os.path.exists(os.path.join(runs, 'r1')), 'no run folder was made')

    def test_preflight_strict_exits_2_with_one_warning_per_finding(self):
        path = self.bad_plan()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = preflight.main([path, '--strict', '--out', os.path.join(self.home, 'pf')])
        self.assertEqual(code, 2)
        warnings = [line for line in out.getvalue().splitlines() if line.startswith('warning:')]
        self.assertEqual(len(warnings), 2, out.getvalue())

    def test_steps_check_reports_the_file_and_exits_2(self):
        steps_file = os.path.join(self.home, 'steps.txt')
        with open(steps_file, 'w') as handle:
            handle.write('run `a`; run `b`\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(['steps', 'check', self.repo, steps_file])
        self.assertEqual(code, 2)
        self.assertIn('line 1', err.getvalue())
        self.assertIn('semicolon', err.getvalue())


if __name__ == '__main__':
    unittest.main()
