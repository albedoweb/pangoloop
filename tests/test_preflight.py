"""The preflight: the prompt is the shell script's, word for word; readers run in parallel through the runner (a stub
here, never a model); the files and the summary are as described."""
import contextlib
import io
import os
import re
import threading
import time
import unittest
from unittest import mock

import support
from pangoloop import config, preflight
from pangoloop.runners import RunResult


# the `[readers]` table of a configuration file, as config.load() gives it: name -> (model, provider, runner or None)
TABLE = {'quick': ('model-a', 'provider-a', None), 'careful': ('model-b', 'provider-b', None)}


class StubRunner:
    name = 'stub'

    def __init__(self, answers=None, delay=0.3):
        self.calls, self.answers, self.delay = [], answers or {}, delay
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def run(self, session, model, provider, prompt, cwd, toolsets, max_turns, folder, timeout=None):
        with self.lock:
            self.calls.append(dict(session=session, model=model, provider=provider, prompt=prompt, toolsets=toolsets,
                                   max_turns=max_turns, folder=folder))
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        os.makedirs(folder, exist_ok=True)
        return RunResult(0, self.answers.get(model, f'COUNT: 1\n1. CONTRADICTION by {model}\n'), 'rep-' + model)


class Prompt(unittest.TestCase):
    def test_prompt_is_the_recorded_text_word_for_word(self):
        # tests/fixtures/preflight/prompt.txt is the prompt of the shell script this module was ported from, kept as it was
        with open(os.path.join(support.ROOT, 'tests', 'fixtures', 'preflight', 'prompt.txt')) as handle:
            body = handle.read()
        self.assertEqual(preflight.PROMPT, body)
        self.assertEqual(preflight.prompt_for('/x/plan.md'), body.replace('$FILE', '/x/plan.md'))
        self.assertIn('Read the file /x/plan.md in full', preflight.prompt_for('/x/plan.md'))

    def test_parameters_are_the_shell_scripts(self):
        self.assertEqual((preflight.TOOLSETS, preflight.MAX_TURNS), ('file', 40))
        self.assertFalse(hasattr(preflight, 'READERS'), 'no reader name is built into the code')


