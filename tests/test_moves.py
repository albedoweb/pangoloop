"""Step 4, part A: the `move` step, the `steps_file` field of a script node (with the `moves` format) and the dry form
`python3 -m pangoloop steps check`."""
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

import engine_stub
import helpers
import support
from pangoloop import plan, steps
from test_script_node import run_plan


def make_tree(root: str, files: dict) -> str:
    os.makedirs(root, exist_ok=True)
    for path, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, path)) or root, exist_ok=True)
        with open(os.path.join(root, path), 'w') as handle:
            handle.write(text)
    return root


def listing(root: str) -> list[str]:
    return sorted(os.path.relpath(os.path.join(here, name), root) for here, _, names in os.walk(root)
                  for name in names if '.git' not in here.split(os.sep))


class MoveStep(unittest.TestCase):
    def setUp(self):
        self.copy = make_tree(support.temp_dir(self), {'a/b/c/f1.txt': '1', 'a/b/c/f2.txt': '2', 'a/g.txt': 'g', 'keep.txt': 'k',
                                                      'x/y/z.txt': 'z'})

    def test_a_file_and_a_folder_with_missing_parents_made(self):
        done = steps.move_step(self.copy, 'keep.txt', 'new/deeper/kept.txt')
        self.assertEqual(done['moved'], ('keep.txt', 'new/deeper/kept.txt', 1))
        done = steps.move_step(self.copy, 'a/b/c', 'moved/c2')
        self.assertEqual(done['moved'], ('a/b/c', 'moved/c2', 2))
        self.assertEqual(listing(self.copy), ['a/g.txt', 'moved/c2/f1.txt', 'moved/c2/f2.txt', 'new/deeper/kept.txt', 'x/y/z.txt'])

    def test_an_emptied_source_folder_is_removed_upward_until_one_that_is_not_empty(self):
        done = steps.move_step(self.copy, 'x/y/z.txt', 'z.txt')
        self.assertEqual(done['removed'], ['x/y', 'x'])
        self.assertFalse(os.path.exists(f'{self.copy}/x'))
        done = steps.move_step(self.copy, 'a/b/c', 'c')   # a/b becomes empty, a still holds g.txt
        self.assertEqual(done['removed'], ['a/b'])
        self.assertTrue(os.path.isdir(f'{self.copy}/a'))
        self.assertEqual(steps.describe_move(done), 'moved a/b/c -> c (2 files); removed emptied a/b')

    def test_the_root_is_never_removed(self):
        steps.move_step(self.copy, 'keep.txt', 'k.txt')
        for name in ('a', 'x'):
            steps.move_step(self.copy, name, f'm/{name}')
        self.assertTrue(os.path.isdir(self.copy))

    def refused(self, source, target, why):
        before = listing(self.copy)
        with self.assertRaises(steps.StepRefused) as stop:
            steps.move_step(self.copy, source, target)
        self.assertIn(why, str(stop.exception), (source, target))
        self.assertEqual(listing(self.copy), before, 'a refused move changes nothing')

    def test_the_refusals_of_delete_apply(self):
        self.refused('/etc/passwd', 'p', 'absolute')
        self.refused('keep.txt', '/tmp/p', 'absolute')
        self.refused('../outside', 'p', 'leaves the node')
        self.refused('keep.txt', '../p', 'leaves the node')
        self.refused('.', 'p', 'root')
        self.refused('keep.txt', 'a/..', 'root')
        self.refused('.git/config', 'p', '.git')
        self.refused('keep.txt', 'sub/.git/x', '.git')

    def test_links_are_refused_in_source_and_target(self):
        os.symlink(f'{self.copy}/a', f'{self.copy}/link')
        os.symlink(f'{self.copy}/keep.txt', f'{self.copy}/flink')
        self.refused('link', 'p', 'symbolic link')
        self.refused('flink', 'p', 'symbolic link')
        self.refused('link/g.txt', 'p', 'symbolic link')
        self.refused('keep.txt', 'link/in', 'symbolic link')

    def test_source_and_target_conditions(self):
        self.refused('nothing.txt', 'p', 'the source does not exist')
        self.refused('keep.txt', 'a/g.txt', 'target already exists')
        self.refused('keep.txt', 'x', 'target already exists')   # no merge into an existing folder
        self.refused('a', 'a/b/c/inside', 'inside its own source')
        self.refused('a', 'a', 'the source itself')
        self.refused('keep.txt', 'a/g.txt/under', 'is a file')

    def test_the_parser(self):
        step = plan.parse_step('move "my dir/a b.txt" new/')
        self.assertEqual((step['word'], step['src'], step['dst']), ('move', 'my dir/a b.txt', 'new/'))
        self.assertEqual(plan.parse_step('move\told\tnew')['dst'], 'new')
        for text in ('move one', 'move a b c', 'move'):
            with self.assertRaises(ValueError):
                plan.parse_step(text)
        node = plan.parse_nodes(open(helpers.write_plan(support.temp_dir(self), helpers.script_node('s', 'move a b ; delete c'))).read())[0]
        self.assertEqual([item['word'] for item in node['steps']], ['move', 'delete'])


