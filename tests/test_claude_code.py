"""The Claude Code runner: its command line, and its reading of the six streams recorded on November 18, 2026 with
Claude Code 2.1.327 (tests/fixtures/claude_code/<case>/, see the README there). The reading tests rest on RECORDED
output; the two tests marked DOCUMENTED rest on the documentation only (no such failure was recorded)."""
import json
import os
import unittest

import support
from pangoloop.runners import get_runner
from pangoloop.runners import claude_code
from pangoloop.runners.claude_code import ClaudeCodeRunner, DEFAULT_DENY, tools_for, unmapped_toolsets

FIXTURES = os.path.join(support.ROOT, 'tests', 'fixtures', 'claude_code')


def case(name: str) -> str:
    return os.path.join(FIXTURES, name)


def events(name: str) -> list:
    with open(os.path.join(case(name), 'events.jsonl')) as handle:
        return [json.loads(line) for line in handle]


def exit_code(name: str) -> int:
    with open(os.path.join(case(name), 'exit_code')) as handle:
        return int(handle.read())


def read(name: str):
    return ClaudeCodeRunner().read_events(os.path.join(case(name), 'events.jsonl'), exit_code(name))


def records(name: str) -> list:
    runner, out = ClaudeCodeRunner(), []
    with open(os.path.join(case(name), 'events.jsonl')) as handle:
        for line in handle:
            out += runner.parse_events(line)
    return out


