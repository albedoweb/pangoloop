"""The configuration file: sources and precedence (environment, file, default), the checks, `pangoloop config`, the two
example files, and the interface changes of step 3 on the Hermes side (parse_event, the profile, the refusal)."""
import ast
import json
import os
import subprocess
import sys
import unittest

import support
from pangoloop import config, plan
from pangoloop.runners import get_runner

EXAMPLES = os.path.join(support.ROOT, 'examples')
PRESETS = os.path.join(EXAMPLES, 'presets')


def write(root: str, text: str, name: str = 'pangoloop.toml') -> str:
    path = os.path.join(root, name)
    with open(path, 'w') as handle:
        handle.write(text)
    return path


class Load(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)

    def load(self, text=None, environ=None, name='pangoloop.toml'):
        env = dict(environ or {})
        if text is not None:
            path = write(self.home, text, name)
            if name != 'pangoloop.toml':
                env['PANGOLOOP_CONFIG'] = path
        return config.load(env, self.home)

    def test_no_file_no_variables_is_what_it_was(self):
        cfg = self.load()
        self.assertEqual((cfg.path, cfg.runner), (None, 'hermes'))
        routes, default = cfg.routes_table()
        self.assertIs(routes, plan.ROUTES)
        self.assertIs(default, plan.DEFAULT_ROUTE)
        self.assertEqual(cfg.limits, {'max_parallel': 3, 'max_fix_rounds': 2, 'node_limit_seconds': 3600, 'max_turns': 200,
                                      'acceptance_seconds': 1800, 'integration_check_seconds': 900,
                                      'human_wait_seconds': 86400, 'coordinator_wait_seconds': 900.0,
                                      'rulings_chars': 12000, 'max_cost_usd': None})
        self.assertEqual(set(cfg.sources.values()), {'default'})

    def test_file_in_the_current_folder(self):
        cfg = self.load('[runner]\nname = "claude-code"\n[routes]\nreview = "reviewer-model"\n[limits]\nmax_turns = 50\n')
        self.assertEqual(cfg.path, os.path.join(self.home, 'pangoloop.toml'))
        self.assertEqual((cfg.runner, cfg.routes['review'], cfg.default_route), ('claude-code', ('reviewer-model', 'anthropic'), ('default-model', 'anthropic')))
        self.assertEqual(cfg.routes['ui'], ('ui-model', 'anthropic'), 'the built-in routes of the runner stay where the file is silent')
        self.assertEqual(cfg.limits['max_turns'], 50)
        self.assertEqual(cfg.sources['runner.name'], f'file {cfg.path}')
        self.assertEqual(cfg.sources['routes.review'], f'file {cfg.path}')
        self.assertEqual(cfg.sources['routes.ui'], 'default')
        self.assertEqual(cfg.sources['limits.max_parallel'], 'default')

    def test_environment_wins_over_the_file(self):
        cfg = self.load('[runner]\nname = "claude-code"\n[limits]\nmax_turns = 50\nmax_parallel = 2\n',
                        {'PANGOLOOP_RUNNER': 'hermes', 'SG_MAX_TURNS': '7'})
        self.assertEqual((cfg.runner, cfg.limits['max_turns'], cfg.limits['max_parallel']), ('hermes', 7, 2))
        self.assertEqual((cfg.sources['runner.name'], cfg.sources['limits.max_turns']), ('env PANGOLOOP_RUNNER', 'env SG_MAX_TURNS'))
        self.assertTrue(cfg.sources['limits.max_parallel'].startswith('file '))

    def test_hermes_routes_from_the_file_replace_only_what_they_name(self):
        cfg = self.load('[routes]\ndefault = "m@p"\nreview = "r@q"\n')
        routes, default = cfg.routes_table()
        self.assertEqual((default, routes['review'], routes['ui']), (('m', 'p'), ('r', 'q'), plan.ROUTES['ui']))

    def test_explicit_file_and_none(self):
        cfg = self.load('[limits]\nmax_turns = 9\n', name='other.toml')
        self.assertEqual(cfg.limits['max_turns'], 9)
        self.assertIsNone(config.load({'PANGOLOOP_CONFIG': 'none'}, self.home).path)
        with self.assertRaises(SystemExit):
            config.load({'PANGOLOOP_CONFIG': os.path.join(self.home, 'missing.toml')}, self.home)

    def test_mistakes_stop_before_anything_runs(self):
        for text, word in (('[runnr]\nname = "x"\n', 'unknown tables'), ('[runner]\nname = "nope"\n', 'the runners are'),
                           ('[runner]\nnam = "hermes"\n', 'unknown keys'), ('[limits]\nmax_turns = "many"\n', 'not a number'),
                           ('[limits]\nturns = 3\n', 'unknown keys'),
                           ('[runner]\nname = "claude-code"\n[routes]\nreview = "model@other"\n', 'anthropic'),
                           ('[routes]\nreview = "justamodel"\n', 'needs "model@provider"'), ('[routes]\nx = 3\n', 'expected'),
                           ('this is not toml', 'cannot read')):
            with self.assertRaises(SystemExit, msg=text) as stop:
                self.load(text)
            self.assertIn(word, str(stop.exception), text)

    def test_runner_tables_reach_the_runner(self):
        cfg = self.load('[runner.hermes]\nprofile = "mine"\n[runner.claude_code]\ndeny = ["Bash(x)"]\n')
        self.assertEqual(get_runner('hermes', cfg.runner_settings['hermes']).profile, 'mine')
        self.assertEqual(get_runner('hermes').profile, '')   # no private default: Hermes's own
        self.assertIn('runner.hermes.profile', cfg.sources)
        with self.assertRaises(SystemExit):
            get_runner('hermes', {'profil': 'x'})
        cmd = get_runner('hermes', {'profile': 'mine'}).command('s', 'm', 'p', '/p', 'file', 1)
        self.assertEqual(cmd[:3], ['hermes', '-p', 'mine'])

    def test_describe_says_where_each_value_came_from(self):
        text = self.load('[limits]\nmax_turns = 50\n', {'SG_MAX_PARALLEL': '5'}).describe()
        self.assertIn('config file: ' + os.path.join(self.home, 'pangoloop.toml'), text)
        self.assertRegex(text, r'limits\.max_turns = 50 +\[file ')
        self.assertRegex(text, r"limits\.max_parallel = 5 +\[env SG_MAX_PARALLEL\]")
        self.assertRegex(text, r"limits\.acceptance_seconds = 1800 +\[default\]")
        self.assertRegex(text, r"runner\.name = 'hermes' +\[default\]")