class StepsFiles(unittest.TestCase):
    def test_the_lines(self):
        found, problems = plan.parse_steps_file('# a comment\n\nmove a b\n\tdelete? "c d"\t e\nrun `echo hi`\n')
        self.assertEqual(problems, [])
        self.assertEqual([(item['word'], item.get('paths') or item.get('command') or (item['src'], item['dst'])) for item in found],
                         [('move', ('a', 'b')), ('delete?', ['c d', 'e']), ('run', 'echo hi')])

    def test_all_wrong_lines_are_named(self):
        found, problems = plan.parse_steps_file('move a\nzap x\nmove ok fine\ndelete "u\n')
        self.assertEqual(len(found), 1)
        self.assertEqual([item.split(':')[0] for item in problems], ['line 1', 'line 2', 'line 4'])

    def test_the_moves_format(self):
        found, problems = plan.parse_steps_file('# old\tnew\nold one.txt\tnew dir/one.txt\n\nb\tc\n', 'moves')
        self.assertEqual(problems, [])
        self.assertEqual([(item['src'], item['dst']) for item in found], [('old one.txt', 'new dir/one.txt'), ('b', 'c')])
        self.assertEqual(found[0]['text'], "move 'old one.txt' 'new dir/one.txt'")
        _, problems = plan.parse_steps_file('only-one-field\na b\tc\td\n', 'moves')
        self.assertEqual(len(problems), 2)

    def test_the_fields_of_a_node(self):
        def parse(*lines):
            node = helpers.script_node('s', 'x').replace('- STEPS: x\n', ''.join(f'- {line}\n' for line in lines))
            return plan.parse_nodes(open(helpers.write_plan(support.temp_dir(self), node)).read())[0]
        node = parse('steps_file: m.tsv', 'steps_file_format: moves')
        self.assertEqual((node['steps'], node['steps_file'], node['steps_file_format']), ([], 'm.tsv', 'moves'))
        node = parse('STEPS: move a b', 'steps_file: m.txt')
        self.assertEqual((len(node['steps']), node['steps_file']), (1, 'm.txt'))
        for lines, message in ((('steps_file_format: moves', 'STEPS: run true'), 'but no steps_file'),
                               (('steps_file: m', 'steps_file_format: csv'), 'is unknown')):
            with self.assertRaises(SystemExit) as stop:
                parse(*lines)
            self.assertIn(message, stop.exception.code)
        with self.assertRaises(SystemExit) as stop:
            plan.parse_nodes(open(helpers.write_plan(support.temp_dir(self), helpers.node_text('a', steps_file='m'))).read())
        self.assertIn('has steps_file but its role is', stop.exception.code)


def moves_tsv(count: int) -> str:
    return ''.join(f'old/dir{i % 10}/file{i}.txt\tnew/part{i % 3}/dir{i % 10}/file{i}.txt\n' for i in range(count))


def file_node(node_id='mv', *extra, fmt='moves', deps='none', first=None):
    node = helpers.script_node(node_id, first or 'run true', deps=deps)
    tail = ''.join(f'- {line}\n' for line in extra)
    if fmt:
        tail += f'- steps_file_format: {fmt}\n'
    if first:
        return node + tail
    return node.replace('- STEPS: run true\n', tail)