class Readers(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.file = os.path.join(self.home, 'plan.md')
        with open(self.file, 'w') as handle:
            handle.write('# a plan\n')

    def test_two_readers_in_parallel_with_summary(self):
        runner = StubRunner(answers={'model-a': 'COUNT: 2\nA\nB\n', 'model-b': 'Intro\nCOUNT: 0\nnone\n'})
        done = preflight.preflight(self.file, ['quick', 'careful'], f'{self.home}/out', runner=runner, table=TABLE)
        self.assertEqual(runner.peak, 2, 'the readers ran side by side')
        self.assertEqual(sorted((c['model'], c['provider']) for c in runner.calls),
                         [('model-a', 'provider-a'), ('model-b', 'provider-b')])
        for call in runner.calls:
            self.assertEqual((call['toolsets'], call['max_turns']), ('file', 40))
            self.assertEqual(call['prompt'], preflight.prompt_for(self.file))
            self.assertTrue(call['session'].startswith('preflight-plan-md-'))
        self.assertEqual(open(f'{self.home}/out/quick.md').read(), 'COUNT: 2\nA\nB\n')
        self.assertEqual(open(f'{self.home}/out/careful.md').read(), 'Intro\nCOUNT: 0\nnone\n')
        summary = open(f'{self.home}/out/summary.md').read()
        self.assertEqual(done['summary'], f'{self.home}/out/summary.md')
        head, rest = summary.split('\n## quick', 1)
        self.assertRegex(head, r'- quick: model model-a \(provider-a\), \d+ s, COUNT: 2')
        self.assertRegex(head, r'- careful: model model-b \(provider-b\), \d+ s, COUNT: 0')
        self.assertLess(rest.index('A\nB'), rest.index('## careful'))
        self.assertIn('Intro\nCOUNT: 0\nnone', rest)

    def test_model_at_provider_works_without_a_table(self):
        runner = StubRunner(delay=0)
        preflight.preflight(self.file, ['some-model@some-provider'], f'{self.home}/o1', runner=runner, table={})
        self.assertEqual((runner.calls[0]['model'], runner.calls[0]['provider']), ('some-model', 'some-provider'))
        self.assertTrue(os.path.exists(f'{self.home}/o1/some-model.md'))

    def test_with_no_reader_every_reader_of_the_table_runs(self):
        runner = StubRunner(delay=0)
        done = preflight.preflight(self.file, [], f'{self.home}/o2', runner=runner, table=TABLE)
        self.assertEqual([item['label'] for item in done['readers']], ['quick', 'careful'])
        self.assertEqual(sorted(call['model'] for call in runner.calls), ['model-a', 'model-b'])

    def test_with_no_reader_and_an_empty_table_it_says_what_to_write(self):
        with self.assertRaises(ValueError) as stop:
            preflight.preflight(self.file, [], f'{self.home}/o5', runner=StubRunner(), table={})
        self.assertIn('[readers]', str(stop.exception))
        self.assertIn('model@provider', str(stop.exception))
        self.assertFalse(os.path.exists(f'{self.home}/o5'))

    def test_a_reader_may_have_a_runner_of_its_own(self):
        table = {'quick': ('model-a', 'provider-a', None), 'cc': ('model-c', 'anthropic', 'claude-code')}
        asked = []
        stub = StubRunner(delay=0)
        cfg = config.load({}, self.home)
        with mock.patch.object(preflight.config, 'load', return_value=cfg), \
                mock.patch.object(preflight, 'get_runner', side_effect=lambda name, settings=None: (asked.append(name), stub)[1]):
            preflight.preflight(self.file, [], f'{self.home}/o6', table=table)
        self.assertEqual(sorted(asked), ['claude-code', 'hermes'])

    def test_same_reader_twice_gets_two_files(self):
        runner = StubRunner(delay=0)
        preflight.preflight(self.file, ['quick', 'quick'], f'{self.home}/o3', runner=runner, table=TABLE)
        self.assertTrue(os.path.exists(f'{self.home}/o3/quick.md') and os.path.exists(f'{self.home}/o3/quick-2.md'))

    def test_bad_reader_and_missing_file(self):
        with self.assertRaises(ValueError) as stop:
            preflight.parse_reader('nobody', TABLE)
        self.assertIn('careful, quick', str(stop.exception), 'the error names the readers of the configuration')
        with self.assertRaises(FileNotFoundError):
            preflight.preflight(f'{self.home}/missing.md', ['quick'], f'{self.home}/o4', runner=StubRunner(), table=TABLE)

    def test_no_count_line_is_said(self):
        self.assertEqual(preflight.count_line('hello\nworld'), '(no COUNT line)')
        self.assertEqual(preflight.count_line('**COUNT: 3**\nx'), '**COUNT: 3**')


class ReadersFromTheConfiguration(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.file = os.path.join(self.home, 'plan.md')
        with open(self.file, 'w') as handle:
            handle.write('# a plan\n')

    def configure(self, text):
        path = os.path.join(self.home, 'pangoloop.toml')
        with open(path, 'w') as handle:
            handle.write(text)
        return mock.patch.dict(os.environ, {'PANGOLOOP_CONFIG': path})

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = preflight.main([self.file, '--out', f'{self.home}/pf', *args])
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_an_empty_table_stops_with_exit_2_and_a_sentence(self):
        with self.configure('[limits]\nmax_turns = 5\n'):
            code, _, err = self.main()
        self.assertEqual(code, 2)
        self.assertIn('Write a [readers] table in the configuration file', err)
        self.assertFalse(os.path.exists(f'{self.home}/pf'))

    def test_the_names_come_from_the_table_and_run_through_the_runner(self):
        calls = []
        runner = type('R', (), {'name': 'stub'})()
        runner.run = lambda **kwargs: (calls.append(kwargs), RunResult(0, 'COUNT: 0\nnone\n', 'm'))[1]
        with self.configure('[readers]\nquick = "model-a@provider-a"\ncareful = "model-b@provider-b"\n'), \
                mock.patch.object(preflight, 'get_runner', return_value=runner):
            code, out, _ = self.main('--reader', 'careful')
            both, _, _ = self.main('--out', f'{self.home}/pf-all')
        self.assertEqual((code, both), (0, 0))
        self.assertEqual([(c['model'], c['provider']) for c in calls],
                         [('model-b', 'provider-b'), ('model-a', 'provider-a'), ('model-b', 'provider-b')])
        self.assertIn('careful', out)

    def test_an_unknown_name_is_refused_with_the_known_ones(self):
        with self.configure('[readers]\nquick = "model-a@provider-a"\n'):
            code, _, err = self.main('--reader', 'nobody')
        self.assertEqual(code, 2)
        self.assertIn('quick', err)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
