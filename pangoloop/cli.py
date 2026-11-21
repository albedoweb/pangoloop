"""The command line: python3 -m pangoloop <command>. Commands: init, run, resume, cleanup, status, view, preflight, requests,
roles, steps, config, version."""
import os
import sys

import pangoloop

USAGE = '''usage: python3 -m pangoloop <command> [arguments]

commands:
  init       make this folder a project: pangoloop.toml, .pangoloop/{runs,plans,roles}, the example roles (asks nothing)
  run        run a plan as a graph of agent nodes: `run <plan>` in a project, `run <plan> <repo> <name>` anywhere
             (--detach starts it in the background; --resume continues a run that was not verified)
  resume     continue such a run by its name: `resume <run name>`
  cleanup    stop what a killed run left running (workers, commands), for a run that will not be resumed
  status     the project's runs, or one run's nodes, open requests, cost and verdict (--json for a program)
  view       serve the live page of the project's runs on 127.0.0.1
  roles      list the project's roles (who a worker is), or show one
  preflight  have reader models check instructions for contradictions before a run
  requests   list, wait for, and answer the requests nodes make to a person
  steps      check a script node's steps file against a folder (`steps check`), changing nothing
  config     print the effective configuration and where each value came from
  version    print the version

A project is a git repository with a `pangoloop.toml` at its root. Commands find it from the current folder upward (they stop at
the home folder); `--project <path>` (or $PANGOLOOP_PROJECT) names it. `--config <file>` reads that configuration file instead
of the project's.

`python3 -m pangoloop <command> --help` says more about one command.
'''

RUN_HELP = '''usage: pangoloop run <plan> [--detach [--json]] [--view] [--name <run name>]
       pangoloop run <plan file> <repository folder> <run name> [--detach] [--resume [--reset-rounds] [--accept-plan-changes]]

The short form runs in the current project (see `pangoloop init`): the repository is the project's folder, the run is named from
the plan's file name and a time stamp (printed), and a plan given by a bare name is looked for in .pangoloop/plans/ (<name> or
<name>.md). It sets SG_NODE_WORKTREES=1, SG_HUMAN=1 and SG_NODE_SCRATCH=1 unless you set them: each build node works in its own
git worktree, nodes can ask (`pangoloop requests`), temporary files stay in the run folder. The full form runs exactly as it
always did, with the variables you give it.

Runs a plan (Markdown with a "## Nodes" section) as a graph: each node a task for a model session, in its own git
worktree when SG_NODE_WORKTREES=1, then the scripted acceptance and the reviews. Prints one line per session and a
final `verdict=... reviews=... nodes=...` line; the run is kept under <runs folder>/<run name>/.
--detach starts the engine as a process of its own that survives this command, and returns at once (exit 0) when the run's
folder exists, printing the run's name, its folder, the engine's log (<run folder>/engine.log) and the page's address when a page
is serving the project; with --json that is one JSON object {run, folder, log, pid, page, page_run}. A plan or configuration
fault is reported by this command (exit 2), not left in the log. --view also makes sure a page serves the project
(`pangoloop view` in the background) and prints its address.
The settings are the configuration file (the project's pangoloop.toml or --config <file>) and environment variables (SG_*,
PANGOLOOP_RUNS, PANGOLOOP_RUNNER, PANGOLOOP_MAX_COST_USD; they win over the file); `pangoloop config` prints what is in effect.
The runs folder is $PANGOLOOP_RUNS, else the project's, else ./.pangoloop/runs under the current folder.

exit codes:
  0    the run is verified (with --detach: the run started)
  1    the run ended and is not verified (a node failed, the review did not pass, the acceptance failed,
       nobody answered a request in time, the cost limit was reached: result.json says reason "cost limit")
  2    a plan or configuration error, found before anything ran (also a wrong number of arguments), a run
       name already used, an unknown role, or a refused resume (the run is verified, is still running, is not a run folder,
       the repository or branch is not the run's, the plan changed under an accepted node)
  3    a person aborted the run (ABORT)
  124  stopped by SIGTERM    130  stopped by Ctrl-C (SIGINT)
  In both stops the workers are stopped and finished_at and result.json (verdict "stopped") are written.

--resume continues the run <run name> in its folder (same repository and branch) when it ended before it was verified:
accepted nodes are skipped, the rest runs again. --reset-rounds starts the review and fix round counters at zero again;
--accept-plan-changes lets the plan differ from the earlier one under accepted nodes, and records it.

example:
  pangoloop run build-feature --detach --view
  SG_NODE_WORKTREES=1 SG_HUMAN=1 python3 -m pangoloop run examples/toy/plan.md /path/to/repo try1
'''

