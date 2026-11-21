"""Resuming a run that ended before it was verified (pangoloop/resume.py, `run --resume`, `pangoloop resume`).

The engine runs in a process of its own (tests/resume_driver.py: the model replaced by a scripted stub), so that it can be
stopped by a failing node, and also killed with `kill -9` while a worker runs, a node is integrated, the acceptance runs
or a review is being repaired. Every test of a resume asserts that the final verdict is verified, that no worker was
called again for a node accepted before the stop (the stub keeps every call), and that worker numbers go on.
"""
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from collections import namedtuple

import helpers
import support
from pangoloop import requests as rq
from pangoloop import resume

DRIVER = os.path.join(support.ROOT, 'tests', 'resume_driver.py')
Done = namedtuple('Done', 'code out err')
FAIL3 = ['fail', 'fail', 'fail', 'ok']   # a node's three tries all fail, then it works


class Scene:
    """A repository, a plan and a runs folder, and the means to start, kill and inspect the engine."""

    def __init__(self, test, *nodes, worktrees=False, env=None):
        self.test = test
        self.home = support.temp_dir(test)
        self.repo = helpers.make_repo(self.home)
        self.stub = os.path.join(self.home, 'stub')
        os.makedirs(self.stub)
        self.plan = helpers.write_plan(self.home, *nodes)
        self.runs = os.path.join(self.home, 'runs')
        self.env = {'PANGOLOOP_RUNS': self.runs, 'STUB_DIR': self.stub}
        if worktrees:
            self.env['SG_NODE_WORKTREES'] = '1'
        self.env.update(env or {})
        self.processes: list = []
        test.addCleanup(self.cleanup)

    def cleanup(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()

    def behave(self, **behavior):
        path = os.path.join(self.home, 'behavior.json')
        with open(path, 'w') as handle:
            json.dump({key.replace('_', '-'): value for key, value in behavior.items()}, handle)
        self.env['STUB_BEHAVIOR'] = path

    def start(self, name, *flags, plan=None, repo=None, env=None):
        environment = support.clean_environment(**{**self.env, **(env or {})})
        process = subprocess.Popen([sys.executable, DRIVER, plan or self.plan, repo or self.repo, name, *flags],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
                                   cwd=support.ROOT, start_new_session=True)
        self.processes.append(process)
        return process

    def finish(self, process) -> Done:
        out, err = process.communicate(timeout=90)
        return Done(process.returncode, out, err)

    def run(self, name, *flags, **kw) -> Done:
        return self.finish(self.start(name, *flags, **kw))

    def kill9(self, process) -> None:
        """`kill -9` of the engine and everything it started."""
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=30)

    def wait_for(self, path, seconds=30):
        deadline = time.time() + seconds
        while not os.path.exists(path):
            if time.time() > deadline:
                raise AssertionError(f'{path} did not appear')
            time.sleep(0.05)

    def marker(self, name):
        return os.path.join(self.stub, name)

    def folder(self, name):
        return os.path.join(self.runs, name)

    def state(self, name):
        return helpers.read_json(os.path.join(self.folder(name), 'state.json'))

    def result(self, name):
        return helpers.read_json(os.path.join(self.folder(name), 'result.json'))

    def calls(self, node=None):
        path = os.path.join(self.stub, 'calls.jsonl')
        found = [json.loads(line) for line in open(path)] if os.path.exists(path) else []
        return [call for call in found if node is None or call['node'] == node]

    def tags(self, name):
        return sorted(os.listdir(os.path.join(self.folder(name), 'workers')))

    def assert_numbers_continue(self, name):
        numbers = [int(tag.split('-')[0]) for tag in self.tags(name)]
        self.test.assertEqual(numbers, list(range(1, len(numbers) + 1)), 'worker numbers have no gap and no repeat')

    def assert_resumed_ok(self, name, done, accepted_before):
        """The verdict is verified, nothing accepted before the stop was called again, numbers go on."""
        self.test.assertEqual(done.code, 0, done.out + done.err)
        self.test.assertEqual(self.result(name)['verdict'], 'verified')
        for node, count in accepted_before.items():
            self.test.assertEqual(len(self.calls(node)), count, f'no worker was called again for {node}')
        self.assert_numbers_continue(name)


