# Setting it up

My recommendations for a first setup, in the order I would make the choices. They are advice, not measurements: I give
reasons, not results, and you should check each choice against your own work. The files to start from are in
`examples/presets/` (and `pangoloop init --preset ...` writes the same ones into a project), and the comments in them say what
each setting does; `pangoloop --help` and `pangoloop <command> --help` list the commands and environment variables;
`examples/toy/plan.md` explains the plan format.

## With your agent

The package comes with a skill, `skills/pangoloop/`, that lets the coding agent you already use drive the tool for you: you write
what you want done; the agent asks which models to use for which roles (once per project), writes a plan from your request and
the repository, has it read by the readers you chose, shows you a summary, starts the run, gives you the address of the page,
answers the nodes' questions that are the coordinator's, brings you the ones that are yours, and reports the result. It is
a text of instructions with five reference pages (`plan-format.md`, `cutting-work.md`, `roles.md`, `requests-and-rulings.md`,
`commands.md`), and it uses only the commands described in this guide.

- **Claude Code, as a plugin** (the repository is the marketplace): `claude plugin marketplace add albedoweb/pangoloop`, then
  `claude plugin install pangoloop@pangoloop`.
- **Claude Code, one project**: copy the folder `skills/pangoloop/` into the project's `.claude/skills/`.
- **Hermes**: not verified yet in use. Its documentation says a skill folder goes under `~/.hermes/skills/`, or into a project's
  `.hermes/skills/` (or `.agents/skills/`) after `hermes skills trust` in that repository.

The rules for cutting work into nodes are written once at length, in `prompts/STAGE_AUTHOR.md`, and once in short in the skill's
`references/cutting-work.md`, under the same headings; they are changed together. The skill never pushes, merges or deletes a
branch, and it tells you what this guide says about the page (no login) and the deny list (a seat belt, not a sandbox).

## A project

Everything the package keeps for one piece of work lies in that work's own repository. A **project** is a git repository with a
`pangoloop.toml` at its root, and `pangoloop init` makes one:

```
my-project/
  pangoloop.toml          the configuration: runner, routes, readers, limits
  .gitignore              gets .pangoloop/runs/ and the page's two files (view.json, view.log)
  .pangoloop/
    runs/                 one folder per run (Git-ignored)
    plans/                plans, one Markdown file each (kept in git if you want)
    roles/                roles, one Markdown file each (kept in git if you want)
    view.json             which process serves the page, and where (while one does)
```

Every command finds the project the same way: `--project <path>`, else `$PANGOLOOP_PROJECT`, else the first folder with a
`pangoloop.toml` on the way up from the current folder. The search stops at the filesystem root and at your home folder, and your
home folder is never itself a project. The three folders are those defaults, and each can be moved under `[paths]` in the
configuration file (`runs`, `plans`, `roles`; relative to the project, or absolute) or by `PANGOLOOP_RUNS`, `PANGOLOOP_PLANS` and
`PANGOLOOP_ROLES`.

A run writes only under the project: its folder in `.pangoloop/runs/`, the node worktrees inside that folder, the node branches
`sg/<node>` and the references `refs/pangoloop/<run>/tip` in the project's own git, and, with the short `run`, the workers'
temporary files in the run folder (`SG_NODE_SCRATCH`). Two things reach beyond it, and both are the model tool's: Claude Code keeps
no session file unless you set `persist_sessions`; Hermes keeps its sessions in its own place and the engine names each one with
the run's name and start time. The run folder is not part of what the engine records as the work: it is excluded from the
checkpoints.

Run the short form in a project:

```
pangoloop run <plan> [--detach] [--view] [--name <run name>]
```

The plan is a file, or a bare name looked for as `<name>` and `<name>.md` in `.pangoloop/plans/`. The repository is the project's
root. The run is named `<plan file name>-<YYYYMMDD-HHMMSS>` (printed), or what `--name` says; a name is never reused. The short form
sets `SG_NODE_WORKTREES=1`, `SG_HUMAN=1` and `SG_NODE_SCRATCH=1` unless you set them, so that every build node works in its own
git worktree, nodes may ask for help (`pangoloop requests`) and temporary files stay in the run folder. The full form,
`pangoloop run <plan> <repo> <name>`, runs exactly as before, with the variables you give it.

