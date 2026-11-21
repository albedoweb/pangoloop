"""The Claude Code runner: builds the headless `claude -p` command and reads its stream-json events.

Written in step 3 (November 18, 2026) against Claude Code 2.1.327 (`claude --version`), from `claude --help`, the
documentation (https://code.claude.com/docs/en/headless, /cli-reference, /permissions) and calls recorded under
tests/fixtures/claude_code/.

One node is one `claude -p` process: a fresh session (a new session id each call, no session file kept unless
`persist_sessions` is set), the prompt as the argument, `--output-format stream-json --verbose`, the model, a turn limit,
and permissions fixed by flags, not by anyone's settings: `--permission-mode dontAsk` (a tool call that is not allowed
is refused with a message to the model, and the run goes on), `--allowedTools` from the node's tool sets,
`--disallowedTools` from DEFAULT_DENY plus the configuration's own patterns. Matching command text is not a sandbox: it
does not stop other forms of a command, reading files outside the worktree, or the network.
"""
import dataclasses
import datetime
import json
import os
import re
import uuid

from pangoloop.runners import Runner, RunResult, arg_summary, short

# Tool sets of the plan's categories (strings made for Hermes, e.g. `file,terminal,omh-lsp,vision`) -> Claude Code tools.
# `vision` needs no tool: the Read tool shows images to the model. `omh-lsp` (the language-server tools of the oh-my-hermes plugin) has no
# equivalent here. Names not listed are reported by `unmapped_toolsets` and add nothing.
TOOL_MAP = {'file': ('Read', 'Edit', 'Write', 'Glob', 'Grep'), 'terminal': ('Bash',), 'vision': (), 'omh-lsp': ()}
NO_EQUIVALENT = ('omh-lsp',)

# Commands no node runs unasked, as patterns of Claude Code's permission rules (`Bash(<pattern>)`, `*` any text). A deny
# rule is checked against every part of a compound command (split at && || ; | & and newlines). Patterns stop the form
# Claude usually writes; /usr/bin/git, `sh -c '...'`, `git -C . push`-like variants only where listed: this is not a
# sandbox.
DEFAULT_DENY = (
    # a push of anything
    'Bash(git push *)', 'Bash(git push)', 'Bash(git * push *)', 'Bash(git * push)',
    # recursive deletes (a script node's `delete` is the engine's own, with its own refusals)
    'Bash(rm -r *)', 'Bash(rm -R *)', 'Bash(rm -rf *)', 'Bash(rm -fr *)', 'Bash(rm --recursive *)',
    'Bash(rm * -r *)', 'Bash(rm * -rf *)', 'Bash(rm * -fr *)', 'Bash(find * -delete*)', 'Bash(find * -exec rm *)',
    # privilege
    'Bash(sudo *)', 'Bash(sudo)', 'Bash(su *)', 'Bash(doas *)',
    # the network into a shell
    'Bash(curl *|*sh*)', 'Bash(wget *|*sh*)', 'Bash(* | sh)', 'Bash(* | bash)', 'Bash(* | zsh)', 'Bash(* | sh *)',
    'Bash(* | bash *)', 'Bash(* | sudo *)', 'Bash(sh -s *)', 'Bash(bash -s *)', 'Bash(sh)', 'Bash(bash)',
    'Bash(eval *curl*)', 'Bash(eval *wget*)',
    # publishing packages
    'Bash(npm publish*)', 'Bash(yarn publish*)', 'Bash(pnpm publish*)', 'Bash(cargo publish*)',
    'Bash(twine upload*)', 'Bash(python* -m twine upload*)', 'Bash(poetry publish*)', 'Bash(gem push *)',
    'Bash(docker push *)', 'Bash(docker login*)',
    # other machines
    'Bash(ssh *)', 'Bash(scp *)', 'Bash(sftp *)', 'Bash(rsync *@*:*)', 'Bash(rsync *::*)',
    # clouds and clusters
    'Bash(kubectl *)', 'Bash(helm *)', 'Bash(terraform apply*)', 'Bash(terraform destroy*)', 'Bash(tofu apply*)',
    'Bash(tofu destroy*)', 'Bash(aws *)', 'Bash(gcloud *)', 'Bash(az *)', 'Bash(ansible-playbook *)',
)

REFUSAL_WORDS = re.compile(r'(?i)\b(?:429|529)\b|rate.?limit|overloaded')