def chain(*more):
    return (helpers.node_text('a'), helpers.node_text('b', deps='a'), helpers.node_text('c', deps='b'), *more)


class Cases:
    """Cases that hold with and without node worktrees; the two subclasses below set WORKTREES."""
    WORKTREES = False

    def scene(self, *nodes, **kw):
        return Scene(self, *nodes, worktrees=self.WORKTREES, **kw)

    def test_stop_after_a_node_was_accepted_then_resume_with_the_plan_fixed(self):
        s = self.scene(helpers.node_text('a'), helpers.script_node('s', 'run `echo hi > ran.txt`', deps='a'),
                       helpers.node_text('b', deps='s'), helpers.node_text('c', deps='b'))
        s.behave(b=FAIL3)
        first = s.run('r1')
        self.assertEqual(first.code, 1, first.out + first.err)
        state = s.state('r1')
        self.assertEqual({k: v['status'] for k, v in state['nodes'].items()},
                         {'a': 'accepted', 's': 'accepted', 'b': 'failed', 'c': 'pending'})
        self.assertFalse(os.path.exists(os.path.join(s.folder('r1'), 'lock.json')), 'the lock is given back')
        workers_before = len(s.tags('r1'))
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 1})
        self.assertEqual(len([t for t in s.tags('r1') if t.endswith('-s')]), 1, 'the script node did not run again')
        self.assertEqual(len(s.calls('b')), 4)
        self.assertEqual(s.tags('r1')[workers_before], f'{workers_before + 1:02d}-b', 'the next worker has the next number')
        self.assertTrue(all(os.path.exists(os.path.join(s.repo, name)) for name in ('a.txt', 'ran.txt', 'b.txt', 'c.txt')))
        # the record
        result = s.result('r1')
        self.assertEqual(result['resumes'], 1)
        self.assertEqual([seg['ended'] for seg in result['segments']], ['not_verified', 'verified'])
        self.assertEqual(result['segments'][1]['carried'], ['a', 's'])
        self.assertEqual(result['segments'][1]['nodes_run'], ['b', 'c'])
        self.assertEqual(result['usage_totals']['workers'], len(s.tags('r1')), 'usage adds up over the segments')
        self.assertIn('verdict=verified', second.out.splitlines()[-1], 'the line other tools parse keeps its form')
        self.assertIn('a: accepted in segment 1, skipped', second.out)
        self.assertTrue(os.path.exists(os.path.join(s.folder('r1'), 'result.segment-1.json')))

    def test_kill_9_while_a_worker_runs(self):
        s = self.scene(*chain())
        s.behave(b=['sleep', 'ok'])
        process = s.start('r1')
        s.wait_for(s.marker('b.started'))
        lock = helpers.read_json(os.path.join(s.folder('r1'), 'lock.json'))
        self.assertEqual(lock['pid'], process.pid)
        self.assertTrue(lock['process_started'])
        s.kill9(process)
        state = s.state('r1')
        self.assertEqual(state['nodes']['b']['status'], 'running')
        self.assertEqual(state['nodes']['a']['status'], 'accepted')
        self.assertFalse(os.path.exists(os.path.join(s.folder('r1'), 'result.json')))
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 1})
        self.assertIn('is stale', second.out)
        self.assertIn('b: was running when the run stopped', second.out)
        self.assertEqual(len(s.calls('b')), 2)
        self.assertEqual([seg['ended'] for seg in s.result('r1')['segments']], ['killed', 'verified'])
        self.assertEqual(s.tags('r1')[:3], ['01-a', '02-b', '03-b'])
        if self.WORKTREES:
            self.assertTrue(os.path.isdir(os.path.join(s.folder('r1'), 'wt', 'b-interrupted-1')), 'the old copy is kept')
            self.assertIn('sg/b-interrupted-1', helpers.vcs(s.repo, 'branch', '--list', 'sg/b*'))

    def test_kill_9_during_the_acceptance(self):
        command = '`touch "$STUB_DIR/acc.started"; if [ -f "$STUB_DIR/acc.go" ]; then true; else sleep 60; fi`'
        s = self.scene(helpers.node_text('a'), helpers.node_text('acc', role='acceptance', deps='a', VERIFY=command))
        process = s.start('r1')
        s.wait_for(s.marker('acc.started'))
        s.kill9(process)
        self.assertEqual(s.state('r1')['nodes']['acc']['status'], 'running')
        open(s.marker('acc.go'), 'w').close()
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 1})
        self.assertEqual(len(s.result('r1')['acceptance']), 1)

    def test_kill_9_while_a_review_is_repaired_and_the_rounds_go_on(self):
        s = self.scene(helpers.node_text('a'), helpers.node_text('rev', role='review', category='review', deps='a'),
                       env={'SG_MAX_FIX_ROUNDS': '2'})
        s.behave(rev=['changes', 'changes', 'pass'], rev_fix1=['sleep', 'ok'])
        process = s.start('r1')
        s.wait_for(s.marker('rev-fix1.started'))
        s.kill9(process)
        progress = s.state('r1')['nodes']['rev']
        self.assertEqual((progress['review_rounds'], progress['fix_rounds']), (1, 1))
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 1})
        tags = s.tags('r1')
        self.assertIn('05-rev-fix2', tags, 'the fix rounds continue from the state (fix 1 was done before the stop)')
        self.assertEqual(len(s.calls('rev')), 3)
        self.assertEqual(s.result('r1')['review_verdicts'], ['CHANGES REQUESTED', 'CHANGES REQUESTED', 'PASS'])

    def test_a_run_folder_without_a_state_file_is_reconstructed(self):
        s = self.scene(*chain())
        s.behave(b=FAIL3)
        self.assertEqual(s.run('r1').code, 1)
        os.remove(os.path.join(s.folder('r1'), 'state.json'))
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 1})
        self.assertIn('no state file: reconstructed', second.out)
        self.assertEqual(s.result('r1')['resumes'], 1)


