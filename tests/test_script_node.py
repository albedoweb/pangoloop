"""Step 2, part A: a node with `role: script` is done by the engine, not by a model: `delete` (with its refusals and
wildcards) and `run`, in the node's own copy or in the repository folder, recorded like a worker, shown by the viewer."""
import glob
import json
import os
import threading
import unittest
import urllib.request

import engine_stub
import helpers
import support
from pangoloop import plan, steps

TREE = {'old/a.txt': '1', 'old/sub/b.txt': '2', 'keep.txt': 'k', 'junk1.tmp': 'j', 'x/junk2.tmp': 'j2'}


def run_plan(test, home, repo, *nodes, name='r', env=None, behave=None):
    sg = engine_stub.engine(test, home, **(env or {}))
    path = helpers.write_plan(home, *nodes)
    cls = engine_stub.stub_class(sg, behave)
    run = cls(path, repo, name)
    sg.CURRENT = run
    return sg, run, cls.calls


class BetweenBuildNodes(unittest.TestCase):
    STEPS = 'delete old/ ; delete? missing/* ; delete **/*.tmp ; run `echo hi > ran.txt ; echo done`'

    def nodes(self):
        return (helpers.node_text('a'), helpers.script_node('clean', self.STEPS, deps='a'),
                helpers.node_text('b', deps='clean'))

    def test_in_own_copies(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files=TREE)
        seen = {}

        def behave(node_id, prompt, cwd, calls):
            if node_id == 'a':
                with open(f'{cwd}/a.txt', 'w') as handle:
                    handle.write('from a')
            if node_id == 'b':
                seen['b'] = sorted(engine_stub.tree(cwd))
                with open(f'{cwd}/b.txt', 'w') as handle:
                    handle.write('from b')

        sg, run, calls = run_plan(self, home, repo, *self.nodes(), env={'SG_NODE_WORKTREES': '1'}, behave=behave)
        self.assertEqual(run.execute(), 'verified', run.status)
        self.assertEqual([call['node'] for call in calls], ['a', 'b'], 'no model session for the script node')
        self.assertEqual(seen['b'], ['a.txt', 'keep.txt', 'ran.txt'], 'b starts from the accepted work, cleaned')
        self.assertEqual(sorted(engine_stub.tree(repo)), ['a.txt', 'b.txt', 'keep.txt', 'ran.txt'])
        self.assertEqual(open(f'{repo}/ran.txt').read(), 'hi\n')
        self.assertIn('sg/clean', helpers.vcs(repo, 'branch', '--list', 'sg/clean'))
        self.assertIn('clean', helpers.vcs(repo, 'log', '--format=%s'))
        copies = {fact['node']: fact for fact in run.copies}
        self.assertTrue(copies['clean']['integrated'])
        self.assertFalse(os.path.exists(f'{run.out}/wt/clean'), 'its copy is removed after integration, like a build node')

    def test_without_node_worktrees_it_runs_in_the_repository_folder(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files=TREE)
        sg, run, calls = run_plan(self, home, repo, *self.nodes())
        self.assertEqual(run.execute(), 'verified', run.status)
        self.assertEqual([call['cwd'] for call in calls], [None, None])
        self.assertEqual(sorted(engine_stub.tree(repo)), ['keep.txt', 'ran.txt'])

    def test_the_handoff_lists_each_step_with_what_it_did(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files=TREE)
        sg, run, calls = run_plan(self, home, repo, *self.nodes())
        run.execute()
        text = run.handoffs['clean']
        lines = text.splitlines()
        self.assertEqual(lines[0], 'Script node `clean`: 4 steps, all done.')
        self.assertEqual(lines[1:6], ['1. delete old/', '    -> deleted old (2 files)',
                                      '2. delete? missing/*', '    -> deleted nothing; missing/*: nothing matched, skipped',
                                      '3. delete **/*.tmp'])
        self.assertEqual(lines[6], '    -> deleted junk1.tmp (1 file), x/junk2.tmp (1 file)')
        self.assertEqual(lines[7], '4. run `echo hi > ran.txt ; echo done`')
        self.assertRegex(lines[8], r'^    -> exit 0, \d+s$')
        [answer] = glob.glob(f'{run.out}/workers/*-clean/answer.md')
        with open(answer) as handle:
            self.assertEqual(handle.read(), text)

    def test_the_run_records_a_worker_row_with_the_model_script_and_the_viewer_shows_it(self):
        from pangoloop import view
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files=TREE)
        sg, run, calls = run_plan(self, home, repo, *self.nodes(), name='shown')
        verdict = run.execute()   # no result.json yet: the page works from the files, as for a run that is going
        self.assertEqual(verdict, 'verified')
        row = [rec for rec in run.records if rec['node'] == 'clean']
        self.assertEqual(len(row), 1)
        self.assertEqual((row[0]['model'], row[0]['category'], row[0]['exit'], row[0]['ok']), ('script', 'script', 0, True))
        sg.write_result(run, verdict, run.status)
        self.assertEqual([w['model'] for w in json.load(open(f'{run.out}/result.json'))['workers'] if w['node'] == 'clean'],
                         ['script'])
        saved = view.RUNS
        view.RUNS = f'{home}/runs'
        self.addCleanup(setattr, view, 'RUNS', saved)
        server = view.Server(('127.0.0.1', 0), view.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        data = json.load(urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}/api/run/shown'))
        node = next(item for item in data['nodes'] if item['id'] == 'clean')
        self.assertEqual((node['role'], node['model'], node['state']), ('script', 'script', 'accepted'))
        self.assertEqual(node['task'], self.STEPS)
        worker = next(item for item in data['workers'] if item['node'] == 'clean')
        self.assertEqual((worker['model'], worker['status'], worker['tool_calls']), ('script', 'ok', 4))
        self.assertEqual(data['by_model']['script']['workers'], 1)

    def test_the_viewer_shows_a_failed_script_node_as_failed_without_retrying_it(self):
        from pangoloop import view
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files=TREE)
        sg, run, calls = run_plan(self, home, repo, helpers.script_node('bad', 'run exit 3'), name='badrun')
        self.assertEqual(run.execute(), 'not_verified')
        saved = view.RUNS
        view.RUNS = f'{home}/runs'
        self.addCleanup(setattr, view, 'RUNS', saved)
        node = view.build_run('badrun')['nodes'][0]
        self.assertEqual((node['state'], node['state_text'], node['model']), ('failed', 'a step failed', 'script'))


