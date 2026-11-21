"""Fault 3 (November 20, 2026): the engine records every child it starts (a worker, a script step, an acceptance command, an
integration check) in state.json, each in a process group of its own; `--resume` and `pangoloop cleanup <run>` stop what is
recorded, alive and provably the same process (never by pid alone); SIGTERM and SIGINT still stop all children.

The engine runs in a process of its own (tests/driver_spawn.py: the real process machinery, the runner's command is a shell
stub that writes its pids and sleeps), so that it can be killed with `kill -9`."""
import os
import signal
import subprocess
import sys
import time
import unittest

import engine_stub
import helpers
import support
from pangoloop import children

STUB = os.path.join(support.ROOT, 'tests', 'claude_stub.py')
DRIVER = os.path.join(support.ROOT, 'tests', 'driver_spawn.py')


def alive(pid: int) -> bool:
    out = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith('Z')


def wait_for(test, condition, what, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        found = condition()
        if found:
            return found
        time.sleep(0.05)
    test.fail(f'timed out waiting for {what}')


class Scenario(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.repo = helpers.make_repo(self.home)
        self.stub = os.path.join(self.home, 'stub')
        os.makedirs(self.stub)
        self.runs = os.path.join(self.home, 'runs')
        # a worker: writes its pid, starts a grandchild that sleeps, waits; after `resumed` exists it is the ordinary stub
        self.wrapper = os.path.join(self.home, 'claude')
        with open(self.wrapper, 'w') as handle:
            handle.write(f'#!/bin/sh\nif [ -e "{self.stub}/resumed" ]; then exec {sys.executable} {STUB} "$@"; fi\n'
                         f'echo $$ > "{self.stub}/worker.pid"\nsleep 300 &\necho $! > "{self.stub}/worker-child.pid"\nwait\n')
        os.chmod(self.wrapper, 0o755)
        # a command (a script step, an acceptance command): writes its pid, starts a grandchild that sleeps
        self.sleeper = os.path.join(self.home, 'sleeper.sh')
        with open(self.sleeper, 'w') as handle:
            handle.write(f'#!/bin/sh\nif [ -e "{self.stub}/resumed" ]; then exit 0; fi\n'
                         f'echo $$ > "{self.stub}/command.pid"\nsleep 300 &\necho $! > "{self.stub}/command-child.pid"\nwait\n')
        self.pids = []
        self.addCleanup(self.reap)

    def reap(self):
        for pid in self.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def env(self, **extra):
        return support.clean_environment(PANGOLOOP_RUNNER='claude-code', PANGOLOOP_CLAUDE_COMMAND=self.wrapper,
                                         STUB_LOG=os.path.join(self.home, 'stub.log'), PANGOLOOP_RUNS=self.runs, **extra)

    def start(self, *nodes, name='r', extra=()):
        plan = helpers.write_plan(self.home, *nodes)
        process = subprocess.Popen([sys.executable, DRIVER, plan, self.repo, name, *extra], cwd=self.home, env=self.env(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        self.plan = plan
        return process

    def pid_of(self, name):
        path = os.path.join(self.stub, name)

        def read():
            try:
                with open(path) as handle:
                    return int(handle.read().strip() or 0)
            except (OSError, ValueError):
                return 0
        pid = wait_for(self, read, name)
        self.pids.append(pid)
        return pid

    def recorded(self, name='r'):
        state = helpers.read_json(os.path.join(self.runs, name, 'state.json'))
        return state.get('children') or {}

    def cleanup(self, name='r'):
        return subprocess.run([sys.executable, '-m', 'pangoloop', 'cleanup', name], capture_output=True, text=True,
                              env=self.env(), cwd=self.home, timeout=60)

    def worker_nodes(self):
        return (helpers.node_text('a'),)

    def command_node(self, role):
        if role == 'script':
            return helpers.script_node('a', f'run `sh {self.sleeper}`')
        return helpers.node_text('a', role='acceptance', VERIFY=f'`sh {self.sleeper}`')


class KillNine(Scenario):
    def check(self, nodes, pidfile, childfile, kind):
        process = self.start(*nodes)
        pid, grandchild = self.pid_of(pidfile), self.pid_of(childfile)
        entry = wait_for(self, lambda: self.recorded().get(str(pid)), 'the record of the child')
        self.assertEqual(entry['kind'], kind)
        self.assertEqual(entry['started'], children.start_time(pid), 'the start time the system reports')
        self.assertEqual(os.getpgid(pid), pid, 'the child leads a process group of its own')
        process.kill()
        process.wait()
        self.assertTrue(alive(pid) and alive(grandchild), 'the orphans are running: that is the fault')
        done = self.cleanup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f'stopped {kind}', done.stdout)
        self.assertIn(f'pid {pid}', done.stdout)
        wait_for(self, lambda: not alive(pid) and not alive(grandchild), 'the orphans to be stopped')
        self.assertEqual(self.recorded(), {}, 'the record is cleared')
        again = self.cleanup()
        self.assertEqual((again.returncode, again.stdout.strip()), (0, 'no children recorded'))

    def test_a_worker_and_its_child_are_stopped_by_cleanup(self):
        self.check(self.worker_nodes(), 'worker.pid', 'worker-child.pid', 'worker')

    def test_a_script_step_is_stopped_by_cleanup(self):
        self.check((self.command_node('script'),), 'command.pid', 'command-child.pid', 'script step')

    def test_an_acceptance_command_is_stopped_by_cleanup(self):
        self.check((self.command_node('acceptance'),), 'command.pid', 'command-child.pid', 'acceptance command')

    def test_resume_stops_the_orphans_before_anything_else_and_says_so(self):
        process = self.start(*self.worker_nodes())
        pid, grandchild = self.pid_of('worker.pid'), self.pid_of('worker-child.pid')
        wait_for(self, lambda: self.recorded().get(str(pid)), 'the record of the child')
        process.kill()
        process.wait()
        open(os.path.join(self.stub, 'resumed'), 'w').close()   # the next worker is the ordinary stub
        done = subprocess.run([sys.executable, DRIVER, self.plan, self.repo, 'r', '--resume'], capture_output=True,
                              text=True, env=self.env(), cwd=self.home, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('resume: orphan stopped worker', done.stdout)
        self.assertFalse(alive(pid) or alive(grandchild))
        self.assertEqual(helpers.read_json(os.path.join(self.runs, 'r', 'result.json'))['verdict'], 'verified')
        self.assertEqual(self.recorded(), {}, 'nothing is left recorded when the run has ended')

    def test_cleanup_refuses_a_run_that_a_live_engine_holds_and_a_folder_that_is_no_run(self):
        process = self.start(*self.worker_nodes())
        pid = self.pid_of('worker.pid')
        wait_for(self, lambda: self.recorded().get(str(pid)), 'the record of the child')
        done = self.cleanup()
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn('is being run by process', done.stderr)
        self.assertTrue(alive(pid), 'nothing was stopped')
        process.kill()
        process.wait()
        self.assertEqual(self.cleanup('nonesuch').returncode, 2)
        self.assertEqual(subprocess.run([sys.executable, '-m', 'pangoloop', 'cleanup'], capture_output=True, text=True,
                                        env=self.env(), cwd=self.home).returncode, 2)


class NeverByPidAlone(unittest.TestCase):
    def setUp(self):
        self.sleeper = subprocess.Popen(['sleep', '300'], start_new_session=True)
        self.addCleanup(self.sleeper.kill)

    def test_a_process_with_another_start_time_is_left_alone(self):
        item = children.entry(self.sleeper.pid, 'worker', '01-a')
        item['started'] = 'Mon Jan  1 00:00:00 2001'
        lines = children.stop_recorded({str(self.sleeper.pid): item})
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('left alone', lines[0])
        self.assertIsNone(self.sleeper.poll(), 'the process runs on')

    def test_the_same_process_is_stopped_and_a_gone_one_says_nothing(self):
        item = children.entry(self.sleeper.pid, 'acceptance command', 'sleep 300')
        gone = {'pid': 999999, 'started': 'Mon Jan  1 00:00:00 2001', 'kind': 'worker', 'what': 'x'}
        lines = children.stop_recorded({'a': item, 'b': gone})
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('stopped acceptance command', lines[0])
        self.sleeper.wait(timeout=10)
        self.assertEqual(self.sleeper.returncode, -signal.SIGTERM)

    def test_an_entry_without_a_start_time_is_never_stopped(self):
        lines = children.stop_recorded({'a': {'pid': self.sleeper.pid, 'kind': 'worker', 'what': 'x', 'started': ''}})
        self.assertIsNone(self.sleeper.poll())
        self.assertTrue(all('left alone' in line for line in lines))


class Signals(Scenario):
    def signal_stops_everything(self, sig, code, nodes, pidfile, childfile):
        process = self.start(*nodes)
        pid, grandchild = self.pid_of(pidfile), self.pid_of(childfile)
        wait_for(self, lambda: self.recorded().get(str(pid)), 'the record of the child')
        process.send_signal(sig)
        process.communicate(timeout=60)
        self.assertEqual(process.returncode, code)
        wait_for(self, lambda: not alive(pid) and not alive(grandchild), 'the children to be stopped')
        self.assertEqual(helpers.read_json(os.path.join(self.runs, 'r', 'result.json'))['verdict'], 'stopped')
        time.sleep(0.5)   # a node's thread that was still going must not have started anything new (a repair worker did)
        left = subprocess.run(['pgrep', '-f', self.home], capture_output=True, text=True).stdout.split()
        self.assertEqual(left, [], 'nothing of the run is left running')

    def test_sigterm_stops_a_worker_and_its_child(self):
        self.signal_stops_everything(signal.SIGTERM, 124, self.worker_nodes(), 'worker.pid', 'worker-child.pid')

    def test_sigint_stops_a_worker_and_its_child(self):
        self.signal_stops_everything(signal.SIGINT, 130, self.worker_nodes(), 'worker.pid', 'worker-child.pid')

    def test_sigterm_stops_an_acceptance_command_and_its_child(self):
        self.signal_stops_everything(signal.SIGTERM, 124, (self.command_node('acceptance'),), 'command.pid',
                                     'command-child.pid')


class IntegrationCheck(unittest.TestCase):
    def test_the_check_is_recorded_in_a_group_of_its_own_while_it_runs(self):
        import threading
        home = support.temp_dir(self)
        repo = helpers.make_repo(home)
        sg = engine_stub.engine(self, home, SG_INTEGRATION_CHECK='sleep 1.5')
        run = engine_stub.stub_class(sg)(helpers.write_plan(home, helpers.node_text('a')), repo, 'r')
        seen = {}

        def watch():
            end = time.time() + 5
            while time.time() < end and not run.children:
                time.sleep(0.02)
            for item in list(run.children.values()):
                seen.update(item, group=os.getpgid(item['pid']))
        thread = threading.Thread(target=watch)
        thread.start()
        self.assertIsNone(run.integration_check(home))
        thread.join()
        self.assertEqual(seen.get('kind'), 'integration check')
        self.assertEqual(seen['group'], seen['pid'])
        self.assertEqual(run.children, {}, 'removed when the check ends')


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