class SharedFolder(Cases, unittest.TestCase):
    def test_a_review_that_passed_is_not_repeated_when_nothing_was_integrated_after_it(self):
        s = self.scene(helpers.node_text('a'), helpers.node_text('rev', role='review', category='review', deps='a'),
                       helpers.node_text('b', deps='a'))
        s.behave(b=FAIL3)
        self.assertEqual(s.run('r1').code, 1)
        self.assertEqual(s.state('r1')['nodes']['rev']['status'], 'accepted')
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 1, 'rev': 1})

    def test_a_review_is_repeated_when_work_was_integrated_after_it(self):
        s = self.scene(helpers.node_text('a'), helpers.node_text('rev', role='review', category='review', deps='a'),
                       helpers.node_text('b', deps='a'), helpers.node_text('c', deps='b'))
        s.behave(b=['delay:1.5'], c=FAIL3)
        self.assertEqual(s.run('r1').code, 1)
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 1, 'b': 1})
        self.assertEqual(len(s.calls('rev')), 2, 'b was integrated after the review passed')
        self.assertIn('work was integrated after it passed (node b)', second.out)

    def test_reset_rounds(self):
        def stopped():
            s = self.scene(helpers.node_text('a'), helpers.node_text('rev', role='review', category='review', deps='a'),
                           env={'SG_MAX_FIX_ROUNDS': '1'})
            s.behave(rev=['changes', 'changes', 'pass'], rev_fix1=['sleep', 'ok'])
            process = s.start('r1')
            s.wait_for(s.marker('rev-fix1.started'))
            s.kill9(process)
            return s
        s = stopped()
        again = s.run('r1', '--resume')
        self.assertEqual(again.code, 1, 'one fix round was done: the budget of one is spent')
        self.assertFalse([t for t in s.tags('r1') if 'rev-fix2' in t])
        fresh = stopped()
        done = fresh.run('r1', '--resume', '--reset-rounds')
        fresh.assert_resumed_ok('r1', done, {'a': 1})
        self.assertIn('rev-fix2', ' '.join(fresh.tags('r1')))

    def test_a_folder_with_nothing_provable_is_refused(self):
        s = self.scene(*chain())
        s.behave(a=FAIL3)
        self.assertEqual(s.run('r1').code, 1)
        os.remove(os.path.join(s.folder('r1'), 'state.json'))
        second = s.run('r1', '--resume')
        self.assertEqual(second.code, 2)
        self.assertIn('nothing in it can be proven', second.err)

    def test_resume_command_reads_the_plan_and_the_repository_from_the_run(self):
        s = self.scene(*chain())
        s.behave(b=FAIL3)
        self.assertEqual(s.run('r1').code, 1)
        code = ("import json, sys; from pangoloop import cli, graph; "
                "graph.main = lambda argv: print(json.dumps(argv)) or 0; sys.exit(cli.main(['resume', 'r1', '--reset-rounds']))")
        done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, cwd=support.ROOT, timeout=60,
                              env=support.clean_environment(PANGOLOOP_RUNS=s.runs))
        self.assertEqual(json.loads(done.stdout), [os.path.join(s.folder('r1'), 'plan.source.md'), s.repo, 'r1',
                                                   '--resume', '--reset-rounds'])