class InTheEngine(unittest.TestCase):
    def repo(self, home, count=300, extra=None):
        files = {f'old/dir{i % 10}/file{i}.txt': str(i) for i in range(count)}
        files['moves.tsv'] = moves_tsv(count)
        files.update(extra or {})
        return helpers.make_repo(home, files=files)

    def execute(self, home, repo, *nodes, **kw):
        sg, run, calls = run_plan(self, home, repo, *nodes, env={'SG_NODE_WORKTREES': '1'}, **kw)
        return run, run.execute()

    def test_a_moves_file_of_300_lines(self):
        home = support.temp_dir(self)
        repo = self.repo(home)
        run, verdict = self.execute(home, repo, file_node('mv', 'steps_file: moves.tsv'))
        self.assertEqual(verdict, 'verified', run.status)
        self.assertFalse(os.path.exists(f'{repo}/old'), 'every old folder was emptied and removed')
        self.assertEqual(len([name for name in listing(repo) if name.startswith('new/')]), 300)
        text = run.handoffs['mv']
        lines = text.splitlines()
        self.assertEqual(lines[0], 'Script node `mv`: 300 steps, all done.')
        self.assertEqual(lines[1], "1. move old/dir0/file0.txt new/part0/dir0/file0.txt")
        self.assertEqual(lines[2], '    -> moved old/dir0/file0.txt -> new/part0/dir0/file0.txt (1 file)')
        self.assertEqual(len(lines), 601)
        self.assertIn('removed emptied old/dir9, old', text, 'the last move empties the last folders')
        # git records them as renames when the work is committed
        shown = helpers.vcs(repo, 'log', '-1', '-M', '--name-status', '--format=')
        self.assertEqual(len([row for row in shown.splitlines() if row.startswith('R')]), 300, shown[:300])

    def test_wrong_lines_in_300_and_nothing_moves(self):
        home = support.temp_dir(self)
        lines = moves_tsv(300).splitlines(True)
        lines[217] = 'old/dir7/missing.txt\tnew/elsewhere.txt\n'
        lines[250] = 'old/dir0/file0.txt\tnew/part0/dir0/file300.txt\n'   # a second move out of what step 1 moved
        repo = self.repo(home, extra={'moves.tsv': ''.join(lines)})
        run, verdict = self.execute(home, repo, file_node('mv', 'steps_file: moves.tsv'))
        self.assertNotEqual(verdict, 'verified')
        text = run.handoffs['mv']
        self.assertIn('none run: refused before any step ran: 2 steps would be refused', text)
        self.assertIn('step 218 (move old/dir7/missing.txt new/elsewhere.txt): old/dir7/missing.txt: the source does not exist', text)
        self.assertIn('(step 1 moved old/dir0/file0.txt to new/part0/dir0/file0.txt)', text)
        self.assertEqual(len(listing(repo)), 301, 'nothing was moved')

    def test_order_matters_a_move_into_a_folder_an_earlier_move_made(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files={'a/one.txt': '1', 'b/two.txt': '2', 'c/three.txt': '3'})
        node = helpers.script_node('mv', 'move a pkg/a ; move b pkg/a/inner ; move c pkg/a/inner/c')
        run, verdict = self.execute(home, repo, node)
        self.assertEqual(verdict, 'verified', run.status)
        self.assertEqual(listing(repo), ['pkg/a/inner/c/three.txt', 'pkg/a/inner/two.txt', 'pkg/a/one.txt'])
        # the other way round: a move out of a folder that an earlier step moved away
        repo2 = helpers.make_repo(home, 'repo2', files={'a/one.txt': '1', 'b/two.txt': '2'})
        node = helpers.script_node('mv', 'move a z ; move a/one.txt z2')
        run, verdict = self.execute(home, repo2, node, name='r2')
        self.assertNotEqual(verdict, 'verified')
        self.assertIn('the source does not exist (step 1 moved a to z)', run.handoffs['mv'])
        self.assertEqual(listing(repo2), ['a/one.txt', 'b/two.txt'])

    def test_steps_field_first_then_the_file_and_a_file_made_by_an_earlier_node(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files={'a.txt': 'a', 'b.txt': 'b'})

        def behave(node_id, prompt, cwd, calls):
            with open(f'{cwd}/list.txt', 'w') as handle:
                handle.write('# produced by a node\nmove "moved a.txt" later/a.txt\ndelete b.txt\n')

        node = file_node('mv', 'steps_file: list.txt', fmt='', deps='gen', first='move a.txt "moved a.txt"')
        run, verdict = self.execute(home, repo, helpers.node_text('gen'), node, behave=behave)
        self.assertEqual(verdict, 'verified', run.status)
        self.assertEqual(run.handoffs['mv'].splitlines()[:2], ['Script node `mv`: 3 steps, all done.', '1. move a.txt "moved a.txt"'])
        self.assertEqual(listing(repo), ['later/a.txt', 'list.txt'])

    def test_a_missing_or_unsafe_steps_file_stops_the_node_before_a_step(self):
        home = support.temp_dir(self)
        for number, (name, why) in enumerate((('nope.txt', 'not a file'), ('../x', 'leaves the node'),
                                              ('/etc/hosts', 'absolute'), ('.git/config', '.git'))):
            repo = helpers.make_repo(home, f'repo{number}', files={'a.txt': 'a'})
            node = file_node('mv', f'steps_file: {name}', fmt='', first='move a.txt b.txt')
            run, verdict = self.execute(home, repo, node, name=f'r{number}')
            self.assertNotEqual(verdict, 'verified')
            self.assertIn(why, run.handoffs['mv'], name)
            self.assertEqual(listing(repo), ['a.txt'], 'the STEPS move did not run either')

    def test_a_list_with_a_move_after_a_run_step_is_not_simulated_past_it(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files={'a.txt': 'a'})
        node = helpers.script_node('mv', 'move a.txt b.txt ; run `echo c > made.txt` ; move made.txt c.txt')
        run, verdict = self.execute(home, repo, node)
        self.assertEqual(verdict, 'verified', run.status)
        self.assertEqual(listing(repo), ['b.txt', 'c.txt'])