class Refusals(unittest.TestCase):
    """Each refusal fails the node with a clear message and changes nothing."""

    def setUp(self):
        self.home = support.temp_dir(self)
        os.makedirs(f'{self.home}/outside')
        with open(f'{self.home}/outside/file', 'w') as handle:
            handle.write('precious')
        files = dict(TREE, **{'sub/deep/f.txt': 'f'})
        self.repo = helpers.make_repo(self.home, files=files)
        os.symlink(f'{self.home}/outside', f'{self.repo}/link')
        os.symlink('keep.txt', f'{self.repo}/keeplink')

    def refused(self, steps_text, *messages):
        before, outside = engine_stub.tree(self.repo), engine_stub.tree(f'{self.home}/outside')
        sg, run, calls = run_plan(self, self.home, self.repo, helpers.script_node('s', steps_text),
                                  name='r' + str(abs(hash(steps_text))))
        self.assertEqual(run.execute(), 'not_verified', steps_text)
        self.assertEqual(run.status, {'s': 'failed'})
        self.assertEqual(engine_stub.tree(self.repo), before, f'{steps_text}: the repository changed')
        self.assertEqual(engine_stub.tree(f'{self.home}/outside'), outside)
        self.assertEqual(calls, [], 'no model was asked')
        text = run.handoffs['s']
        for message in messages:
            self.assertIn(message, text, steps_text)
        return text

    def test_absolute_path(self):
        self.refused(f'delete {self.home}/outside/file', 'an absolute path is not allowed')
        self.refused(f'delete {self.repo}/keep.txt', 'an absolute path is not allowed')

    def test_dotdot_that_leaves_the_copy(self):
        self.refused('delete ../outside/file', "leaves the node's copy")
        self.refused('delete sub/../../outside', "leaves the node's copy")

    def test_inside_dot_git(self):
        self.refused('delete .git', 'anything inside .git is refused')
        self.refused('delete .git/config', 'anything inside .git is refused')
        self.refused('delete sub/.git/x', 'anything inside .git is refused')
        self.assertTrue(os.path.isdir(f'{self.repo}/.git'))

    def test_the_root_itself(self):
        for path in ('.', './', 'sub/..', 'sub/../'):
            self.refused(f'delete {path}', 'the root of the node')

    def test_through_a_symbolic_link(self):
        self.refused('delete link/file', 'goes through a symbolic link (link)')
        self.refused('delete link', 'goes through a symbolic link (link)')   # a link is not deleted by a step either
        self.refused('delete keeplink', 'symbolic link')
        self.refused('delete lin?', 'symbolic link')
        self.refused('delete **/file', 'matches nothing')   # `**` does not follow links, so the file is not found

    def test_a_pattern_that_matches_nothing_fails_unless_it_is_delete_question_mark(self):
        text = self.refused('delete nothing/here', "nothing/here: matches nothing in the node's copy", '`delete?`')
        self.assertIn('FAILED', text)

    def test_one_bad_path_stops_the_whole_step_and_a_bad_step_stops_the_node_before_any_runs(self):
        self.refused('delete keep.txt link/file', 'goes through a symbolic link')   # keep.txt is checked first, kept
        text = self.refused('delete keep.txt ; delete ../outside', "leaves the node's copy")
        self.assertIn('refused before any step ran', text)

    def test_delete_question_mark_skips_and_reports_a_missing_path(self):
        sg, run, calls = run_plan(self, self.home, self.repo, helpers.script_node('s', 'delete? nothing/here ; delete keep.txt'))
        self.assertEqual(run.execute(), 'verified')
        self.assertIn('nothing/here: nothing matched, skipped', run.handoffs['s'])
        self.assertFalse(os.path.exists(f'{self.repo}/keep.txt'))

    def test_a_failing_run_fails_the_node_and_later_steps_do_not_run(self):
        sg, run, calls = run_plan(self, self.home, self.repo, helpers.script_node(
            's', 'delete keep.txt ; run `echo before the end ; exit 3` ; delete junk1.tmp'))
        self.assertEqual(run.execute(), 'not_verified')
        text = run.handoffs['s']
        self.assertIn('Script node `s`: 3 steps, 1 done, step 2 failed, 1 not run.', text)
        self.assertRegex(text, r'FAILED: exit 3, \d+s; the end of its output:\n +before the end')
        self.assertFalse(os.path.exists(f'{self.repo}/keep.txt'), 'the step before the failure was done')
        self.assertTrue(os.path.exists(f'{self.repo}/junk1.tmp'), 'the step after it was not')
        record = [rec for rec in run.records if rec['node'] == 's'][0]
        self.assertEqual((record['exit'], record['ok']), (1, False))

    def test_a_copy_reached_through_a_linked_parent_folder_is_not_refused(self):
        os.symlink(self.repo, f'{self.home}/repolink')
        done = steps.delete_step(f'{self.home}/repolink', ['keep.txt'])
        self.assertEqual(done['deleted'], [('keep.txt', 1)])