class NodeWorktrees(Cases, unittest.TestCase):
    WORKTREES = True

    def test_kill_9_while_a_node_is_integrated(self):
        s = self.scene(helpers.node_text('a'), helpers.node_text('b'),
                       env={'SG_INTEGRATION_CHECK': 'touch "$STUB_DIR/check.started"; sleep 60'})
        s.behave(b=['delay:1', 'ok'])
        process = s.start('r1')
        s.wait_for(s.marker('check.started'))
        s.kill9(process)
        state = s.state('r1')
        self.assertEqual((state['nodes']['a']['status'], state['nodes']['b']['status']), ('accepted', 'integrating'))
        second = s.run('r1', '--resume', env={'SG_INTEGRATION_CHECK': 'true'})
        s.assert_resumed_ok('r1', second, {'a': 1})
        self.assertEqual(len(s.calls('b')), 2, 'b was being integrated: it runs again in a fresh copy')
        self.assertTrue(os.path.isdir(os.path.join(s.folder('r1'), 'wt', 'b-interrupted-1')))
        self.assertTrue(os.path.exists(os.path.join(s.repo, 'b.txt')))

    def test_a_rebase_left_in_progress_in_the_repository_is_aborted(self):
        repo = helpers.make_repo(support.temp_dir(self))
        helpers.vcs(repo, 'checkout', '-q', '-b', 'other')
        open(os.path.join(repo, 'base.txt'), 'w').write('other\n')
        helpers.vcs(repo, 'commit', '-q', '-am', 'other')
        helpers.vcs(repo, 'checkout', '-q', 'work')
        open(os.path.join(repo, 'base.txt'), 'w').write('work\n')
        helpers.vcs(repo, 'commit', '-q', '-am', 'work')
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'rebase', 'other'], cwd=repo,
                       capture_output=True)
        self.assertTrue(os.path.isdir(os.path.join(repo, '.git', 'rebase-merge')), 'the rebase is stopped on a conflict')
        notes = resume.abort_in_progress(repo)
        self.assertEqual(notes, ['a rebase left in progress in the repository was aborted'])
        self.assertFalse(os.path.isdir(os.path.join(repo, '.git', 'rebase-merge')))
        self.assertEqual(helpers.vcs(repo, 'rev-parse', '--abbrev-ref', 'HEAD').strip(), 'work')