class Examples(unittest.TestCase):
    def test_the_hermes_example_is_todays_configuration(self):
        cfg = config.load({'PANGOLOOP_CONFIG': os.path.join(PRESETS, 'hermes-mixed.toml')}, '/')
        plain = config.load({}, '/')
        self.assertEqual(cfg.runner, plain.runner)
        routes, default = cfg.routes_table()
        self.assertEqual(sorted(routes), sorted(plan.ROUTES), 'the preset names the routes the built-in table has')
        self.assertTrue(all(plan.is_placeholder(route) for route in list(routes.values()) + [default]),
                        'a preset is copied and edited: its models are placeholders until the user writes their own')
        self.assertEqual(cfg.runner_settings['hermes'], {})

    def test_the_claude_code_example(self):
        cfg = config.load({'PANGOLOOP_CONFIG': os.path.join(PRESETS, 'claude-code-only.toml')}, '/')
        self.assertEqual(cfg.runner, 'claude-code')
        self.assertEqual(sorted(cfg.routes), ['review', 'simple-work', 'ui'])
        self.assertFalse(any(plan.is_placeholder(route) for route in list(cfg.routes.values()) + [cfg.default_route]),
                         'this preset names real aliases and runs as it is')
        self.assertTrue(all(route[1] == 'anthropic' for route in cfg.routes.values()))
        self.assertEqual(cfg.limits['max_turns'], 100)

    def test_builtin_claude_routes_are_placeholders_of_the_provider_anthropic(self):
        built_in = config.load({'PANGOLOOP_RUNNER': 'claude-code'}, '/')
        self.assertEqual(built_in.routes, {k: (m, 'anthropic') for k, (m, _) in plan.ROUTES.items()})
        self.assertEqual(built_in.default_route, (plan.DEFAULT_ROUTE[0], 'anthropic'))
        self.assertTrue(all(plan.is_placeholder(route) for route in list(built_in.routes.values()) + [built_in.default_route]))


