"""Runners: the one place that knows how a model's session is started, followed and read.

The engine (pangoloop/graph.py) and the viewer (pangoloop/view.py) hold a runner, chosen by `[runner] name` of the
configuration file or the environment variable PANGOLOOP_RUNNER (default `hermes`). Nothing outside this package knows a
harness's name, its flags or its event format. The engine keeps supervising the processes (process groups, time limits,
slots, stopping); a runner only describes the command and the output. A runner is an object with this interface
(`Runner` below is the base class with the defaults):

    name: str

    command(session, model, provider, prompt_file, toolsets, max_turns, extra_dirs=(), resume=False) -> list[str]
        The argv that starts one one-shot session on the prompt in `prompt_file`, writing a stream of JSON events to its
        standard output. The engine starts it itself and hands stdout to a file. `extra_dirs` are folders outside the
        working folder the node must be able to read (the run folder, with the plan); `resume=True` asks, where the
        harness can, to continue the session called `session` instead of starting it (the engine does not use it today:
        a node called again after a person's answer is a new session that is sent the whole exchange).

    env(session, model, provider) -> dict[str, str]
        Environment variables to add for that process ({} by default).

    stdin_closed: bool
        True when the process must not inherit the engine's standard input (it is then started with /dev/null).

    read_events(events_path, exit_code) -> RunResult
        Reads that stream after the process ended and returns what the engine needs.

    parse_event(line) -> dict | None
        One line of the live stream as a small neutral record the page shows, or None for a line with nothing to show:
            kind     'start' | 'text' | 'tool' | 'tool_result' | 'result' | 'other'
            text     a short text (the words said, the result, a note); '' when there is none
            tool     the tool's name (kind 'tool' and 'tool_result'), else None
            input    a one-line summary of the tool's input (kind 'tool')
            command  the shell command in full, when the tool is a shell (kind 'tool')
            error    True for a tool result that is an error or a refusal
            ms       how long the tool ran, when the line says so
            ts       when, in milliseconds since the epoch, when the line says so
            model    the model a session reports (kind 'start')
            tokens   {'input','output','cache_read','cache_write'} when the line carries them (kind 'result')
            exit_code the harness's own exit code or status, when the line carries it (kind 'result')
        (`parse_events(line)` returns the list of every record of a line; a line may hold several blocks.)

    retry_after_refusal(stderr_text, exit_code, answer='') -> bool
        True when the session ended because the harness refused to admit it or the service was busy, so that the engine
        should wait `refusal_wait_seconds` and start it again, at most `refusal_max_waits` times.

    run(session, model, provider, prompt, cwd, toolsets, max_turns, folder, timeout=None) -> RunResult
        Everything in one call for a caller that has no supervision of its own (the preflight): writes
        <folder>/prompt.txt, starts the command in `cwd`, writes <folder>/events.jsonl and <folder>/stderr.log,
        waits (at most `timeout` seconds; 124 when it had to stop it) and returns the result.

RunResult carries `exit_code` (int; 124 for a timeout), `text` (the final answer, '' when there is none),
`reported_model` (the model the session said it ran on, or None), `usage` (None, or a dict of optional facts:
input, output, cache_read, cache_write, cost_usd, turns) and `notes` (short facts about the ending that the page and
result.json keep: 'turn limit', 'permission denials: 2', ...).

A runner is added by writing pangoloop/runners/<name>.py with a module-level `RUNNER`. The engine does not change.
"""
import dataclasses
import importlib
import json
import os
import signal
import subprocess
import time


LEGACY_RUNNER = 'hermes'   # the runner of every run recorded before step 3 (they have no `runner` file)


@dataclasses.dataclass
class RunResult:
    exit_code: int
    text: str
    reported_model: str | None = None
    usage: dict | None = None
    notes: list = dataclasses.field(default_factory=list)


def short(value, limit: int) -> str:
    """One line of at most `limit` characters."""
    text = ' '.join(str(value if value is not None else '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def arg_summary(args) -> str:
    if not isinstance(args, dict):
        return short(args, 160)
    for key in ('command', 'path', 'pattern', 'query', 'url', 'image_url', 'file_path', 'question', 'task'):
        if isinstance(args.get(key), str):
            extra = ''
            if key == 'path' and isinstance(args.get('content'), str):
                extra = f"  ({len(args['content'])} chars)"
            return short(args[key], 170) + extra
    return short(json.dumps(args, ensure_ascii=False), 160)


class Runner:
    """Defaults of the interface. A runner overrides command, read_events and parse_event."""
    name = ''
    stdin_closed = False
    refusal_wait_seconds = 20
    refusal_max_waits = 45

    def configured(self, settings: dict | None):
        """A copy of this runner with the settings of its table in the configuration file ({} or None: as it is)."""
        return self

    def command(self, session, model, provider, prompt_file, toolsets, max_turns, extra_dirs=(), resume=False):
        raise NotImplementedError

    def env(self, session, model, provider) -> dict:
        return {}

    def read_events(self, events_path: str, exit_code: int) -> RunResult:
        raise NotImplementedError

    def parse_event(self, line):
        found = self.parse_events(line)
        return found[0] if found else None

    def parse_events(self, line) -> list:
        raise NotImplementedError

    def retry_after_refusal(self, stderr_text: str, exit_code: int, answer: str = '') -> bool:
        return False

    def run(self, session: str, model: str, provider: str, prompt: str, cwd: str, toolsets: str,
            max_turns, folder: str, timeout: float | None = None) -> RunResult:
        os.makedirs(folder, exist_ok=True)
        prompt_file = f'{folder}/prompt.txt'
        with open(prompt_file, 'w') as handle:
            handle.write(prompt)
        command = self.command(session, model, provider, prompt_file, toolsets, max_turns)
        environment = dict(os.environ, **self.env(session, model, provider))
        with open(f'{folder}/events.jsonl', 'w') as events, open(f'{folder}/stderr.log', 'w') as errors:
            process = subprocess.Popen(command, cwd=cwd, stdout=events, stderr=errors, start_new_session=True,
                                       env=environment, stdin=subprocess.DEVNULL if self.stdin_closed else None)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                for sign in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sign)
                    except ProcessLookupError:
                        break
                    time.sleep(5)
                code = 124
        return self.read_events(f'{folder}/events.jsonl', code)


def get_runner(name: str, settings: dict | None = None):
    """The runner called `name`: the module pangoloop.runners.<name> must define RUNNER. `name` may be written with
    dashes (`claude-code`). With `settings` (the runner's table of the configuration file) a configured copy."""
    module_name = name.replace('-', '_')
    if not module_name.isidentifier():
        raise SystemExit(f'runner {name!r} is not a runner name')
    try:
        module = importlib.import_module(f'pangoloop.runners.{module_name}')
    except ModuleNotFoundError as error:
        if error.name != f'pangoloop.runners.{module_name}':
            raise
        raise SystemExit(f'runner {name!r}: no such runner (pangoloop/runners/{module_name}.py)') from None
    return module.RUNNER.configured(settings) if settings else module.RUNNER