CLEANUP_HELP = '''usage: python3 -m pangoloop cleanup <run name>

Stops what the engine of a run started and left running when it was killed (`kill -9`): workers, script steps, acceptance
commands and integration checks. The engine records each child it starts in the run's state.json (pid, start time, what it
is); this command stops the recorded ones that are still alive AND are provably the same process (the start time the
system reports must equal the recorded one: a process is never stopped by its pid alone) and says what it stopped. A
`--resume` does the same before anything else; use `cleanup` for a run that will not be resumed. Refused (exit status 2)
while a live engine still runs the folder, or when the folder is not a run. Exit status 0 otherwise, also when nothing was
recorded.

example:
  python3 -m pangoloop cleanup try1
'''

RESUME_HELP = '''usage: python3 -m pangoloop resume <run name> [--plan <plan file>] [--reset-rounds] [--accept-plan-changes]

Continues a run that ended before it was verified, as `run <plan> <repository> <run name> --resume` does, reading the
repository from the run's state file and the plan from the run folder (the plan as it was given) unless --plan names
another.
'''

VIEW_HELP = '''usage: pangoloop view [<run name>] [--port N]

Serves a live page of the project's runs on http://127.0.0.1:<port>/ (graph, timeline, a node's task and tail, acceptance,
review) and takes a person's answer to an open request. It prints that address as its first line. The port is --port, else
[view] port of the configuration, else the first free port from 5420 upward. In a project it writes
<project>/.pangoloop/view.json (pid, port, started), and a second `view` for the same project says "already serving at <address>"
instead of starting another; a file left by a process that is gone is replaced.
It listens on 127.0.0.1 only and has NO LOGIN: anyone who can reach that address on this machine can read the runs and answer
a request. Do not forward the port or expose it.
Reads $RUN_VIEW_RUNS, else $PANGOLOOP_RUNS, else the project's runs, else ./.pangoloop/runs under the current folder.
With a run name the page opens on that run; without one, on the newest started.

example:
  pangoloop view
  pangoloop view try1 --port 5430
'''


CONFIG_HELP = '''usage: python3 -m pangoloop config [--config <file>]

Prints the effective configuration (the runner, the routes, the runner's own settings, the limits) and, for each value,
where it came from: an environment variable, the configuration file, or the built-in default. The file is
--config <file>, else $PANGOLOOP_CONFIG, else ./pangoloop.toml, else none. Exit status 2 when the file is wrong.

example:
  python3 -m pangoloop config --config examples/presets/claude-code-only.toml
'''


def take_config(rest: list[str]) -> list[str]:
    """Removes `--config <file>` (or `--config=<file>`) from `rest` and hands it to the loaders as PANGOLOOP_CONFIG."""
    out, index = [], 0
    while index < len(rest):
        if rest[index] == '--config':
            if index + 1 >= len(rest):
                raise SystemExit('pangoloop: --config needs a file')
            os.environ['PANGOLOOP_CONFIG'] = rest[index + 1]
            index += 2
        elif rest[index].startswith('--config='):
            os.environ['PANGOLOOP_CONFIG'] = rest[index].split('=', 1)[1]
            index += 1
        else:
            out.append(rest[index])
            index += 1
    return out


def cleanup_command(rest: list[str]) -> int:
    """`pangoloop cleanup <run name>`: stops the children recorded in the run's state that are still the same processes."""
    rest = take_config(rest)
    if rest[:1] in (['-h'], ['--help']):
        print(CLEANUP_HELP)
        return 0
    if len(rest) != 1 or rest[0].startswith('--'):
        print(CLEANUP_HELP, file=sys.stderr)
        return 2
    from pangoloop import children, paths, resume
    folder = os.path.join(paths.runs_folder(), rest[0])
    state = resume.read_state(folder)
    if state is None:
        print(f'pangoloop: cannot clean up: {folder} is not a run folder with a state file', file=sys.stderr)
        return 2
    held = resume.read_json(os.path.join(folder, resume.LOCK_FILE))
    if isinstance(held, dict) and resume.process_alive(held.get('pid'), held.get('process_started')):
        print(f"pangoloop: the run is being run by process {held.get('pid')} (started {held.get('started_at')}); "
              'nothing was stopped. Stop that engine first', file=sys.stderr)
        return 2
    recorded = state.get('children') or {}
    lines = children.stop_recorded(recorded)
    for line in lines:
        print(line)
    if not lines:
        print('nothing recorded is still running' if recorded else 'no children recorded')
    if recorded:
        state['children'] = {}
        resume.write_state(folder, state)
    return 0