class Readers(unittest.TestCase):
    """`[readers]`: the names of the readers of `preflight` and the models behind them are the user's; none is built in."""

    def load(self, text, environ=None):
        home = support.temp_dir(self)
        write(home, text)
        return config.load(dict(environ or {}), home)

    def test_no_table_no_readers(self):
        self.assertEqual(config.load({}, '/').readers, {})

    def test_a_name_maps_to_model_at_provider_and_perhaps_a_runner(self):
        cfg = self.load('[readers]\nquick = "model-a@provider-a"\ncareful = { model = "model-b", runner = "claude-code" }\n')
        self.assertEqual(cfg.readers, {'quick': ('model-a', 'provider-a', None), 'careful': ('model-b', 'anthropic', 'claude-code')})
        self.assertTrue(cfg.sources['readers.quick'].startswith('file '))
        text = cfg.describe()
        self.assertIn('readers.quick', text)
        self.assertIn('model-b@anthropic (claude-code)', text)

    def test_the_providers_rule_of_the_runner_applies_to_a_reader_too(self):
        for text, word in (('[readers]\nq = "model-a"\n', 'needs "model@provider"'),
                           ('[readers]\nq = { model = "m@other", runner = "claude-code" }\n', 'anthropic'),
                           ('[readers]\nq = { model = "m@p", runner = "nope" }\n', 'the runners are'),
                           ('[readers]\nq = { model = "m@p", extra = 1 }\n', 'unknown keys'),
                           ('[readers]\n"a b" = "m@p"\n', "a reader's name"),
                           ('[readers]\nq = 5\n', 'expected "model@provider"')):
            with self.assertRaises(SystemExit) as stop:
                self.load(text)
            self.assertIn(word, str(stop.exception), text)

    def test_the_code_names_no_reader_and_no_model(self):
        with open(os.path.join(support.ROOT, 'pangoloop', 'preflight.py')) as handle:
            code = handle.read().lower()
        for word in ('luna', 'gpt', 'codex', 'openai'):
            self.assertNotIn(word, code)


class Presets(unittest.TestCase):
    """The three configuration files a user copies and edits."""
    NAMES = ('claude-code-only.toml', 'hermes-mixed.toml', 'one-model.toml')

    def test_each_loads_and_prints_through_the_command(self):
        for name in self.NAMES:
            path = os.path.join(PRESETS, name)
            cfg = config.load({'PANGOLOOP_CONFIG': path}, '/')
            self.assertTrue(cfg.readers, name)
            self.assertIn('review', cfg.routes, name)
            done = subprocess.run([sys.executable, '-m', 'pangoloop', 'config', '--config', path], capture_output=True, text=True,
                                  env=support.clean_environment(), cwd=support.ROOT, timeout=60)
            self.assertEqual(done.returncode, 0, (name, done.stderr))
            self.assertIn('readers.quick', done.stdout, name)
            self.assertIn('routes.default', done.stdout, name)

    def test_each_says_at_its_top_when_to_use_it_and_what_to_replace(self):
        for name in self.NAMES:
            with open(os.path.join(PRESETS, name)) as handle:
                head = []
                for line in handle:
                    if not line.startswith('#'):
                        break
                    head.append(line)
            text = ''.join(head)
            self.assertTrue(text.startswith('# Preset:'), name)
            self.assertIn('Use it ', text, name)
            self.assertIn('What to replace', text, name)

    def test_the_presets_of_other_vendors_are_placeholders_that_stop_a_run(self):
        for name in ('hermes-mixed.toml', 'one-model.toml'):
            cfg = config.load({'PANGOLOOP_CONFIG': os.path.join(PRESETS, name)}, '/')
            routes, default = cfg.routes_table()
            self.assertTrue(all(plan.is_placeholder(route) for route in list(routes.values()) + [default]), name)


class Command(unittest.TestCase):
    def pangoloop(self, *args, cwd=None, **env):
        return subprocess.run([sys.executable, '-m', 'pangoloop', *args], capture_output=True, text=True,
                              env=support.clean_environment(**env), cwd=cwd or support.ROOT, timeout=60)

    def test_config_command_prints_the_effective_configuration(self):
        done = self.pangoloop('config', '--config', os.path.join(PRESETS, 'claude-code-only.toml'), SG_MAX_PARALLEL='4')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("runner.name = 'claude-code'", done.stdout)
        self.assertRegex(done.stdout, r"routes\.review = '[^']+@anthropic'")
        self.assertRegex(done.stdout, r'limits\.max_parallel = 4 +\[env SG_MAX_PARALLEL\]')

    def test_config_in_the_current_folder_and_the_run_command_reads_it(self):
        home = support.temp_dir(self)
        write(home, '[limits]\nmax_turns = 11\n')
        done = self.pangoloop('config', cwd=home, PANGOLOOP_CONFIG='')
        self.assertRegex(done.stdout, r'limits\.max_turns = 11 +\[file ')
        probe = subprocess.run([sys.executable, '-c', 'from pangoloop import graph; print(graph.MAX_TURNS)'],
                               capture_output=True, text=True, env=support.clean_environment(PYTHONPATH=support.ROOT, PANGOLOOP_CONFIG=''), cwd=home)
        self.assertEqual(probe.stdout.strip(), '11', probe.stderr)

    def test_a_bad_file_is_exit_2_with_the_message(self):
        home = support.temp_dir(self)
        path = write(home, '[runner]\nname = "nope"\n', 'bad.toml')
        for args in (('config', '--config', path), ('run', 'plan.md', home, 'x', '--config', path)):
            done = self.pangoloop(*args, cwd=home)
            self.assertEqual(done.returncode, 2, args)
            self.assertIn('the runners are hermes, claude-code', done.stderr)


