"""Detached runs, `status`, the JSON forms of `requests`, the page's port and file, and two projects at once. The model is the
stand-in of the suite (tests/claude_stub.py); nothing here calls a real one."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest

import project_helpers as h
import support

ASK_PLAN = '''# Plan: one node that asks

Repository: `{{REPO}}`, branch `{{BRANCH}}`.

## Nodes

### a
- label: Make a file
- kind: build
- role: coder
- depends_on: none
- write_scope: a.txt
- TASK: Create a.txt.
- DELIVERABLE: a.txt
- SCOPE: a.txt
- VERIFY: `true`
- STOP WHEN: done

## Task statement

Make a.txt.
'''


def start_view(test, folder: str, env: dict, *args: str):
    """`pangoloop view` in a process of its own; (the process, its first line)."""
    process = subprocess.Popen([sys.executable, '-m', 'pangoloop', 'view', *args], cwd=folder, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    test.addCleanup(lambda: (process.poll() is None and process.terminate(), process.wait(timeout=10),
                             process.stdout.close(), process.stderr.close()))
    return process, process.stdout.readline().strip()


class Detach(unittest.TestCase):
    def test_returns_at_once_with_the_run_its_folder_and_a_log_in_it(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        began = time.time()
        done = h.pangoloop('run', 'checks', '--detach', cwd=folder, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(time.time() - began, 30)
        lines = dict(line.split(': ', 1) for line in done.stdout.splitlines() if ': ' in line)
        name = lines['run']
        run = os.path.join(os.path.realpath(folder), '.pangoloop', 'runs', name)
        self.assertEqual(lines['folder'], run)
        self.assertEqual(lines['engine log'], os.path.join(run, 'engine.log'))
        self.assertTrue(os.path.isdir(run))
        self.assertIn('page: not serving', done.stdout)
        over = h.wait_until_over(folder, env, name)
        self.assertEqual((over['state'], over['verdict']), ('verified', 'verified'))
        with open(os.path.join(run, 'engine.log')) as handle:
            self.assertIn('verdict=verified', handle.read())
        self.assertEqual([f for f in os.listdir(os.path.join(run, '..')) if f.startswith('.')], [], 'no early log is left')

    def test_the_engine_survives_the_end_of_the_command_and_is_in_its_own_session(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'ask.md', ASK_PLAN)
        done = h.pangoloop('run', 'ask', '--detach', '--json', cwd=folder, env=dict(env, STUB_CLAUDE='ask'))
        self.assertEqual(done.returncode, 0, done.stderr)
        info = json.loads(done.stdout)
        self.assertEqual(sorted(info), ['folder', 'log', 'page', 'page_run', 'pid', 'run'])
        self.assertIsNone(info['page'])
        os.kill(info['pid'], 0)   # alive after the command ended
        self.assertEqual(os.getsid(info['pid']), info['pid'], 'a session of its own')
        self.assertNotEqual(os.getpgid(info['pid']), os.getpgid(0))
        ended = h.pangoloop('requests', 'wait', info['run'], '--json', '--for', 'person', cwd=folder, env=env)
        self.assertEqual(ended.returncode, 0, ended.stdout + ended.stderr)
        h.pangoloop('requests', 'answer', info['run'], 'a-1', '--abort', '--by', 'person', cwd=folder, env=env)
        over = h.wait_until_over(folder, env, info['run'])
        self.assertEqual(over['state'], 'aborted')

    def test_a_plan_fault_is_reported_by_the_command_and_leaves_no_run(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'bad.md', h.SCRIPT_PLAN.replace('- kind: script\n', '- kind: nonsense\n'))
        done = h.pangoloop('run', 'bad', '--detach', '--name', 'bad-1', cwd=folder, env=env)
        self.assertEqual(done.returncode, 2)
        self.assertIn('kind', done.stderr)
        runs = os.path.join(folder, '.pangoloop', 'runs')
        self.assertEqual(sorted(os.listdir(runs)), ['README.md'], 'no run folder and no early log')

    def test_a_used_name_is_refused(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        self.assertEqual(h.pangoloop('run', 'checks', '--detach', '--name', 'same', cwd=folder, env=env).returncode, 0)
        h.wait_until_over(folder, env, 'same')
        again = h.pangoloop('run', 'checks', '--detach', '--name', 'same', cwd=folder, env=env)
        self.assertEqual(again.returncode, 2)
        self.assertIn('exists already', again.stderr)

    def test_view_with_detach_starts_the_page_and_prints_its_address(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        done = h.pangoloop('run', 'checks', '--detach', '--view', cwd=folder, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        record = h.read_json(os.path.join(folder, '.pangoloop', 'view.json'))
        self.assertEqual(sorted(record), ['pid', 'port', 'process_started', 'project', 'started', 'url'])
        self.assertIn(f"page: http://127.0.0.1:{record['port']}/?run=", done.stdout)
        status, page = h.fetch(record['url'])
        self.assertEqual(status, 200)
        self.assertIn('<html', page)
        name = re.search(r'^run: (\S+)', done.stdout, re.M).group(1)
        self.assertEqual(h.run_state(folder, env, name)['page'], record['url'])
        self.assertEqual(h.run_state(folder, env)['page'], record['url'])
        listed = json.loads(h.fetch(record['url'] + 'api/runs')[1])['runs']
        self.assertEqual([r['name'] for r in listed], [name], 'the page serves this project\'s runs')

    def test_a_second_page_for_the_project_is_not_started(self):
        folder, env = h.make_project(self)
        first, line = start_view(self, folder, env)
        self.assertRegex(line, r'^http://127\.0\.0\.1:\d+/$')
        second = h.pangoloop('view', cwd=folder, env=env)
        self.assertEqual(second.returncode, 0)
        self.assertIn(f'already serving at {line}', second.stdout)

    def test_a_stale_view_file_is_replaced(self):
        folder, env = h.make_project(self)
        stale = {'pid': 2 ** 22 + 1, 'port': 5999, 'started': '2026-01-01T00:00:00Z', 'process_started': 'x',
                 'url': 'http://127.0.0.1:5999/', 'project': folder}
        with open(os.path.join(folder, '.pangoloop', 'view.json'), 'w') as handle:
            json.dump(stale, handle)
        self.assertEqual(h.run_state(folder, env)['page'], None, 'a stale file is not an address')
        process, line = start_view(self, folder, env)
        record = h.read_json(os.path.join(folder, '.pangoloop', 'view.json'))
        self.assertEqual((record['pid'], record['url']), (process.pid, line))
        # a live process with another start time (a reused pid) is stale too
        record['process_started'] = 'Mon Jan  1 00:00:00 2001'
        with open(os.path.join(folder, '.pangoloop', 'view.json'), 'w') as handle:
            json.dump(record, handle)
        self.assertIsNone(h.run_state(folder, env)['page'])

    def test_the_file_goes_when_the_page_stops(self):
        folder, env = h.make_project(self)
        process, _ = start_view(self, folder, env)
        path = os.path.join(folder, '.pangoloop', 'view.json')
        self.assertTrue(os.path.exists(path))
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
        self.assertFalse(os.path.exists(path))


class Ports(unittest.TestCase):
    def test_the_given_port_wins_then_the_configurations_then_the_first_free_one(self):
        folder, env = h.make_project(self)
        busy = subprocess.Popen([sys.executable, '-c', 'import socket,sys,time; s=socket.socket(); '
                                 's.bind(("127.0.0.1", 0)); print(s.getsockname()[1], flush=True); time.sleep(60)'],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (busy.kill(), busy.wait(), busy.stdout.close()))
        taken = int(busy.stdout.readline())
        process, line = start_view(self, folder, env, '--port', str(taken + 1))
        self.assertEqual(line, f'http://127.0.0.1:{taken + 1}/')
        process.terminate()
        process.wait(timeout=10)
        with open(os.path.join(folder, 'pangoloop.toml'), 'a') as handle:
            handle.write(f'\n[view]\nport = {taken + 2}\n')
        process, line = start_view(self, folder, env)
        self.assertEqual(line, f'http://127.0.0.1:{taken + 2}/', 'the configuration\'s port')
        process.terminate()
        process.wait(timeout=10)
        with open(os.path.join(folder, 'pangoloop.toml')) as handle:
            text = handle.read().replace(f'port = {taken + 2}', '# port')
        with open(os.path.join(folder, 'pangoloop.toml'), 'w') as handle:
            handle.write(text)
        process, line = start_view(self, folder, env)
        self.assertRegex(line, r'^http://127\.0\.0\.1:(\d+)/$')
        self.assertGreaterEqual(int(re.search(r':(\d+)/', line).group(1)), 5420)

    def test_a_given_port_that_is_taken_is_an_error(self):
        folder, env = h.make_project(self)
        first, line = start_view(self, folder, env)
        port = re.search(r':(\d+)/', line).group(1)
        other, other_env = h.make_project(self, 'other')
        done = h.pangoloop('view', '--port', port, cwd=other, env=other_env)
        self.assertEqual(done.returncode, 2)
        self.assertIn('cannot listen', done.stderr)

    def test_it_listens_on_the_loopback_address_only_and_says_there_is_no_login(self):
        folder, env = h.make_project(self)
        process, line = start_view(self, folder, env)
        port = int(re.search(r':(\d+)/', line).group(1))
        lsof = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'], capture_output=True, text=True) if shutil.which('lsof') else None
        if lsof is not None and lsof.returncode == 0 and lsof.stdout:
            self.assertIn('127.0.0.1:', lsof.stdout)
            self.assertNotIn('*:', lsof.stdout)
        self.assertIn('no login', process.stdout.readline())
        self.assertIn('no login', h.pangoloop('view', '--help', cwd=folder, env=env).stdout.lower())


class Status(unittest.TestCase):
    def test_the_contract_of_status_json_without_a_run(self):
        folder, env = h.make_project(self)
        empty = h.run_state(folder, env)
        self.assertEqual(sorted(empty), ['page', 'project', 'runs', 'runs_folder'])
        self.assertEqual((empty['runs'], empty['page'], empty['project']), ([], None, os.path.realpath(folder)))
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        self.assertEqual(h.pangoloop('run', 'checks', '--name', 'one', cwd=folder, env=env).returncode, 0)
        self.assertEqual(h.pangoloop('run', 'checks', '--name', 'two', cwd=folder, env=env).returncode, 0)
        rows = h.run_state(folder, env)['runs']
        self.assertEqual(sorted(rows[0]), ['ended', 'name', 'open_requests', 'started', 'state', 'verdict', 'waiting_for'])
        self.assertEqual([r['name'] for r in rows], ['two', 'one'], 'newest first')
        self.assertEqual({(r['state'], r['verdict'], r['waiting_for'], r['open_requests']) for r in rows},
                         {('verified', 'verified', None, 0)})
        self.assertRegex(rows[0]['started'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertRegex(rows[0]['ended'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        text = h.pangoloop('status', cwd=folder, env=env).stdout
        self.assertIn('verified', text)
        self.assertIn('one', text)

    def test_the_contract_of_status_json_for_a_run(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        self.assertEqual(h.pangoloop('run', 'checks', '--name', 'one', cwd=folder, env=env).returncode, 0)
        data = h.run_state(folder, env, 'one')
        self.assertEqual(sorted(data), ['branch', 'cost', 'ended', 'folder', 'name', 'nodes', 'page', 'plan', 'repo', 'requests',
                                        'reviews', 'runner', 'started', 'state', 'verdict', 'waiting_for'])
        self.assertEqual((data['name'], data['state'], data['runner'], data['branch']), ('one', 'verified', 'claude-code', 'main'))
        self.assertEqual([sorted(n) for n in data['nodes']],
                         [['depends_on', 'id', 'kind', 'label', 'role', 'state', 'workers']] * 2)
        self.assertEqual([(n['id'], n['kind'], n['state'], n['depends_on']) for n in data['nodes']],
                         [('one', 'script', 'accepted', []), ('accept', 'acceptance', 'accepted', ['one'])])
        self.assertEqual(data['requests'], [])
        self.assertIn('usd', data['cost'])
        text = h.pangoloop('status', 'one', cwd=folder, env=env).stdout
        self.assertIn('run one: verified', text)
        self.assertIn('accepted', text)
        missing = h.pangoloop('status', 'nonesuch', cwd=folder, env=env)
        self.assertEqual(missing.returncode, 2)

    def test_a_run_whose_engine_is_gone_without_a_result_is_stopped(self):
        folder, env = h.make_project(self)
        runs = os.path.join(folder, '.pangoloop', 'runs')
        run = os.path.join(runs, 'ghost')
        os.makedirs(run)
        with open(os.path.join(run, 'started_at'), 'w') as handle:
            handle.write('2026-11-18T10:00:00Z\n')
        with open(os.path.join(run, 'lock.json'), 'w') as handle:
            json.dump({'pid': 2 ** 22 + 1, 'started_at': 'x', 'process_started': 'x'}, handle)
        self.assertEqual(h.run_state(folder, env, 'ghost')['state'], 'stopped')
        os.remove(os.path.join(run, 'lock.json'))
        old = time.time() - 3600
        os.utime(run, (old, old))
        self.assertEqual(h.run_state(folder, env, 'ghost')['state'], 'stopped')

    def test_not_verified_and_aborted_are_told_apart(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'bad.md', h.SCRIPT_PLAN.replace('STEPS: run `true`', 'STEPS: run `false`'))
        done = h.pangoloop('run', 'bad', '--name', 'bad-1', cwd=folder, env=env)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        data = h.run_state(folder, env, 'bad-1')
        self.assertEqual((data['state'], data['verdict']), ('not verified', 'not_verified'))
        self.assertEqual({n['id']: n['state'] for n in data['nodes']}, {'one': 'failed', 'accept': 'waiting'})


class Requests(unittest.TestCase):
    def test_requests_json_and_the_states_of_a_run_that_asks(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'ask.md', ASK_PLAN)
        started = h.pangoloop('run', 'ask', '--detach', '--json', cwd=folder, env=dict(env, STUB_CLAUDE='ask'))
        self.assertEqual(started.returncode, 0, started.stderr)
        name = json.loads(started.stdout)['run']
        waited = h.pangoloop('requests', 'wait', name, '--json', cwd=folder, env=env, timeout=90)
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        request = json.loads(waited.stdout)
        self.assertEqual(sorted(request), ['answered_at', 'answered_by', 'asked_at', 'for', 'id', 'node', 'question', 'state',
                                           'worker', 'worker_kind'])
        self.assertEqual((request['id'], request['node'], request['for'], request['question'], request['state']),
                         ('a-1', 'a', 'person', 'which one?', 'open'))
        self.assertRegex(request['asked_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        listed = h.pangoloop('requests', 'list', name, '--json', cwd=folder, env=env)
        self.assertEqual([json.loads(line) for line in listed.stdout.splitlines()], [request])
        data = h.run_state(folder, env, name)
        self.assertEqual((data['state'], data['waiting_for']), ('running', 'person'))
        self.assertEqual(data['requests'], [{'id': 'a-1', 'node': 'a', 'for': 'person', 'question': 'which one?',
                                             'asked_at': request['asked_at']}])
        self.assertEqual([n['state'] for n in data['nodes']], ['waiting for human'])
        self.assertEqual(h.run_state(folder, env)['runs'][0]['open_requests'], 1)
        h.pangoloop('requests', 'answer', name, 'a-1', '--abort', '--by', 'person', cwd=folder, env=env)
        over = h.wait_until_over(folder, env, name)
        self.assertEqual((over['state'], over['verdict']), ('aborted', 'stopped_by_human'))
        after = h.pangoloop('requests', 'wait', name, '--json', cwd=folder, env=env)
        self.assertEqual(after.returncode, 3)
        self.assertEqual(json.loads(after.stdout), {'run_over': True, 'run': name, 'verdict': 'stopped_by_human'})
        self.assertEqual(json.loads(h.pangoloop('requests', 'list', name, '--json', cwd=folder, env=env).stdout.splitlines()[0])
                         ['state'], 'answered')

    def test_list_json_of_a_run_without_requests_prints_nothing(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        self.assertEqual(h.pangoloop('run', 'checks', '--name', 'one', cwd=folder, env=env).returncode, 0)
        done = h.pangoloop('requests', 'list', 'one', '--json', cwd=folder, env=env)
        self.assertEqual((done.returncode, done.stdout), (0, ''))
        self.assertEqual(h.pangoloop('requests', 'wait', 'one', '--json', cwd=folder, env=env).returncode, 3)

    def test_a_lock_decides_whether_an_engine_lives_not_a_search_of_the_machine(self):
        from pangoloop import requests
        folder = support.temp_dir(self)
        with open(os.path.join(folder, 'lock.json'), 'w') as handle:
            json.dump({'pid': os.getpid(), 'process_started': ''}, handle)
        self.assertTrue(requests.engine_alive('whatever-name', folder))
        with open(os.path.join(folder, 'lock.json'), 'w') as handle:
            json.dump({'pid': 2 ** 22 + 1, 'process_started': ''}, handle)
        self.assertFalse(requests.engine_alive('whatever-name', folder))


class TwoProjects(unittest.TestCase):
    def test_two_projects_run_the_toy_at_once_each_with_its_own_page_and_nothing_of_the_other(self):
        a, env_a = h.make_project(self, 'alpha')
        b, env_b = h.make_project(self, 'beta')
        for folder in (a, b):
            h.write_plan(folder, 'toy.md', open(h.TOY_PLAN).read())
        started = {}
        for key, folder, env in (('a', a, env_a), ('b', b, env_b)):
            done = h.pangoloop('run', 'toy', '--detach', '--view', '--json', cwd=folder, env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            started[key] = json.loads(done.stdout)
        self.assertNotEqual(started['a']['page'], started['b']['page'])
        self.assertNotEqual(started['a']['pid'], started['b']['pid'])
        for key, folder, env in (('a', a, env_a), ('b', b, env_b)):
            over = h.wait_until_over(folder, env, started[key]['run'], seconds=120)
            self.assertEqual((over['state'], over['verdict']), ('verified', 'verified'), key)
        for key, folder, other in (('a', a, 'beta'), ('b', b, 'alpha')):
            run = started[key]['folder']
            self.assertTrue(run.startswith(os.path.realpath(folder) + os.sep), 'the run folder is in its own project')
            listed = [r['name'] for r in json.loads(h.fetch(started[key]['page'] + 'api/runs')[1])['runs']]
            self.assertEqual(listed, [started[key]['run']], 'each page shows only its own project\'s run')
            for root, _, files in os.walk(os.path.join(os.path.realpath(folder), '.pangoloop')):
                for name in files:
                    with open(os.path.join(root, name), errors='replace') as handle:
                        self.assertNotIn(f'/{other}/', handle.read(), os.path.join(root, name))
            self.assertEqual(h.run_state(folder, env_a if key == 'a' else env_b)['page'], started[key]['page'])
            result = h.read_json(os.path.join(run, 'result.json'))
            self.assertEqual(sorted(result['nodes']), ['accept', 'add-double', 'baseline', 'run-tests'])
        with open(os.path.join(a, 'calc.py')) as handle:
            self.assertIn('add', handle.read())
        a_log = open(os.path.join(os.path.dirname(a), 'alpha-stub.log')).read()
        self.assertNotIn('/beta', a_log)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