def resume_command(rest: list[str]) -> int:
    """`pangoloop resume <run name> [--plan <file>] [--reset-rounds] [--accept-plan-changes]`: `run --resume` with the plan
    and the repository read from the run folder (the plan as it was given, or the one named by --plan)."""
    rest = take_config(rest)
    if os.environ.get('PANGOLOOP_PROJECT'):   # in a project a resume is set up as the short `run` was
        for name, value in (('SG_NODE_WORKTREES', '1'), ('SG_HUMAN', '1'), ('SG_NODE_SCRATCH', '1')):
            os.environ.setdefault(name, value)
    plan, flags, names = None, [], []
    index = 0
    while index < len(rest):
        if rest[index] == '--plan' and index + 1 < len(rest):
            plan = rest[index + 1]
            index += 2
            continue
        (flags if rest[index].startswith('--') else names).append(rest[index])
        index += 1
    if len(names) != 1:
        print(RESUME_HELP, file=sys.stderr)
        return 2
    from pangoloop import graph, resume
    folder = f'{graph.RUNS}/{names[0]}'
    state = resume.read_state(folder)
    if state is None:
        print(f'pangoloop: cannot resume: {folder} has no state file (a run written before resume existed); '
              f'use `run <plan> <repository> {names[0]} --resume`', file=sys.stderr)
        return 2
    if plan is None:
        plan = os.path.join(folder, resume.SOURCE_FILE)
        if not os.path.isfile(plan):
            plan = os.path.join(folder, 'plan.md')
    return graph.main([plan, state['repo'], names[0], '--resume', *flags])