A node's branch is `sg/<node id>`. A second run of the same plan finds the first run's branches there and keeps them as
`sg/<node id>-earlier-<n>`; `git branch -D 'sg/*'` removes them when you do not want them. A run ends with the accepted work in
your working folder as one uncommitted change; commit `pangoloop.toml`, the `.gitignore` line, and the plans and roles you want in
git **before** a run (an untracked file is carried through a run as work in progress and left as it was).

### Detached runs

`pangoloop run <plan> --detach` starts the engine as a process of its own, in a session of its own, so that it survives the command
and its terminal, and returns (exit 0) as soon as the run's folder exists, which is after the plan and the configuration were
read and checked. It prints the run's name, its folder, the engine's log (`engine.log` in the run folder) and the address of the
page when one is serving the project. A plan or configuration fault is printed by the command itself, with exit 2, and no run is
left. `--json` prints one object: `run`, `folder`, `log`, `pid`, `page`, `page_run` (the last two are null when no page
serves). `--view` makes sure a page serves the project first, and prints its address.

### Several projects at once

Projects share nothing: each has its own folders, its own `view.json`, its own page on its own port, its own locks, and each
run's lock names the engine's process and start time. Two projects can run at the same time, each with a detached run and a page;
the test suite runs exactly that with the stand-in for a model. A page for a project shows only that project's runs.

## The page

`pangoloop view` is part of the package (its page is inside `pangoloop/view.py`; nothing else is installed). It shows one run's
graph, each worker's model and role, time and tokens, the handoffs, the acceptance and review rounds, the open requests with a box
to answer, and the run's facts. With no arguments it serves the current project's runs.

It listens on `127.0.0.1` only, prints its address as its first line (`http://127.0.0.1:<port>/`) and records itself in
`.pangoloop/view.json` (`pid`, `port`, `started`, `process_started`, `url`, `project`). The port is `--port`, else `[view] port` in
the configuration, else the first free port from 5420 upward. A second `view` for the same project prints `already serving at
<address>` instead of starting another; a file left by a process that is gone (checked by pid and start time) is replaced; the file
is removed when the page stops.

**It has no login.** Anyone who can reach the address can read every run of the project (prompts, answers, file contents the nodes
read) and can answer a request. Keep it on the loopback address; do not forward the port, put it behind a tunnel or run it on a
shared machine you do not trust.

## Kinds and roles

A node of a plan says two different things, and the plan keeps them apart:

- **`kind`** is what the engine does with the node. The list is closed:
  - `build`: a model session in its own git worktree, then an integration by rebase; it must change something unless it says
    `may_be_empty: yes`.
  - `review`: a model session that reads the whole change and ends with a verdict; on `CHANGES REQUESTED` a repair worker gets the
    findings and the review runs again, up to `max_fix_rounds`.
  - `baseline`: a model session that looks and changes nothing (a first run of the tests, a survey).
  - `script`: no model; the steps of its `STEPS` line (delete, move, run a command).
  - `acceptance`: no model; the script runs the backticked commands of `VERIFY` and calls a repair worker when one fails.
  - `human`: a stop for a person; its `TASK` is the question.
- **`role`** is who the worker is: a name the plan chooses, defined by a file `<roles folder>/<name>.md`. It belongs to the kinds
  that have a model worker (`build`, `review`, `baseline`). A node of kind `script`, `acceptance` or `human` has no role, and giving
  one is a plan error.

A role file is a small header and then the prompt:

```
---
kind: build            # which kinds of node may use it: build, review, or both ("build, review")
category: ui           # optional: the route's category, which selects the model (a node's own `category` wins)
description: one line, shown in lists
---
You are ... what you must do, what you must not do, what your handoff holds, and when to stop and ask.
```