class Wildcards(unittest.TestCase):
    def setUp(self):
        self.copy = support.temp_dir(self)
        for path in ('a1.log', 'a2.log', 'b.log', 'c.txt', 'sub/c.log', 'sub/deep/d.log', '.hidden.log', '.cache/z.log',
                     '.git/HEAD', 'dir/x', 'dir/y'):
            os.makedirs(os.path.dirname(f'{self.copy}/{path}'), exist_ok=True)
            with open(f'{self.copy}/{path}', 'w') as handle:
                handle.write(path)

    def left(self):
        return sorted(engine_stub.tree(self.copy))

    def test_question_mark_and_star_stay_in_one_component(self):
        done = steps.delete_step(self.copy, ['a?.log', 's*/c.log'])
        self.assertEqual(sorted(path for path, _ in done['deleted']), ['a1.log', 'a2.log', 'sub/c.log'])
        self.assertIn('b.log', self.left())
        self.assertIn('sub/deep/d.log', self.left())

    def test_double_star_matches_any_depth_but_not_hidden_names_or_git(self):
        done = steps.delete_step(self.copy, ['**/*.log'])
        self.assertEqual(sorted(path for path, _ in done['deleted']), ['a1.log', 'a2.log', 'b.log', 'sub/c.log', 'sub/deep/d.log'])
        self.assertEqual(self.left(), ['.cache/z.log', '.hidden.log', 'c.txt', 'dir/x', 'dir/y'])
        self.assertTrue(os.path.exists(f'{self.copy}/.git/HEAD'))

    def test_a_dot_pattern_matches_hidden_names_but_never_git(self):
        done = steps.delete_step(self.copy, ['.*'])
        self.assertEqual(sorted(path for path, _ in done['deleted']), ['.cache', '.hidden.log'])
        self.assertTrue(os.path.exists(f'{self.copy}/.git/HEAD'))

    def test_a_folder_goes_whole_with_its_file_count_and_a_trailing_double_star_means_everything_below(self):
        done = steps.delete_step(self.copy, ['dir/', 'sub'])
        self.assertEqual(done['deleted'], [('dir', 2), ('sub', 2)])
        done = steps.delete_step(self.copy, ['**'])
        self.assertEqual(sorted(path for path, _ in done['deleted']), ['a1.log', 'a2.log', 'b.log', 'c.txt'])
        self.assertEqual(self.left(), ['.cache/z.log', '.hidden.log'])
        self.assertTrue(os.path.exists(f'{self.copy}/.git/HEAD'))

    def test_overlapping_matches_are_deleted_once(self):
        done = steps.delete_step(self.copy, ['sub', 'sub/**/*.log', 'sub/deep'])
        self.assertEqual(done['deleted'], [('sub', 2)])