class HermesInterface(unittest.TestCase):
    """The viewer's Hermes parsing moved into the runner; what the page shows for Hermes runs did not change."""

    def parse(self, event):
        return get_runner('hermes').parse_event(json.dumps(event))

    def test_records_of_the_hermes_stream(self):
        self.assertEqual(self.parse({'type': 'text', 'text': 'hi', 'timestamp': 5}), {'kind': 'text', 'text': 'hi', 'ts': 5})
        tool = self.parse({'type': 'tool_use', 'name': 'terminal', 'input': {'command': 'ls -la'}})
        self.assertEqual((tool['kind'], tool['tool'], tool['input'], tool['command']), ('tool', 'terminal', 'ls -la', 'ls -la'))
        other = self.parse({'type': 'tool_use', 'name': 'read_file', 'input': {'path': 'a.txt', 'content': 'xyz'}})
        self.assertEqual((other['input'], other['command']), ('a.txt  (3 chars)', None))
        result = self.parse({'type': 'tool_result', 'name': 'terminal', 'is_error': True, 'duration_ms': 12,
                             'output': json.dumps({'output': 'boom', 'exit_code': 2})})
        self.assertEqual((result['kind'], result['error'], result['ms'], result['text']), ('tool_result', True, 12, 'exit 2: boom'))
        start = self.parse({'type': 'system', 'subtype': 'init', 'model': 'm1', 'timestamp': 7})
        self.assertEqual((start['kind'], start['model'], start['ts'], start['text']), ('start', 'm1', 7, 'session started on m1'))
        end = self.parse({'type': 'result', 'text': 'x', 'exit_code': 0, 'tokens': {'input': 1, 'output': 2}})
        self.assertEqual((end['kind'], end['exit_code'], end['tokens'], end['text']), ('result', 0, {'input': 1, 'output': 2}, 'finished, exit code 0'))
        self.assertIsNone(self.parse({'type': 'nonsense'}))
        self.assertIsNone(get_runner('hermes').parse_event('not json'))

    def test_usage_is_the_tokens_of_the_result(self):
        home = support.temp_dir(self)
        path = os.path.join(home, 'e.jsonl')
        with open(path, 'w') as handle:
            handle.write(json.dumps({'type': 'result', 'text': 'x', 'exit_code': 0,
                                     'tokens': {'input': 10, 'output': 2, 'cache_read': 5, 'cache_write': 0}}) + '\n')
        self.assertEqual(get_runner('hermes').read_events(path, 0).usage, {'input': 10, 'output': 2, 'cache_read': 5, 'cache_write': 0})

    def test_nothing_outside_the_runners_knows_a_harness(self):
        """The engine, the viewer and the preflight have no harness's name, flag or event type in their code."""
        words = {'graph.py': ('stream-json', 'active session limit', '--oneshot', 'hermes', 'claude-code'),
                 'view.py': ("'tool_use'", 'stream-json', "'terminal'", "'hermes'", 'claude-code'),
                 'preflight.py': ("'tool_use'", 'stream-json', '--oneshot', "'terminal'", 'claude-code')}
        for name, forbidden in words.items():
            with open(os.path.join(support.ROOT, 'pangoloop', name)) as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):   # docstrings are prose, not code
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(node, clean=False):
                    node.body = node.body[1:] or [ast.Pass()]
            text = ast.unparse(tree)
            for word in forbidden:
                self.assertNotIn(word, text, f'{name}: {word}')

    def test_a_script_node_is_read_with_the_script_stream_in_a_claude_run(self):
        from pangoloop import view
        runner = get_runner('script')
        found = runner.parse_event(json.dumps({'type': 'tool_use', 'name': 'run', 'input': {'command': 'run `true`'}}))
        self.assertEqual((found['kind'], found['tool']), ('tool', 'run'))
        self.assertEqual(runner.name, 'script')
        with self.assertRaises(NotImplementedError):
            runner.command()


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