class Refusals(unittest.TestCase):
    def failed_run(self, **kw):
        s = Scene(self, *chain(), **kw)
        s.behave(b=FAIL3)
        self.assertEqual(s.run('r1').code, 1)
        return s

    def test_a_verified_run_is_refused(self):
        s = Scene(self, *chain())
        self.assertEqual(s.run('r1').code, 0)
        done = s.run('r1', '--resume')
        self.assertEqual(done.code, 2)
        self.assertIn('is verified', done.err)

    def test_a_folder_that_is_not_a_run_is_refused(self):
        s = Scene(self, *chain())
        os.makedirs(os.path.join(s.runs, 'empty'))
        done = s.run('empty', '--resume')
        self.assertEqual(done.code, 2)
        self.assertIn('is not a run folder', done.err)
        self.assertEqual(s.run('missing', '--resume').code, 2)

    def test_without_resume_an_existing_run_is_refused_as_before(self):
        s = self.failed_run()
        done = s.run('r1')
        self.assertEqual(done.code, 2)
        self.assertIn('--resume', done.err)

    def test_a_live_lock_is_refused_and_a_stale_one_is_taken_over(self):
        s = self.failed_run()
        before = open(os.path.join(s.folder('r1'), 'state.json')).read()
        holder = subprocess.Popen(['sleep', '60'])
        self.addCleanup(holder.kill)
        lock = os.path.join(s.folder('r1'), 'lock.json')
        with open(lock, 'w') as handle:
            json.dump({'pid': holder.pid, 'started_at': 'x', 'process_started': resume._ps(holder.pid, 'lstart')}, handle)
        done = s.run('r1', '--resume')
        self.assertEqual(done.code, 2)
        self.assertIn(f'process {holder.pid}', done.err)
        self.assertEqual(open(os.path.join(s.folder('r1'), 'state.json')).read(), before, 'a refusal changes nothing')
        self.assertTrue(os.path.exists(lock))
        holder.kill()
        holder.wait()
        again = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', again, {'a': 1})
        self.assertIn('is stale', again.out)

    def test_another_repository_branch_or_history_is_refused(self):
        s = self.failed_run()
        other = helpers.make_repo(s.home, 'other')
        done = s.run('r1', '--resume', repo=other)
        self.assertEqual(done.code, 2)
        self.assertIn('repository', done.err)
        helpers.vcs(s.repo, 'checkout', '-q', '-b', 'elsewhere')
        done = s.run('r1', '--resume')
        self.assertEqual(done.code, 2)
        self.assertIn('branch', done.err)
        helpers.vcs(s.repo, 'checkout', '-q', 'work')
        open(os.path.join(s.repo, 'mine.txt'), 'w').write('mine\n')
        helpers.vcs(s.repo, 'add', 'mine.txt')
        helpers.vcs(s.repo, 'commit', '-q', '-m', 'mine')
        done = s.run('r1', '--resume')
        self.assertEqual(done.code, 2)
        self.assertIn("not the run's", done.err)

    def test_a_changed_accepted_node_is_refused_and_accepted_with_the_flag(self):
        s = self.failed_run()
        changed = helpers.write_plan(s.home, helpers.node_text('a', TASK='something else'),
                                     helpers.node_text('b', deps='a'), helpers.node_text('c', deps='b'), name='plan2.md')
        before = open(os.path.join(s.folder('r1'), 'state.json')).read()
        done = s.run('r1', '--resume', plan=changed)
        self.assertEqual(done.code, 2)
        self.assertIn('node a: TASK', done.err)
        self.assertEqual(open(os.path.join(s.folder('r1'), 'state.json')).read(), before)
        allowed = s.run('r1', '--resume', '--accept-plan-changes', plan=changed)
        s.assert_resumed_ok('r1', allowed, {'a': 1})
        self.assertTrue(os.path.exists(os.path.join(s.folder('r1'), 'plan.before-resume-1.md')))
        self.assertIn('something else', open(os.path.join(s.folder('r1'), 'plan.md')).read())
        self.assertNotIn('something else', open(os.path.join(s.folder('r1'), 'plan.before-resume-1.md')).read())
        recorded = s.result('r1')['plan_changes_accepted']
        self.assertEqual([(item['node'], item['field']) for item in recorded], [('a', 'TASK')])

    def test_a_changed_pending_node_and_a_new_node_are_fine(self):
        s = self.failed_run()
        changed = helpers.write_plan(s.home, helpers.node_text('a'), helpers.node_text('b', deps='a', TASK='fixed'),
                                     helpers.node_text('c', deps='b', SCOPE='wider'),
                                     helpers.node_text('d', deps='c'), name='plan2.md', before='A new background line.\n')
        done = s.run('r1', '--resume', plan=changed)
        s.assert_resumed_ok('r1', done, {'a': 1})
        self.assertEqual(len(s.calls('d')), 1)
        self.assertIn('A new background line.', open(os.path.join(s.folder('r1'), 'plan.md')).read())