class Command(unittest.TestCase):
    def build(self, runner=None, **kwargs):
        home = support.temp_dir(self)
        prompt = os.path.join(home, 'prompt.txt')
        with open(prompt, 'w') as handle:
            handle.write('Before anything else read the plan.\n\nDo the node.')
        arguments = dict(session='sg-run-1a-01-a', model='model-s', provider='anthropic', prompt_file=prompt,
                         toolsets='file,terminal,omh-lsp', max_turns='200')
        arguments.update(kwargs)
        return (runner or ClaudeCodeRunner()).command(**arguments)

    def value(self, command, flag):
        return command[command.index(flag) + 1]

    def test_the_headless_command(self):
        command = self.build()
        self.assertEqual(command[:3], ['claude', '-p', 'Before anything else read the plan.\n\nDo the node.'])
        self.assertEqual((self.value(command, '--model'), self.value(command, '--max-turns'),
                          self.value(command, '--output-format')), ('model-s', '200', 'stream-json'))
        self.assertIn('--verbose', command)   # stream-json needs it with -p
        self.assertEqual((self.value(command, '--permission-mode'), self.value(command, '--permission-prompts')),
                         ('dontAsk', 'none'))
        self.assertEqual(self.value(command, '--tools'), 'Read,Edit,Write,Glob,Grep,Bash')
        self.assertEqual(self.value(command, '--allowedTools'), 'Read,Edit,Write,Glob,Grep,Bash')
        self.assertIn('--strict-mcp-config', command)
        self.assertIn('--no-session-persistence', command)   # a fresh session, kept nowhere
        self.assertNotIn('--resume', command)
        self.assertNotIn('--dangerously-skip-permissions', command)
        self.assertNotIn('--bare', command)   # --bare reads no login: only ANTHROPIC_API_KEY

    def test_it_is_the_command_that_was_recorded(self):
        """The recorded calls were made with this runner's command(): same words, but the prompt and the binary."""
        with open(os.path.join(case('4-denied'), 'command.json')) as handle:
            recorded = json.load(handle)
        command = ClaudeCodeRunner().command('s', 'model-h', 'anthropic', os.path.join(case('4-denied'), 'prompt.txt'),
                                             'file,terminal,omh-lsp', 6)
        self.assertEqual(command[3:], recorded[3:])

    def test_each_call_is_a_new_session_unless_asked_to_resume(self):
        first = self.build(session='a')
        self.assertEqual(first, self.build(session='b'), 'nothing in the command names a session: none is kept')
        keeping = ClaudeCodeRunner(persist_sessions=True)
        self.assertNotEqual(self.build(keeping, session='a'), self.build(keeping, session='b'))
        persisted = self.build(ClaudeCodeRunner(persist_sessions=True), session='a')
        self.assertEqual(self.value(persisted, '--session-id'), claude_code.session_uuid('a'))
        self.assertNotIn('--no-session-persistence', persisted)
        resumed = self.build(ClaudeCodeRunner(persist_sessions=True), session='a', resume=True)
        self.assertEqual(self.value(resumed, '--resume'), claude_code.session_uuid('a'))
        self.assertNotIn('--session-id', resumed)

    def test_the_provider_must_be_anthropic(self):
        with self.assertRaises(SystemExit) as stop:
            self.build(model='cheap-model', provider='cheap-provider')
        self.assertIn('anthropic', str(stop.exception))
        self.assertEqual(self.value(self.build(model='model-o-5-5', provider=''), '--model'), 'model-o-5-5')

    def test_tool_sets_are_mapped(self):
        self.assertEqual(tools_for('file,terminal,omh-lsp'), ['Read', 'Edit', 'Write', 'Glob', 'Grep', 'Bash'])
        self.assertEqual(tools_for('file,terminal,omh-lsp,vision'), ['Read', 'Edit', 'Write', 'Glob', 'Grep', 'Bash'])
        self.assertEqual(tools_for('file'), ['Read', 'Edit', 'Write', 'Glob', 'Grep'])
        self.assertEqual(unmapped_toolsets('file,terminal,omh-lsp,vision,web'), ['omh-lsp', 'web'])
        command = self.build(toolsets='file')   # no terminal set: no shell at all
        self.assertEqual(self.value(command, '--tools'), 'Read,Edit,Write,Glob,Grep')
        self.assertNotIn('Bash', self.value(command, '--allowedTools'))

    def test_the_deny_list_is_on_by_default_and_complete(self):
        deny = self.value(self.build(), '--disallowedTools').split(',')
        self.assertEqual(deny, list(DEFAULT_DENY))
        text = ' '.join(deny)
        for needle in ('git push', 'rm -rf', 'sudo', '| sh', 'npm publish', 'twine upload', 'ssh', 'scp', 'kubectl',
                       'terraform apply', 'aws', 'gcloud', 'docker push'):
            self.assertIn(needle, text, needle)
        self.assertTrue(all(item.startswith('Bash(') and item.endswith(')') for item in deny))

    def test_configuration_adds_and_can_turn_the_default_off(self):
        runner = ClaudeCodeRunner().configured({'deny': ['Bash(make deploy*)'], 'allow': ['Bash(make test)'],
                                                'flags': ['--effort', 'low']})
        command = self.build(runner)
        self.assertEqual(self.value(command, '--disallowedTools').split(',')[-1], 'Bash(make deploy*)')
        self.assertIn('Bash(git push *)', self.value(command, '--disallowedTools'))
        self.assertEqual(self.value(command, '--allowedTools').split(',')[-1], 'Bash(make test)')
        self.assertEqual(command[-2:], ['--effort', 'low'])
        bare = self.build(ClaudeCodeRunner().configured({'default_deny': False, 'deny': ['Bash(x)']}))
        self.assertEqual(self.value(bare, '--disallowedTools'), 'Bash(x)')
        none = self.build(ClaudeCodeRunner().configured({'default_deny': False}))
        self.assertNotIn('--disallowedTools', none)
        with self.assertRaises(SystemExit):
            ClaudeCodeRunner().configured({'denny': []})
        with self.assertRaises(SystemExit):
            ClaudeCodeRunner().configured({'deny': 'Bash(x)'})

    def test_extra_folders_are_added_and_the_stdin_is_closed(self):
        command = self.build(extra_dirs=['/runs/r1'])
        self.assertEqual(command[command.index('--add-dir') + 1], '/runs/r1')
        self.assertTrue(ClaudeCodeRunner().stdin_closed)
        self.assertFalse(get_runner('hermes').stdin_closed)

    def test_registry_knows_both_spellings(self):
        self.assertEqual(get_runner('claude-code').name, 'claude-code')
        self.assertEqual(get_runner('claude_code').name, 'claude-code')
        self.assertEqual(get_runner('claude-code', {'deny': ['Bash(x)']}).deny, ('Bash(x)',))