def main(argv: list[str] | None = None) -> int:
    """Runs the command; a message that stops it before anything ran (a bad configuration, a missing file) is printed
    and the status is 2."""
    try:
        return dispatch(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as stop:
        if isinstance(stop.code, str):
            print(stop.code, file=sys.stderr, flush=True)
            return 2
        raise


def take_project(rest: list[str]) -> tuple[list[str], str | None]:
    """Removes `--project <path>` (or `--project=<path>`) from `rest`; returns the rest and the path (None when not given)."""
    out, explicit, index = [], None, 0
    while index < len(rest):
        if rest[index] == '--project':
            if index + 1 >= len(rest):
                raise SystemExit('pangoloop: --project needs a folder')
            explicit = rest[index + 1]
            index += 2
        elif rest[index].startswith('--project='):
            explicit = rest[index].split('=', 1)[1]
            index += 1
        else:
            out.append(rest[index])
            index += 1
    return out, explicit


def activate(explicit: str | None) -> str | None:
    """Finds the project (see pangoloop/project.py) and makes it the one this process sees; its root, or None."""
    from pangoloop import project
    root = project.resolve(explicit)
    if root:
        project.apply(root)
    return root


def take_flags(rest: list[str], names: tuple[str, ...]) -> tuple[list[str], set[str]]:
    """Removes the given flags (those without a value) from `rest`; returns the rest and the flags found."""
    return [item for item in rest if item not in names], {item for item in rest if item in names}


def take_value(rest: list[str], name: str) -> tuple[list[str], str | None]:
    """Removes `<name> <value>` (or `<name>=<value>`) from `rest`; the value, or None."""
    out, value, index = [], None, 0
    while index < len(rest):
        if rest[index] == name:
            if index + 1 >= len(rest):
                raise SystemExit(f'pangoloop: {name} needs a value')
            value = rest[index + 1]
            index += 2
        elif rest[index].startswith(name + '='):
            value = rest[index].split('=', 1)[1]
            index += 1
        else:
            out.append(rest[index])
            index += 1
    return out, value


def run_name(plan: str, runs: str, now: float | None = None) -> str:
    """`<plan file name>-<YYYYMMDD-HHMMSS>` (letters, digits, dots, dashes, underscores); a number is added when it is taken."""
    import re
    import time
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', os.path.splitext(os.path.basename(plan))[0]).strip('-.') or 'run'
    base = f"{stem}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}"
    name, number = base, 1
    while os.path.exists(os.path.join(runs, name)):
        number += 1
        name = f'{base}-{number}'
    return name


def find_plan(given: str) -> str:
    """The plan file for what `run` was given: that file when it exists, else `<name>` or `<name>.md` in the plans folder."""
    from pangoloop import paths
    if os.path.isfile(given):
        return os.path.abspath(given)
    folder = paths.plans_folder()
    for candidate in (given, given + '.md'):
        path = os.path.join(folder, candidate)
        if not os.sep in candidate and os.path.isfile(path):
            return path
    have = sorted(name for name in os.listdir(folder) if name.endswith('.md') and name.lower() != 'readme.md') \
        if os.path.isdir(folder) else []
    raise SystemExit(f'pangoloop: no plan {given!r}: it is not a file, and {folder} has '
                     + (', '.join(have) if have else 'no plans'))


def run_command(rest: list[str], root: str | None) -> int:
    from pangoloop import detach, paths, viewfile
    rest, flags = take_flags(rest, ('--detach', '--view', '--json'))
    rest, named = take_value(rest, '--name')
    positional = [item for item in rest if not item.startswith('--')]
    engine_flags = [item for item in rest if item.startswith('--')]
    if len(positional) not in (1, 3):
        print(RUN_HELP, file=sys.stderr)
        return 2
    if len(positional) == 1:
        if root is None:
            print('pangoloop: no project here: there is no pangoloop.toml in this folder or above it. Run `pangoloop init` '
                  'in the repository, or give the full form `run <plan file> <repository folder> <run name>`', file=sys.stderr)
            return 2
        for name, value in (('SG_NODE_WORKTREES', '1'), ('SG_HUMAN', '1'), ('SG_NODE_SCRATCH', '1')):
            os.environ.setdefault(name, value)
        plan = find_plan(positional[0])
        runs = paths.runs_folder()
        name = named or run_name(plan, runs)
        arguments = [plan, root, name]
    else:
        if named:
            print('pangoloop: --name belongs to the short form; the full form has the run name as its third argument', file=sys.stderr)
            return 2
        arguments = positional
        name, runs = positional[2], paths.runs_folder()
    if '/' in name or name.startswith('.'):
        print(f'pangoloop: a run name is one word of letters, digits, dots and dashes, not {name!r}', file=sys.stderr)
        return 2
    if 'view' in {item.lstrip('-') for item in flags}:
        try:
            if root is None:
                raise detach.Failed('pangoloop: --view needs a project (the page is the project\'s)', 2)
            detach.ensure_view(root)
        except detach.Failed as failure:
            print(failure.message, file=sys.stderr)
            return failure.code
    if '--detach' not in flags:
        if len(positional) == 1:
            print(f'run: {name}', flush=True)
        from pangoloop import graph
        return graph.main([*arguments, *engine_flags])
    try:
        info = detach.start_engine([*arguments, *engine_flags], runs, name, root or os.getcwd())
    except detach.Failed as failure:
        print(failure.message, file=sys.stderr)
        return failure.code
    print(detach.describe(info, name, viewfile.address(root), '--json' in flags))
    return 0


def dispatch(argv: list[str]) -> int:
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command == 'version':
        print(f'pangoloop {pangoloop.__version__}')
        return 0
    if command == 'init':
        from pangoloop import initproject
        rest, explicit = take_project(rest)
        if explicit and not [item for item in rest if not item.startswith('-')]:
            rest = [explicit, *rest]
        if rest[:1] in (['-h'], ['--help']):
            initproject.build_parser().print_help()
            return 0
        return initproject.main(rest)
    known = ('run', 'resume', 'cleanup', 'status', 'view', 'preflight', 'requests', 'roles', 'steps', 'config')
    if command not in known:
        print(f'pangoloop: unknown command {command!r}\n', file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    rest = take_config(rest)
    rest, explicit = take_project(rest)
    helping = rest[:1] in (['-h'], ['--help'])
    root = None if helping else activate(explicit)
    if command == 'config':
        if helping:
            print(CONFIG_HELP)
            return 0
        from pangoloop import config
        print(config.load().describe())
        return 0
    if command == 'run':
        if helping:
            print(RUN_HELP)
            return 0
        return run_command(rest, root)
    if command == 'cleanup':
        return cleanup_command(rest)
    if command == 'resume':
        if helping:
            print(RESUME_HELP)
            return 0
        return resume_command(rest)
    if command == 'view':
        if helping:
            print(VIEW_HELP)
            return 0
        from pangoloop import view
        return view.main(rest) or 0
    if command == 'status':
        from pangoloop import status
        return status.main(rest)
    if command == 'roles':
        from pangoloop import roles
        return roles.main(rest)
    if command == 'preflight':
        from pangoloop import preflight
        return preflight.main(rest)
    if command == 'steps':
        from pangoloop import steps
        if rest[:1] == ['check']:
            return steps.check_main(rest[1:])
        print(steps.CHECK_HELP, file=sys.stderr)
        return 0 if helping else 2
    from pangoloop import requests
    return requests.main(rest)