class DryForm(unittest.TestCase):
    def setUp(self):
        self.copy = make_tree(support.temp_dir(self), {'a/f1': '1', 'a/f2': '2', 'b/g': 'g', 'c/h': 'h', 'keep': 'k'})
        self.work = support.temp_dir(self)

    def check(self, text, *extra):
        path = os.path.join(self.work, 'steps.txt')
        with open(path, 'w') as handle:
            handle.write(text)
        out, err = io.StringIO(), io.StringIO()
        before = listing(self.copy)
        with redirect_stdout(out), redirect_stderr(err):
            code = steps.check_main([self.copy, path, *extra])
        self.assertEqual(listing(self.copy), before, 'the dry form changes nothing')
        return code, out.getvalue(), err.getvalue()

    def test_a_clean_file(self):
        code, out, _ = self.check('move a/f1 n/f1\nmove a/f2 n/f2\ndelete? nothing\ndelete b\n')
        self.assertEqual(code, 0)
        self.assertIn('would move a/f1 -> n/f1', out)
        self.assertIn('would remove emptied a', out)
        self.assertIn('nothing would be refused', out)

    def test_collisions_between_steps(self):
        code, out, _ = self.check('move a/f1 t/x\nmove b/g t/x\ndelete c\nmove c/h h2\nmove a t/x/inside\nmove keep keep/in\n')
        self.assertEqual(code, 1)
        self.assertIn('step 2 (move b/g t/x): t/x: the target already exists (a move never overwrites or merges) '
                      '(step 1 moves a/f1 there: two steps to one target)', out)
        self.assertIn('c/h: the source does not exist (step 3 deleted c)', out)
        self.assertIn('keep/in: the target is inside its own source keep', out)
        self.assertIn('4 of 6 steps would be refused', out)

    def test_the_moves_format_and_the_exit_status_for_a_wrong_file(self):
        code, out, _ = self.check('a/f1\tz1\n', '--format', 'moves')
        self.assertEqual(code, 0)
        code, _, err = self.check('a/f1 z1\n', '--format', 'moves')
        self.assertEqual(code, 2)
        self.assertIn('line 1', err)
        code, _, err = self.check('zap\n')
        self.assertEqual(code, 2)

    def test_through_the_command_line(self):
        path = os.path.join(self.work, 'm.tsv')
        with open(path, 'w') as handle:
            handle.write('a/f1\tz1\nnope\tz2\n')
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'steps', 'check', self.copy, path, '--format', 'moves'],
                              cwd=support.ROOT, capture_output=True, text=True, env=support.clean_environment())
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn('nope: the source does not exist', done.stdout)
        done = subprocess.run([sys.executable, '-m', 'pangoloop', 'steps', 'check'], cwd=support.ROOT, capture_output=True,
                              text=True, env=support.clean_environment())
        self.assertEqual(done.returncode, 2)


class OverlayTests(unittest.TestCase):
    def test_wildcard_delete_after_a_move_sees_the_moved_names(self):
        copy = make_tree(support.temp_dir(self), {'a/x.tmp': '1', 'a/y.txt': '2'})
        found, _ = plan.parse_steps_file('move a/x.tmp b/x.tmp\ndelete b/*.tmp\ndelete? a/*.tmp\ndelete a/*.tmp\n')
        report = steps.simulate(copy, found, stop_at_first=False)
        self.assertEqual([item['status'] for item in report], ['ok', 'ok', 'ok', 'refused'])


def setUpModule():
    support.quiet_resource_warnings()