The prompt is text the engine puts into the worker's prompt **after the line that names the node and before the node's
contract lines** (TASK, DELIVERABLE, SCOPE, VERIFY, STOP WHEN), as `Your role: <name> (<description>).` and the text. The prompt may
use `{{REPO}}` and `{{BRANCH}}`, filled as in the plan: the folder the node works in (its own copy, with worktrees) and its
branch. A role contains nothing else: no tools, no limits; those stay the node's and the configuration's.

The engine adds one thing to a `review` node, whatever the role says: a paragraph that tells the worker to list each finding on a
line starting `FINDING 1:`, `FINDING 2:`, ... and to end with `VERDICT: PASS` or `VERDICT: CHANGES REQUESTED`, which is how the
engine reads a review. It is in the engine and not in a role file, so a role you write cannot break it.

A node that wants a role writes both lines:

```
### add-parser
- label: Add the parser
- kind: build
- role: coder
- depends_on: none
- write_scope: src/parser.py, tests/test_parser.py
- TASK: ...
```

`category` may be left out of a node that has a `kind`: the role's is used, else `default` (`review` for a review node). The
role's name does not choose a model: the model comes from the category through `[routes]`.

`pangoloop roles` lists the project's roles (name, kinds, category, description, file) and `pangoloop roles show <name>` prints one.
A role named in a plan that has no file stops the run before it starts (exit 2) with a sentence that names the roles folder and
lists the roles it found; so does a role used with a kind it does not allow, and a file that is broken.

`pangoloop init` copies seven example roles into `.pangoloop/roles/` (they are in `examples/roles/` too). They are examples in
one author's voice, short and plain, not measured; edit them or add your own:

| role | kind | what it is |
|---|---|---|
| `coder` | build | writes the code for one node, small and in the repository's style |
| `test-writer` | build | writes tests for behaviour that exists or is specified, changes no production code |
| `interface-builder` | build | builds or changes a screen and looks at it before saying it is done (category `ui`) |
| `researcher` | build | finds something out and writes it as a document at a path the task names, changes no code |
| `reviewer` | review | reads the whole change for correctness against the task statement (category `review`) |
| `security-reviewer` | review | reads the change as someone who wants to misuse it (category `review`) |
| `simplicity-reviewer` | review | reads the change for what can be left out (category `review`) |

**Plans written before kinds** have `role: build` (or `baseline`, `script`, `acceptance`, `review`, `human`) and no `kind`. Such a
node is read as that kind with the built-in role of the same name, whose prompt is empty: the worker gets exactly what it always
got. A `role` word that is not a kind, has no `kind` beside it and has no role file is, as it was, a plain model session (the
kind `baseline`); the engine prints a warning for it. A new plan should always write `kind:`.

### Models for the categories

A run has a few places where a model is chosen. The configuration names a model for each category; no model is built into the
code, so write down what you have.

- **The coder** is the default route and does most nodes. Choose it for price and speed, and for following a literal
  instruction to the end: the nodes of a good plan are small and exact, so the cheapest model that can finish one reliably
  is the right one. Look for steady tool use (it edits, runs the commands, reads the output) more than for cleverness.
- **The reviewer** is the `review` route, and it must be another model than the coder, ideally from another vendor: a model
  tends to approve what it would have written. Here, spend more. Look for a model that reads a whole change and a statement
  and says what is wrong in the words of the rules, without inventing faults.
- **The interface model** is the `ui` route, for nodes that must look at a screen. It has to accept images; the other
  routes need not. If you have no such nodes, leave it as the coder.
- **Readers** are the models `pangoloop preflight` uses to read a plan before a run, named in `[readers]`. Use two that
  differ, a quick one for every draft and a careful one before a long run; they find different faults. They only read.
- **The coordinator** answers the requests a node makes (`NEEDS COORDINATOR:`) with `pangoloop requests`. It can be a
  model in a session of your own, a script, or you. It must know the plan and the goal; it needs no route in the
  configuration. Questions for a person (`NEEDS HUMAN:`) are yours.

