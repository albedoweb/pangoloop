"""The command line (every command's --help runs) and the viewer serving a fake run folder."""
import json
import subprocess
import sys
import threading
import unittest
import urllib.request

import support


def pangoloop(*args, **kwargs):
    return subprocess.run([sys.executable, '-m', 'pangoloop', *args], capture_output=True, text=True,
                          env=support.clean_environment(**kwargs.pop('env', {})), cwd=support.ROOT, timeout=60, **kwargs)


class Help(unittest.TestCase):
    def test_every_command_has_help_with_an_example(self):
        for args in (['--help'], ['run', '--help'], ['view', '--help'], ['preflight', '--help'], ['requests', '--help'],
                     ['requests', 'list', '--help'], ['requests', 'wait', '--help'], ['requests', 'answer', '--help'], ['config', '--help']):
            done = pangoloop(*args)
            self.assertEqual(done.returncode, 0, (args, done.stderr))
            self.assertIn('usage', done.stdout.lower(), args)
        for args in (['run', '--help'], ['view', '--help'], ['preflight', '--help'], ['requests', '--help'], ['config', '--help']):
            self.assertIn('example', pangoloop(*args).stdout, args)

    def test_version_and_unknown_command(self):
        self.assertEqual(pangoloop('version').stdout.strip(), 'pangoloop 0.1.0')
        self.assertEqual(pangoloop('nonesuch').returncode, 2)
        self.assertEqual(pangoloop().returncode, 2)
        self.assertEqual(pangoloop('run', 'only-one-argument').returncode, 2)


class Viewer(unittest.TestCase):
    def test_serves_the_page_and_the_runs(self):
        from pangoloop import view
        home = support.temp_dir(self)
        support.make_run(home, 'fake-run', requests={'a-1': support.request_info()})
        saved = (view.RUNS, view.NOTIFY)
        view.RUNS, view.NOTIFY = home, False
        self.addCleanup(lambda: (setattr(view, 'RUNS', saved[0]), setattr(view, 'NOTIFY', saved[1])))
        server = view.Server(('127.0.0.1', 0), view.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        page = urllib.request.urlopen(base + '/').read().decode()
        self.assertIn('<html', page)
        runs = json.load(urllib.request.urlopen(base + '/api/runs'))['runs']
        self.assertEqual([(r['name'], r['state']) for r in runs], [('fake-run', 'waiting for you')])
        run = json.load(urllib.request.urlopen(base + '/api/run/fake-run'))
        self.assertEqual(run['nodes'][0]['state'], 'waiting for human')

    def test_runs_folder_variables(self):
        for env, expected in (({'PANGOLOOP_RUNS': '/tmp/pg-a'}, '/tmp/pg-a'),
                              ({'PANGOLOOP_RUNS': '/tmp/pg-a', 'RUN_VIEW_RUNS': '/tmp/pg-b'}, '/tmp/pg-b')):
            done = subprocess.run([sys.executable, '-c', 'from pangoloop import view; print(view.RUNS)'],
                                  capture_output=True, text=True, env=support.clean_environment(**env), cwd=support.ROOT)
            self.assertEqual(done.stdout.strip(), expected, done.stderr)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
