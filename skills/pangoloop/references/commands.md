# The commands

Every command finds the project (a folder with `pangoloop.toml`) from the current folder upward, or by `--project <path>`. All of
them work with no prompt, so a program can drive them. Times are UTC (`2026-11-20T10:00:00Z`); a field with no value is `null`.

| command | what it does |
|---|---|
| `pangoloop version` | prints `pangoloop 0.1.0` |
| `pangoloop init [--preset claude-code-only\|hermes-mixed\|one-model] [--runner hermes\|claude-code] [--route <category>=<model@provider>]... [--reader <name>=<model@provider>]... [--force] [--json]` | makes the project: `pangoloop.toml`, `.pangoloop/{runs,plans,roles}`, the seven example roles, `.gitignore` lines. Refuses to overwrite `pangoloop.toml` without `--force`; runs no `git init` and no commit |
| `pangoloop config` | prints the configuration in effect and where each value came from |
| `pangoloop roles [--json]`, `pangoloop roles show <name>` | the project's roles |
| `pangoloop preflight <plan> [--reader <name>]... [--out <folder>] [--strict]` | reader models read the plan and list contradictions; writes `summary.md` under `--out` (default `./preflight-out`: pass a folder outside the repository, such as a temporary one). Exit 0 when every reader answered |
| `pangoloop run <plan> [--detach] [--view] [--json] [--name <run>]` | runs the plan; a bare name is looked up in `.pangoloop/plans/` |
| `pangoloop status [<run>] [--json]` | the project's runs, or one run |
| `pangoloop requests list\|wait\|answer\|escalate\|rulings\|rule ...` | see `references/requests-and-rulings.md` |
| `pangoloop resume <run> [--plan <file>] [--reset-rounds] [--accept-plan-changes]` | continues a run that ended not verified |
| `pangoloop cleanup <run>` | stops what a killed engine left running, for a run that will not be resumed |
| `pangoloop view [--port N]` | serves the page of the project's runs (`run --view` does it for you) |
| `pangoloop steps check <repo> <steps file>` | shows what a steps file would do; changes nothing |

`init` takes the models as flags so that you can pass what the user answered. For the Claude Code runner a model is the name Claude
Code accepts for `--model`, with no provider; for Hermes it is `model@provider`. A route name is a category: `default` (the coder),
`review`, `ui`, or one of the project's own. Without `--route` the preset's values stay, and a route that still holds a placeholder
stops a run that needs it, with exit 2.

## Starting a run

`pangoloop run <plan> --detach --view --json` prints one object and exits 0 once the run's folder exists:
`{"run": "<name>", "folder": "<path>", "log": "<engine.log>", "pid": <n>, "page": "http://127.0.0.1:5420/", "page_run":
"http://127.0.0.1:5420/?run=<name>"}`. Give the user `page_run`. The run is named `<plan file name>-<YYYYMMDD-HHMMSS>`. A plan or
configuration fault is printed by the command (exit 2) and no run is left. The short form sets one worktree per build node, requests
on, and temporary files inside the run's folder.

## Reading a run

`pangoloop status --json`: `{"project", "page", "runs_folder", "runs": [{"name", "state", "verdict", "started", "ended",
"waiting_for", "open_requests"}]}`, newest first.

`pangoloop status <run> --json`: `{"name", "state", "verdict", "started", "ended", "folder", "plan", "repo", "branch", "runner",
"page", "waiting_for", "reviews": ["PASS"], "cost": {"usd", "partial", "workers", "workers_with_cost", "limit_usd", ...},
"nodes": [{"id", "kind", "role", "label", "state", "depends_on", "workers"}], "requests": [{"id", "node", "for", "question",
"asked_at"}]}`. Exit 0; 2 when the run does not exist.

- `state`: `running` (a run waiting for an answer is running), `verified`, `not verified`, `aborted` (a person answered ABORT),
  `stopped` (no result and no engine: stopped or killed; `resume` continues it).
- `verdict`: the engine's word: `verified`, `not_verified`, `stopped_by_human`, `no_answer_from_human`, `stopped`; null while running.
- `waiting_for`: `person`, `coordinator` or null.
- A node's `state`: `waiting`, `running`, `integrating`, `accepted`, `failed`, `waiting for human`, `waiting for coordinator`.
- `cost.usd` is the sum of the known costs, null when none is known (say "unknown", never zero); `cost.partial` says some worker's
  cost is unknown. A cost comes from the `[prices]` table of `pangoloop.toml` (dollars per million tokens, by model id or prefix) or,
  when the table has no price, from what Claude Code itself reports. `limit_usd` is the run's limit.

The run's folder (`.pangoloop/runs/<run>/`) holds `plan.md`, `result.json`, `rulings.md`, `engine.log` and `workers/<nn>-<node>/`
with each worker's `prompt.txt`, `events.jsonl` and `answer.md` (its final text). Read a failed node's `answer.md` and the engine's
log to say why it failed.

## Exit codes of `pangoloop run` (in the foreground)

0 verified (with `--detach`: the run started); 1 ended not verified (a node failed, the review did not pass, the acceptance failed,
nobody answered in time, the cost limit was reached); 2 a plan or configuration error found before anything ran, a name already
used, an unknown role, or a refused resume; 3 a person aborted it; 124 and 130 stopped by a signal.

## Resuming

`pangoloop resume <run>` continues a run that ended not verified, was stopped or was killed: accepted nodes are kept, the rest runs
again, from what git confirms; it never continues a node's half-done work. `--reset-rounds` starts the review and repair counters
at zero (use it when a review kept asking and the user wants more rounds); `--accept-plan-changes` lets the plan differ from the
one the run started with under accepted nodes (edit the plan only for nodes that have not run). Refused with exit 2 when the run is
verified, a live engine holds it, or the repository or branch is not the run's.

## Limits and cost

`pangoloop.toml` `[limits]`: `max_cost_usd` (no new worker starts once the known cost is over it; it does not stop a worker that is
running and does not count a model with no price, so it is a brake, not a guarantee), `max_turns`, `node_limit_seconds`,
`max_parallel` (default 3), `max_fix_rounds` (default 2). `PANGOLOOP_MAX_COST_USD` overrides the first.
A command the engine runs (a script step, an acceptance command) may report money it spent itself with a line that starts with
`PANGOLOOP-SPEND: <usd> [label]`; the cap counts it. It is self-reported and not verified, and it is seen only after the command
ends: the cap stops the next worker, it is not a hard ceiling.