You may define more categories of your own (a cheaper one for mechanical nodes, a stronger one for a hard node) by adding
a key to `[routes]` and writing the category in the node, or in a role. A run whose plan needs a route that is still a
placeholder stops before it starts and names it. `pangoloop init --route <category>=<model@provider>` (repeatable) fills routes
when it writes the file, and `--reader <name>=<model@provider>` the readers.

## Reading a run from a program

`pangoloop status` and `pangoloop requests` have JSON forms, so that a script or another agent can drive a run with commands and no
prompt. Their fields are a contract. Times are UTC, `2026-11-20T10:00:00Z`; a field with no value is `null`.

`pangoloop status --json` prints one object for the project:

```
{"project": "<root>" | null, "page": "http://127.0.0.1:5420/" | null, "runs_folder": "<path>",
 "runs": [{"name", "state", "verdict", "started", "ended", "waiting_for", "open_requests"}, ...]}
```

newest run first. `state` is one of `running` (the engine's process is alive; a run waiting for an answer is running),
`verified`, `not verified`, `aborted` (a person answered ABORT) and `stopped` (no result and no engine: it was stopped or killed;
`pangoloop resume <run>` continues it). `verdict` is the engine's word (`verified`, `not_verified`, `stopped_by_human`,
`no_answer_from_human`, `stopped`) or null while the run has none. `waiting_for` is `person`, `coordinator` or null.

`pangoloop status <run> --json` prints one object for a run:

```
{"name", "state", "verdict", "started", "ended", "folder", "plan", "repo", "branch", "runner", "page", "waiting_for",
 "reviews": ["PASS", ...], "cost": {"usd", "partial", "workers", "workers_with_cost", "limit_usd", ...},
 "nodes": [{"id", "kind", "role", "label", "state", "depends_on": [...], "workers"}, ...],
 "requests": [{"id", "node", "for", "question", "asked_at"}, ...]}
```

A node's `state` is `waiting`, `running`, `integrating`, `accepted`, `failed`, `waiting for human` or `waiting for coordinator`.
`requests` are the open ones. `cost.usd` is the sum of the known costs (null when none is known) and `cost.partial` says some
worker's cost is unknown. `page` is the address of the page, or null. Exit status 0; 2 when the run does not exist.