def session_uuid(session: str) -> str:
    """The session id Claude Code takes (it must be a UUID) from the engine's session name: the same name, the same id.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'pangoloop:' + session))


def tools_for(toolsets: str) -> list[str]:
    """The Claude Code tools of a Hermes-style tool-set string; an empty string gives the file tools and the shell."""
    names = [part.strip() for part in (toolsets or '').split(',') if part.strip()]
    tools: list[str] = []
    for name in names:
        for tool in TOOL_MAP.get(name, ()):
            if tool not in tools:
                tools.append(tool)
    return tools


def unmapped_toolsets(toolsets: str) -> list[str]:
    """The names of a tool-set string this runner has no tool for (known absent ones included)."""
    return [part.strip() for part in (toolsets or '').split(',')
            if part.strip() and (part.strip() not in TOOL_MAP or part.strip() in NO_EQUIVALENT)]


@dataclasses.dataclass
class ClaudeCodeRunner(Runner):
    name = 'claude-code'
    stdin_closed = True   # `claude -p` reads standard input too: never the engine's
    refusal_wait_seconds = 30
    refusal_max_waits = 20
    binary: str = 'claude'
    allow: tuple = ()          # extra allowed patterns, e.g. 'Bash(make deploy-staging)'
    deny: tuple = ()           # extra denied patterns, added to DEFAULT_DENY
    default_deny: bool = True  # False: only `deny`
    flags: tuple = ()          # extra command-line words, appended last
    persist_sessions: bool = False   # True: keep the session file (needed to `resume`); default: --no-session-persistence
    strict_mcp: bool = True    # no MCP server from the person's own configuration

    def configured(self, settings):
        s = dict(settings or {})
        known = {'binary', 'allow', 'deny', 'default_deny', 'flags', 'persist_sessions', 'strict_mcp'}
        unknown = sorted(set(s) - known)
        if unknown:
            raise SystemExit(f'[runner.claude_code]: unknown keys {unknown}; the keys are {sorted(known)}')
        def words(key):
            value = s.get(key, ())
            if isinstance(value, str) or not all(isinstance(item, str) for item in value):
                raise SystemExit(f'[runner.claude_code] {key} must be a list of strings')
            return tuple(value)
        return dataclasses.replace(
            self, binary=str(s.get('binary') or self.binary),
            allow=words('allow'), deny=words('deny'), flags=words('flags'),
            default_deny=bool(s.get('default_deny', self.default_deny)),
            persist_sessions=bool(s.get('persist_sessions', self.persist_sessions)),
            strict_mcp=bool(s.get('strict_mcp', self.strict_mcp)))

    def denied(self) -> list[str]:
        return (list(DEFAULT_DENY) if self.default_deny else []) + list(self.deny)

    def command(self, session: str, model: str, provider: str, prompt_file: str, toolsets: str,
                max_turns: str | int, extra_dirs=(), resume: bool = False) -> list[str]:
        if provider not in (None, '', 'anthropic'):
            raise SystemExit(f"the claude-code runner takes models of the provider 'anthropic' only, not {provider!r} "
                             f'(route {model}@{provider}); write the model as a Claude Code alias or full name')
        with open(prompt_file, errors='replace') as handle:
            prompt = handle.read()
        tools = tools_for(toolsets) or list(TOOL_MAP['file'] + TOOL_MAP['terminal'])
        allowed = tools + list(self.allow)
        command = [self.binary, '-p', prompt, '--model', model, '--max-turns', str(max_turns),
                   '--output-format', 'stream-json', '--verbose',
                   '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
                   '--tools', ','.join(tools), '--allowedTools', ','.join(allowed)]
        denied = self.denied()
        if denied:
            command += ['--disallowedTools', ','.join(denied)]
        if self.strict_mcp:
            command.append('--strict-mcp-config')
        if resume:
            command += ['--resume', session_uuid(session)]
        elif self.persist_sessions:
            command += ['--session-id', session_uuid(session)]
        else:
            command.append('--no-session-persistence')
        if extra_dirs:
            command += ['--add-dir', *[str(path) for path in extra_dirs]]
        return command + list(self.flags)

    def env(self, session, model, provider) -> dict:
        return {}

    # ---- reading the stream (written from the recorded fixtures in tests/fixtures/claude_code/)
    def read_events(self, events_path: str, exit_code: int) -> RunResult:
        """The final answer is the `result` text of the last `result` event (absent when the run ended on its turn
        limit); a result with `is_error: true` turns a zero exit status into 1; the reported model is the `model` of
        `system`/`init`; the usage is the result's `usage` with `total_cost_usd` and `num_turns`; the notes say how the
        run ended when it was not `completed` and how many tool calls were refused."""
        answer, ran_on, code, usage, notes = '', None, exit_code, None, []
        with open(events_path, errors='replace') as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'system' and event.get('subtype') == 'init':
                    ran_on = event.get('model') or ran_on
                elif event.get('type') == 'result':
                    answer = str(event.get('result') or '')
                    if event.get('is_error'):
                        code = code or 1
                    usage, notes = result_usage(event), result_notes(event)
        return RunResult(code, answer, ran_on, usage, notes)

    def parse_events(self, line) -> list:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return []
        if not isinstance(event, dict):
            return []
        kind, subtype, ts = event.get('type'), event.get('subtype'), stamp_ms(event.get('timestamp'))
        if kind == 'system':
            if subtype == 'init':
                return [{'kind': 'start', 'model': event.get('model'), 'ts': ts,
                         'text': f"session started on {event.get('model')}"}]
            if subtype == 'permission_denied':
                return [{'kind': 'other', 'error': True, 'ts': ts, 'tool': event.get('tool_name'),
                         'text': short(event.get('message'), 200)}]
            if subtype == 'api_retry':
                return [{'kind': 'other', 'ts': ts,
                         'text': f"API retry {event.get('attempt')}: {event.get('error')} ({event.get('error_status')})"}]
            return []
        if kind in ('assistant', 'user'):
            content = (event.get('message') or {}).get('content')
            if not isinstance(content, list):
                return []
            found = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'text' and kind == 'assistant':
                    found.append({'kind': 'text', 'text': str(block.get('text') or ''), 'ts': ts})
                elif block.get('type') == 'tool_use':
                    args = block.get('input')
                    command = args.get('command') if isinstance(args, dict) else None
                    found.append({'kind': 'tool', 'tool': str(block.get('name')), 'input': arg_summary(args), 'ts': ts,
                                  'text': '', 'tool_use_id': block.get('id'),
                                  'command': command if block.get('name') == 'Bash' and isinstance(command, str) else None})
                elif block.get('type') == 'tool_result':
                    result = event.get('tool_use_result')
                    ms = result.get('durationMs') if isinstance(result, dict) else None
                    found.append({'kind': 'tool_result', 'tool': None, 'error': bool(block.get('is_error')),
                                  'ms': ms, 'ts': ts, 'tool_use_id': block.get('tool_use_id'),
                                  'text': short(content_text(block.get('content')), 200)})
            return found
        if kind == 'result':
            failed = bool(event.get('is_error'))
            text = event.get('result') if isinstance(event.get('result'), str) else '; '.join(
                str(item) for item in event.get('errors') or []) or str(event.get('subtype') or '')
            usage = result_usage(event)
            tokens = {key: usage[key] for key in ('input', 'output', 'cache_read', 'cache_write') if key in usage} \
                if usage else None
            return [{'kind': 'result', 'ts': ts, 'exit_code': 1 if failed else 0, 'tokens': tokens or None,
                     'error': failed,
                     'text': 'finished' if not failed else 'ended with an error: ' + short(text, 160)}]
        return []

    def retry_after_refusal(self, stderr_text: str, exit_code: int, answer: str = '') -> bool:
        """Claude Code already retries an overloaded or rate-limited API call itself (`system/api_retry` events); only
        when it gave up, with a non-zero exit and its message naming a 429, a 529, overload or a rate limit, is the
        session started again after a wait. Resting on the documentation (the retry event and its error categories),
        not on a recorded failure: no such failure was seen in step 3."""
        return exit_code != 0 and bool(REFUSAL_WORDS.search(f'{answer or ""}\n{stderr_text or ""}'))


def stamp_ms(value) -> int | None:
    """The `timestamp` of an event (ISO 8601 with Z) in milliseconds since the epoch, or None."""
    if not isinstance(value, str):
        return None
    try:
        return round(datetime.datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(str(item.get('text', '')) for item in content if isinstance(item, dict))
    return ''


def result_usage(event: dict) -> dict | None:
    """Usage facts of a `result` event as {input, output, cache_read, cache_write, cost_usd, turns}; any may be absent."""
    usage = event.get('usage') if isinstance(event.get('usage'), dict) else {}
    found = {}
    for key, name in (('input_tokens', 'input'), ('output_tokens', 'output'), ('cache_read_input_tokens', 'cache_read'),
                      ('cache_creation_input_tokens', 'cache_write')):
        if isinstance(usage.get(key), (int, float)):
            found[name] = usage[key]
    if isinstance(event.get('total_cost_usd'), (int, float)):
        found['cost_usd'] = event['total_cost_usd']
    if isinstance(event.get('num_turns'), int):
        found['turns'] = event['num_turns']
    return found or None


def result_notes(event: dict) -> list:
    notes = []
    reason = event.get('terminal_reason')
    if reason == 'max_turns' or event.get('subtype') == 'error_max_turns':
        notes.append('turn limit')
    elif event.get('subtype') not in (None, 'success') or (reason not in (None, 'completed')):
        notes.append(f"ended: {event.get('subtype')} / {reason}")
    denials = event.get('permission_denials')
    if isinstance(denials, list) and denials:
        def what(item):
            args = item.get('tool_input') or {}
            return short(f"{item.get('tool_name')}: {args.get('command') or args.get('file_path') or ''}", 80)
        shown = '; '.join(what(item) for item in denials[:3] if isinstance(item, dict))
        notes.append(f'permission denials: {len(denials)} ({shown})')
    return notes


RUNNER = ClaudeCodeRunner()
