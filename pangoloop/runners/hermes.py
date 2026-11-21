"""The Hermes Agent runner: builds the `hermes ... chat --oneshot` command line and reads its stream-json events.

Extracted unchanged from pangoloop/graph.py (the command list in Run.worker, the event reading at the end of
Run.launch) and from preflight.sh (the same command, with `-t file --max-turns 40`). Since step 3 it also owns what the
viewer used to know about this stream (`parse_events`, formerly `Summary.feed` and `tail_events` of pangoloop/view.py)
and the refusal `active session limit` with its wait (formerly `LIMIT_REFUSAL` in the engine). The profile
is `[runner.hermes] profile` of the configuration file. With no profile named, no `-p` is passed and Hermes uses its own
default (the profile set with `hermes profile use`, else its `default` one).

The stream: one JSON object per line, `type` being `text` (`text`), `tool_use` (`name`, `input`), `tool_result`
(`name`, `is_error`, `duration_ms`, `output`), `system` with `subtype: init` (`model`, `timestamp`) and `result`
(`text`, `exit_code`, `tokens`: input, output, cache_read, cache_write). Observed in November 2026;
not re-recorded in step 3 (no Hermes call was made); the viewer tests are its fixtures.
"""
import dataclasses
import json

from pangoloop.runners import Runner, RunResult, arg_summary, short

LIMIT_REFUSAL = 'active session limit'   # Hermes's answer when it does not admit a session


def result_summary(event: dict) -> str:
    out = event.get('output')
    if isinstance(out, str):
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if isinstance(data, dict):
            if isinstance(data.get('output'), str):
                code = data.get('exit_code')
                return (f'exit {code}: ' if code is not None else '') + short(data['output'], 200)
            for key in ('content', 'matches_text', 'analysis', 'diff', 'error', 'message'):
                if isinstance(data.get(key), str):
                    return short(data[key], 200)
            return short(out, 200)
    return short(out, 200)


@dataclasses.dataclass
class HermesRunner(Runner):
    name = 'hermes'
    profile: str = ''   # '' = no `-p`: Hermes's own default profile
    refusal_wait_seconds = 20
    refusal_max_waits = 45   # give up on a worker after 15 minutes of refusals

    def configured(self, settings):
        unknown = sorted(set(settings or {}) - {'profile'})
        if unknown:
            raise SystemExit(f'[runner.hermes]: unknown keys {unknown}; the key is profile')
        return dataclasses.replace(self, profile=str((settings or {}).get('profile') or self.profile))

    def command(self, session: str, model: str, provider: str, prompt_file: str, toolsets: str,
                max_turns: str | int, extra_dirs=(), resume: bool = False) -> list[str]:
        return ['hermes', *(['-p', self.profile] if self.profile else []), 'chat', '--query-file', prompt_file, '--oneshot',
                '--format', 'stream-json', '-m', model, '--provider', provider, '-t', toolsets,
                '--max-turns', str(max_turns), '-c', session, '--create-if-missing']

    def read_events(self, events_path: str, exit_code: int) -> RunResult:
        """The final answer is the `text` of the last `result` event; a result whose exit_code is not 0 (or None) turns
        a zero exit status into 1; the reported model is the `model` of the last `system`/`init` event that has one;
        the usage is the `tokens` of the last result (the only fact of usage this stream states)."""
        answer, ran_on, code, usage = '', None, exit_code, None
        with open(events_path, errors='replace') as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'result':
                    answer = str(event.get('text') or '')
                    if str(event.get('exit_code')) not in ('0', 'None'):
                        code = code or 1
                    tokens = event.get('tokens')
                    if isinstance(tokens, dict):
                        usage = {key: tokens[key] for key in ('input', 'output', 'cache_read', 'cache_write')
                                 if isinstance(tokens.get(key), (int, float))}
                elif event.get('type') == 'system' and event.get('subtype') == 'init':
                    ran_on = event.get('model') or ran_on
        return RunResult(code, answer, ran_on, usage or None)

    def parse_events(self, line) -> list:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return []
        if not isinstance(event, dict):
            return []
        kind, ts = event.get('type'), event.get('timestamp')
        ts = ts if isinstance(ts, (int, float)) else None
        if kind == 'text':
            return [{'kind': 'text', 'text': str(event.get('text') or ''), 'ts': ts}]
        if kind == 'tool_use':
            args = event.get('input')
            command = args.get('command') if isinstance(args, dict) else None
            return [{'kind': 'tool', 'tool': str(event.get('name')), 'input': arg_summary(args), 'ts': ts,
                     'text': '', 'command': command if isinstance(command, str) and event.get('name') == 'terminal'
                     else None}]
        if kind == 'tool_result':
            return [{'kind': 'tool_result', 'tool': event.get('name'), 'error': bool(event.get('is_error')),
                     'ms': event.get('duration_ms'), 'text': result_summary(event), 'ts': ts}]
        if kind == 'system':
            if event.get('subtype') == 'init':
                return [{'kind': 'start', 'model': event.get('model'), 'ts': ts,
                         'text': f"session started on {event.get('model')}"}]
            return [{'kind': 'start', 'model': event.get('model'), 'ts': ts,
                     'text': f"session started on {event.get('model')}", 'init': False}]
        if kind == 'result':
            tokens = event.get('tokens')
            return [{'kind': 'result', 'ts': ts, 'exit_code': event.get('exit_code'),
                     'tokens': tokens if isinstance(tokens, dict) else None,
                     'text': f"finished, exit code {event.get('exit_code')}"}]
        return []

    def retry_after_refusal(self, stderr_text: str, exit_code: int, answer: str = '') -> bool:
        """Hermes answers `active session limit ...` as the session's text when it does not admit one more session."""
        return LIMIT_REFUSAL in (answer or '')


RUNNER = HermesRunner()