`pangoloop requests list <run> --json` prints one JSON object per line, oldest first, for every request of the run (nothing when
there are none); `pangoloop requests wait <run> --json [--for person|coordinator|any]` waits and prints one line, the oldest open
request, and exits 0; when the run is over it prints `{"run_over": true, "run": "<name>", "verdict": "<word>" | null}` and exits 3.
A request object has the fields `id` (such as `a-1`: the node and a number), `node`, `for` (`person` or `coordinator`), `question`
(the node's words), `asked_at`, and `state` (`open`, `answered`, or what the engine recorded), `worker`, `worker_kind`,
`answered_at`, `answered_by`. Answer with `pangoloop requests answer <run> <id> --text "..." [--by person]`.

## How many nodes side by side

`max_parallel` is the number of workers at once. Start with two or three. More nodes in parallel finish a plan sooner only if
the nodes really are independent, and they use your model account's rate limits: if the provider refuses sessions, workers slow down or fail.
Raise it when a run shows nodes waiting for a slot while the
machine and the account are idle; lower it when you see rate refusals or when rebases keep colliding.

## Limits, and how to choose first values

- **Per node:** `max_turns` (how many model calls one worker may make) and `node_limit_seconds`. Begin generous: a limit that
  cuts a worker in the middle wastes what it did. Then, after a few runs, set them to a bit more than a normal node needs,
  so that a worker that is lost is stopped soon. The first value is a guess; the run records what each worker used, so
  you can correct it.
- **Per run:** `max_fix_rounds` (repair rounds after a review asks for changes; two is the default) and
  `max_parallel`.
- **Cost:** `max_cost_usd` stops new workers once the run's known cost is over it. It does not interrupt a worker that is
  running, and a model with no price in the table is not counted, so it is a brake and not a guarantee. Set it from what
  you would be willing to lose on the first run, not from what you expect to spend. A command the engine runs (a script
  step, an acceptance command) that calls a model itself can report the money with an output line that starts with
  `PANGOLOOP-SPEND: <usd> [label]`; the limit counts it. The figure is what the command says, not something checked.

## Prices and the cost limit

`[prices]` maps a model id (or a prefix of one) to dollars per million tokens: input, output, cache read, cache write. The
numbers are yours to copy from the provider's price list; the presets show only the shape. The key is the id the provider
reports, not your alias. Without a price a worker's cost is unknown and the run says its cost is partial. Keep the table
current: a stale price makes the cost limit mean less than it says.

## Node worktrees

Turn them on (`SG_NODE_WORKTREES=1`; the short `run` does) for any plan with more than one build node. Each build node then works
in its own git worktree and its work is integrated by rebase, so two nodes cannot trample each other's files, and a node that
changed nothing is not accepted. Without them all nodes share one folder, which is only safe for a plan whose nodes never overlap in
time and files.

## What to put in a plan's background

The background is read by every node, so it says how to work, not what must be true (that is the statement, at the end).
Put there:

- the repository's conventions in a few lines, and the exact commands that check the work, with the usual time of each;
- what never to run (the package installer, anything that rewrites tracked files, anything on a port you use);
- how to delete and move files in this setup, and what a node should do when a rule seems to contradict another: stop and
  ask, with the rule's number;
- a paragraph where the coordinator will append rulings.

Give every node the exact commands it may use to check itself, and say that reading files and comparing them is not a
check. Run `pangoloop preflight` on the finished plan, fix what the readers find, and run it again. `prompts/STAGE_AUTHOR.md`
is a longer recommendation on preparing a plan.

## The deny list, and what it does not protect

With the Claude Code runner a worker is started in `dontAsk` mode: it may use the file tools (read, edit, write, find) and the
shell, and anything else is refused instead of asked about. On top of that the runner passes a deny list of Claude Code permission
patterns for shell commands: `git push`, `sudo`, `rm -rf`, `ssh`, `kubectl`, `terraform apply` and some others. A command is
checked piece by piece (a compound command is split at `&&`, `||`, `;`, `|`, `&` and newlines), and a refused command is returned
to the model as an error, so it can try something else. Add your own patterns under `[runner.claude_code] deny`, for example the
deploy command of your project; `default_deny = false` switches the built-in list off.

Treat it as a seat belt, not a sandbox:

- it matches the forms a model usually writes. Other forms of the same command (`/usr/bin/git push`, `sh -c '...'`,
  `git -C . push`, a script that pushes) get through unless a pattern names them;
- it limits commands, not files. A worker runs as you, can read files outside its worktree (your ssh configuration, other
  projects, credentials in your home folder) and write anywhere you can, and it can reach the network with any command the list does
  not name;
- of the patterns, only `git push` has been seen refused in a real call; the others rest on the documentation of the permission
  syntax.

So do not point it at a repository that holds secrets, and do not run it as an account that holds credentials you would mind
losing, until you have isolated that account (a separate user, a container or a virtual machine). With Hermes, what a worker may do
is set by the tool sets the engine passes and by the Hermes profile you name; read both.

## Where the run folder lives

In a project, runs go to `.pangoloop/runs/`, which `pangoloop init` told git to ignore; move it with `[paths] runs` or
`PANGOLOOP_RUNS`. Without a project (the full form of `run`, called from any folder) the default is `./.pangoloop/runs` in the folder you
start from. A run folder holds the plan, every worker's prompt, answer and event stream, and the state a resume reads: it can be
large and it can contain whatever your nodes read. Keep it on a disk with room and not in a folder that is synced or published.
Keep a run's folder until you have looked at it and, if you may resume, until the work is accepted. `pangoloop view` reads the
folders and shows them.

## A first week

Run the toy plan with no model (`TOY_STUB=1 sh examples/toy/run.sh`), then with one model on the toy, then on a small plan
of your own with two or three nodes in a project made by `pangoloop init`, a cost limit and `max_parallel` of two. Look at the page
after each. Change one setting at a time.