class RequestsAndRulings(unittest.TestCase):
    def test_an_open_request_is_superseded_and_a_ruling_stays_in_the_prompts(self):
        s = Scene(self, *chain(), env={'SG_HUMAN': '1'})
        s.behave(a=['ask', 'ok'], b=['ask', 'ok'])
        process = s.start('r1')
        s.wait_for(os.path.join(s.folder('r1'), 'human', 'a-1.json'))
        quiet = io.StringIO()
        self.assertEqual(rq.cmd_answer(s.folder('r1'), 'a-1', 'use the blue one', by='person', err=quiet), 0)
        s.wait_for(os.path.join(s.folder('r1'), 'human', 'b-1.json'))
        self.assertEqual([r['state'] for r in rq.load_requests(s.folder('r1')) if r['id'] == 'b-1'], ['open'])
        s.kill9(process)
        second = s.run('r1', '--resume')
        s.assert_resumed_ok('r1', second, {'a': 2})
        states = {r['id']: r['state'] for r in rq.load_requests(s.folder('r1'))}
        self.assertEqual(states, {'a-1': 'answered', 'b-1': 'superseded'})
        self.assertEqual(rq.open_requests(s.folder('r1')), [])
        refused = io.StringIO()
        self.assertNotEqual(rq.cmd_answer(s.folder('r1'), 'b-1', 'late', by='person', err=refused), 0)
        later = s.calls('b')[1]['prompt']
        self.assertIn('Rulings made during this run', later)
        self.assertIn('use the blue one', later, 'a ruling made before the stop is in the prompts after it')
        self.assertNotIn('Earlier in this node you asked', later, 'a superseded request is not answered')
        human = [(entry['node'], entry['state']) for entry in s.result('r1')['human']]
        self.assertIn(('b', 'superseded'), human)


class ViewOfResumedRun(unittest.TestCase):
    def test_the_page_shows_the_segments_and_what_was_accepted_earlier(self):
        from pangoloop import view
        s = Scene(self, *chain())
        s.behave(b=FAIL3)
        self.assertEqual(s.run('r1').code, 1)
        self.assertEqual(s.run('r1', '--resume').code, 0)
        old = view.RUNS
        view.RUNS = s.runs
        self.addCleanup(setattr, view, 'RUNS', old)
        data = view.build_run('r1')
        self.assertTrue(data['resume_line'].startswith('resumed 1 time; segment 2 started '), data['resume_line'])
        nodes = {node['id']: node for node in data['nodes']}
        self.assertTrue(nodes['a']['accepted_earlier'])
        self.assertEqual(nodes['a']['state'], 'accepted')
        self.assertIn('accepted earlier', nodes['a']['state_text'])
        self.assertFalse(nodes['b']['accepted_earlier'])
        self.assertEqual(data['run_state'], 'finished: verified')
        self.assertEqual([seg['ended'] for seg in data['segments']], ['not_verified', 'verified'])


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