class ReadsRecordedStreams(unittest.TestCase):
    def test_plain_answer(self):
        result = read('1-plain')
        self.assertEqual((result.exit_code, result.text, result.reported_model), (0, 'OK', 'small-model-5-5'))
        self.assertEqual(result.notes, [])

    def test_usage_is_read_from_the_result(self):
        usage = read('2-read').usage
        self.assertEqual(usage, {'input': 4, 'output': 98, 'cache_read': 16308, 'cache_write': 1063,
                                 'cost_usd': 0.00042508, 'turns': 2})
        result = [e for e in events('2-read') if e['type'] == 'result'][0]
        self.assertEqual(usage['cost_usd'], result['total_cost_usd'])
        self.assertEqual(usage['turns'], result['num_turns'])

    def test_a_tool_run_ends_with_the_models_last_words(self):
        result = read('3-edit')
        self.assertEqual(result.exit_code, 0)
        self.assertIn('M notes.txt', result.text)

    def test_denial_is_told_to_the_model_and_recorded(self):
        """Case 4: `git push origin main` was refused; the stream says so, the model got the refusal as an error
        result and answered that it did not run, the exit status is 0 and the run is not an error."""
        stream = events('4-denied')
        denied = [e for e in stream if e.get('subtype') == 'permission_denied']
        self.assertEqual(len(denied), 1)
        self.assertIn('has been denied', denied[0]['message'])
        told = [block for e in stream if e['type'] == 'user' for block in e['message']['content']
                if block.get('type') == 'tool_result']
        self.assertTrue(told[0]['is_error'])
        self.assertIn('denied', told[0]['content'])
        result = read('4-denied')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.notes), 1)
        self.assertTrue(result.notes[0].startswith('permission denials: 1 (Bash: git push origin main)'))
        self.assertIn("didn't run", result.text)
        found = records('4-denied')
        self.assertEqual([r['kind'] for r in found], ['start', 'tool', 'other', 'tool_result', 'text', 'result'])
        self.assertTrue(found[3]['error'])
        self.assertEqual(found[1]['command'], 'git push origin main')

    def test_turn_limit_is_an_error_result_without_text(self):
        """Case 5: --max-turns 1 on a three-step task. Exit status 1, a result with subtype error_max_turns,
        is_error true and no `result` text; the tool calls made so far are in the stream."""
        self.assertEqual(exit_code('5-turn-limit'), 1)
        result = read('5-turn-limit')
        self.assertEqual((result.exit_code, result.text, result.notes), (1, '', ['turn limit']))
        final = [e for e in events('5-turn-limit') if e['type'] == 'result'][0]
        self.assertEqual((final['subtype'], final['terminal_reason'], final['is_error']), ('error_max_turns', 'max_turns', True))
        self.assertNotIn('result', final)
        last = records('5-turn-limit')[-1]
        self.assertEqual((last['kind'], last['error'], last['exit_code']), ('result', True, 1))
        self.assertIn('maximum number of turns', last['text'])

    def test_a_zero_exit_with_an_error_result_becomes_one(self):
        home = support.temp_dir(self)
        path = os.path.join(home, 'events.jsonl')
        with open(path, 'w') as handle:
            handle.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'API Error: x'}) + '\n')
        self.assertEqual(ClaudeCodeRunner().read_events(path, 0).exit_code, 1)
        self.assertEqual(ClaudeCodeRunner().read_events(path, 143).exit_code, 143)

    def test_no_result_is_an_empty_answer_and_bad_lines_are_skipped(self):
        home = support.temp_dir(self)
        path = os.path.join(home, 'events.jsonl')
        with open(path, 'w') as handle:
            handle.write('not json\n[1]\n' + json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm'}) + '\n')
        result = ClaudeCodeRunner().read_events(path, 0)
        self.assertEqual((result.exit_code, result.text, result.reported_model, result.usage), (0, '', 'm', None))

    def test_resume_continues_the_same_session(self):
        """Case 6: the call that resumed case 2's session reports the same session id (the command used --resume)."""
        first = [e for e in events('2-read') if e['type'] == 'result'][0]['session_id']
        second = [e for e in events('6-resume') if e['type'] == 'result'][0]['session_id']
        self.assertEqual(first, second)
        self.assertRegex(first, r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        with open(os.path.join(case('6-resume'), 'command.json')) as handle:
            self.assertIn('--resume', json.load(handle))
        self.assertEqual(read('6-resume').text, 'second line')


class ParseEvent(unittest.TestCase):
    def test_records_of_a_tool_run(self):
        found = records('2-read')
        self.assertEqual([r['kind'] for r in found], ['start', 'tool', 'tool_result', 'text', 'result'])
        start, tool, tool_result, text, result = found
        self.assertEqual(start['model'], 'small-model-5-5')
        self.assertEqual((tool['tool'], tool['input']), ('Read', '/tmp/toy-repo/notes.txt'))
        self.assertIsNone(tool['command'])
        self.assertEqual(tool_result['text'], '1 first line here')
        self.assertFalse(tool_result['error'])
        self.assertEqual(text['text'], 'first line here')
        self.assertTrue(text['ts'] > 1_700_000_000_000)   # milliseconds since the epoch
        self.assertEqual(result['tokens'], {'input': 4, 'output': 98, 'cache_read': 16308, 'cache_write': 1063})

    def test_a_shell_call_carries_its_command(self):
        tool = [r for r in records('3-edit') if r['kind'] == 'tool'][0]
        self.assertEqual(tool['tool'], 'Bash')
        self.assertEqual(tool['command'], "printf '%s\\n' 'third line' >> notes.txt && git status --short")

    def test_lines_with_nothing_to_show_are_none(self):
        runner = ClaudeCodeRunner()
        rate = [line for line in open(os.path.join(case('1-plain'), 'events.jsonl')) if '"rate_limit_event"' in line][0]
        self.assertIsNone(runner.parse_event(rate))
        self.assertIsNone(runner.parse_event('not json'))
        self.assertIsNone(runner.parse_event('{"type": "something_new"}'))
        self.assertIsNone(runner.parse_event(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': ''}]}})))

    def test_parse_event_gives_the_first_record_of_a_line(self):
        line = json.dumps({'type': 'assistant', 'timestamp': '2026-11-18T20:31:33.945Z', 'message': {'content': [
            {'type': 'text', 'text': 'a'}, {'type': 'tool_use', 'id': 'x', 'name': 'Read', 'input': {'file_path': '/f'}}]}})
        runner = ClaudeCodeRunner()
        self.assertEqual([r['kind'] for r in runner.parse_events(line)], ['text', 'tool'])
        self.assertEqual(runner.parse_event(line)['kind'], 'text')

    def test_a_tool_result_can_carry_a_list_of_blocks(self):
        line = json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'is_error': True,
                                                                  'content': [{'type': 'text', 'text': 'boom'}]}]}})
        found = ClaudeCodeRunner().parse_event(line)
        self.assertEqual((found['kind'], found['error'], found['text']), ('tool_result', True, 'boom'))


class Refusal(unittest.TestCase):
    """DOCUMENTED, not recorded: Claude Code retries overloaded and rate-limited calls itself (system/api_retry); when it
    gives up it exits non-zero with the API error as the result."""

    def test_only_a_failed_session_that_names_a_limit_is_retried(self):
        runner = ClaudeCodeRunner()
        self.assertTrue(runner.retry_after_refusal('', 1, 'API Error: 529 {"type":"overloaded_error"}'))
        self.assertTrue(runner.retry_after_refusal('Rate limit reached', 1, ''))
        self.assertFalse(runner.retry_after_refusal('', 0, 'we discussed the rate limit of the API'))   # it succeeded
        self.assertFalse(runner.retry_after_refusal('', 1, 'tests failed'))
        self.assertFalse(runner.retry_after_refusal('', 1, ''))

    def test_hermes_keeps_its_active_session_limit(self):
        runner = get_runner('hermes')
        self.assertTrue(runner.retry_after_refusal('', 0, 'Error: active session limit reached (2)'))
        self.assertFalse(runner.retry_after_refusal('', 1, 'tests failed'))
        self.assertEqual((runner.refusal_wait_seconds, runner.refusal_max_waits), (20, 45))


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