class PlanParsing(unittest.TestCase):
    def parse(self, *nodes):
        return plan.parse_nodes(open(helpers.write_plan(support.temp_dir(self), *nodes)).read())

    def test_the_parsed_steps(self):
        node = self.parse(helpers.script_node('s', 'delete "my dir/" b* ; delete? c ; run `a ; b` ; run make test'))[0]
        self.assertEqual([step['word'] for step in node['steps']], ['delete', 'delete?', 'run', 'run'])
        self.assertEqual(node['steps'][0]['paths'], ['my dir/', 'b*'])
        self.assertEqual([step.get('command') for step in node['steps'][2:]], ['a ; b', 'make test'])
        self.assertEqual((node['category'], node['TASK'], node['VERIFY']), ('script', '', ''))

    def test_which_fields_a_script_node_needs(self):
        with self.assertRaises(SystemExit) as stop:
            self.parse(helpers.script_node('s', 'run true').replace('- STEPS: run true\n', ''))
        self.assertIn("node s lacks ['STEPS or steps_file']", stop.exception.code)
        with self.assertRaises(SystemExit) as stop:
            self.parse(helpers.node_text('a', STEPS='run true'))
        self.assertIn("has STEPS but its role is 'build'", stop.exception.code)

    def test_faults_in_steps_stop_at_parsing(self):
        for text, message in (('zap everything', "unknown step word 'zap'"), ('delete', 'has no argument'),
                              ('run true ;  ; run true', 'is empty'), ('delete "unclosed', 'No closing quotation'),
                              ('rm -rf x', "unknown step word 'rm'")):
            with self.assertRaises(SystemExit) as stop:
                self.parse(helpers.script_node('s', text))
            self.assertIn(message, stop.exception.code, text)

    def test_an_unknown_step_word_fails_before_anything_runs_and_the_command_exits_2(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home, files=TREE)
        path = helpers.write_plan(home, helpers.node_text('a'), helpers.script_node('s', 'delete keep.txt ; zap it', deps='a'))
        done = helpers.drive([path, repo, 'bad'], cwd=home)
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown step word 'zap'", done.stderr)
        self.assertTrue(os.path.exists(f'{repo}/keep.txt'))
        self.assertFalse(os.path.exists(f'{home}/.pangoloop/runs/bad'))

    def test_the_viewer_reads_a_broken_steps_field_without_stopping(self):
        nodes = plan.parse_nodes(open(helpers.write_plan(support.temp_dir(self), helpers.script_node('s', 'zap'))).read(),
                                 strict=False)
        self.assertEqual((nodes[0]['steps'], nodes[0]['STEPS']), ([], 'zap'))


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
