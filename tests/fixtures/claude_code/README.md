# Recorded Claude Code streams

Written on November 18, 2026 by Claude, step 3 of the package. Six real headless calls, made once, with
`claude` 2.1.327 (Claude Code) on a model alias (it reported a model id; both are replaced here by `model-h` and `small-model-5-5`, the recorded format is unchanged), in a temporary folder
outside every project, holding a tiny repository (`notes.txt` with two lines, `app.py`) with no remote. Each call was
built by the runner's own `command()` (`command.json` is that command with the binary and the prompt text left out;
`prompt.txt` is the prompt). Per case: `events.jsonl` (the whole standard output), `stderr.log` (empty in all six),
`exit_code`, `command.json`, `prompt.txt`.

| Case | Prompt | Turn limit | Exit | What it shows |
| --- | --- | --- | --- | --- |
| `1-plain` | answer with the single word OK | 6 | 0 | `system/init`, one `assistant` text, a `result` |
| `2-read` | read notes.txt, say the first line | 6 | 0 | a `Read` tool call and its result; run with `--session-id` (the session is kept) |
| `3-edit` | append a line, run `git status --short` | 6 | 0 | a `Bash` call whose result is ` M notes.txt` |
| `4-denied` | run `git push origin main` (on the deny list) | 6 | 0 | `system/permission_denied`, an error `tool_result` the model reads, `permission_denials` in the result; `is_error` false |
| `5-turn-limit` | a three-step task | 1 | 1 | result `subtype: error_max_turns`, `terminal_reason: max_turns`, `is_error: true`, no `result` text, `errors` |
| `6-resume` | a follow-up question | 6 | 0 | `--resume` of case 2's session: the same `session_id` |

## What was replaced before committing

Nothing else was edited; the streams are otherwise as printed. Replaced, by a script:

- the folder of the toy repository (`/private/tmp/pgl-e.XXXX` and `/tmp/pgl-e.XXXX`) by `/tmp/toy-repo` (26 places);
- the lists in `system/init` that describe the person's own Claude Code installation (`mcp_servers`, `plugins`, `skills`,
  `slash_commands`, `terminal_slash_commands`, `agents`) by empty lists: they named the person's plugins and connectors;
- `memory_paths` (it held the home folder) by `/Users/USER/.claude/projects/-tmp-toy-repo/memory/`;
- `messaging_socket_path` and `credential_expires_at` by `REDACTED`;
- `rate_limit_info` of the `rate_limit_event` lines (the account's utilisation and reset times) by a stub;
- the opaque `signature` of one `thinking` block by `SIGNATURE`.

No account email, organisation id, or key was in the streams (searched for `@`, `org`, `account`, the user name). What
remains and was left: session ids, message ids and request ids (random, not secrets), `total_cost_usd` and token counts,
the model names. `apiKeySource: none` means the login is the subscription's, not an API key.

Cases 2 and 6 were made with one session name, so case 6's session id is case 2's (the test checks it).
