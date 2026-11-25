"""Runs one accepted plan as a graph of nodes with a script in the lead's place. No model leads the run.

    python3 -m pangoloop run <plan source file> <repository folder> <run name>

(Moved from script_graph.py into the package on November 18, 2026; the logic is unchanged. The runs folder is
$PANGOLOOP_RUNS when set, else <current folder>/.pangoloop/runs (pangoloop/paths.py). The model's command line and the
reading of its events live in pangoloop/runners/, chosen by PANGOLOOP_RUNNER, default `hermes`. The plan format (the
nodes' fields, the routes, the patterns) lives in pangoloop/plan.py, shared with the viewer.)

The plan is the same file a dag-ulw run gets (run_graph.sh): the same nodes, dependencies, categories and
contract lines. The difference is who hands nodes out and accepts them. In a dag-ulw run that is a lead session
on a model; here it is this script, with the rules the lead was given in GRAPH_MESSAGE.txt written as code:

  - a node becomes ready when every node it depends on is accepted; all ready nodes start together,
    at most MAX_PARALLEL at a time;
  - every node is done by one worker: a separate one-shot Hermes session on the model its category names
    (the models the dag-ulw children ran on), with the file and terminal tools and no delegation;
  - a node's prompt is the pointer to the plan file plus the node's five contract lines, word for word;
  - a worker that exits with an error or says nothing is retried, at most MAX_REPAIRS times per node;
  - a review node is accepted only with the verdict PASS. On CHANGES REQUESTED a fix worker gets the
    findings word for word, then the review runs again; at most MAX_REPAIRS fix rounds;
  - the verify node starts only after a review with PASS; otherwise the run ends "not_verified".

Everything is kept under <runs folder>/<run name>/: the plan as run, one folder of events per worker, result.json.

A node with the role `acceptance` (added 2026-11-16 at the owner's instruction: nodes check themselves cheaply, the
full acceptance runs once at the end, and a failure goes back to one node that may change anything) is done by the
script itself, with no model: it runs the backticked commands of the node's VERIFY line in the repository folder,
each with a limit of SG_ACCEPTANCE_SECONDS (default 1800; a command past the limit is stopped with everything under
it and counts as failed). When a command fails, one fix worker gets the failing commands with the end of their
output and may change any file the task allows; then every command runs again, at most MAX_FIX_ROUNDS times. A
plan can have the node twice: after the build nodes, and after the review instead of a model that reruns commands.

With SG_NODE_WORKTREES=1 (added 2026-11-15 at the owner's instruction: nodes must not disturb each other, and on a
conflict the node that is rebased resolves it) every build node works in its own git worktree:

  - accepted work lives as commits on the repository's branch. A build node gets the branch `sg/<node>` and the
    folder <runs folder>/<run name>/wt/<node>, made from everything accepted when it starts, with its own copy of
    node_modules and its own copy of the plan;
  - when its worker ends, the script commits the node's work, rebases it onto the work accepted meanwhile and moves
    the branch forward. One node is integrated at a time;
  - if the rebase conflicts, a new session of the same node gets the conflicted files, removes the markers and runs
    its VERIFY again; the script, not the worker, continues the rebase;
  - if the accepted work moved and SG_INTEGRATION_CHECK names a shell command, the script runs it in the node's
    copy after the rebase; when it fails, the same node gets its output and repairs;
  - nodes that are not build nodes (baseline, review and its fixes, verify) run in the repository folder itself,
    where the accepted work is shown as one uncommitted change, as the plans say. The run ends in that state, so
    the evaluator and the grader see what they saw before.
"""
import concurrent.futures
import copy
import datetime
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from pangoloop import children as procs, config, costs, paths, resume, roles as role_files, rulings, steps
from pangoloop.plan import (CATEGORY_TOOLSETS, must_change, CONTRACT, DEFAULT_MAX_FIX_ROUNDS, DEFAULT_ROUTE, FIELDS, FINDING_LINE,
                           FOR_COORDINATOR, FOR_PERSON, MAX_REPAIRS, ASK_LINE, ROUTES, TOOLSETS, VERDICT, find_request,
                           ignored_request, kind_of, parse_nodes, person_commands, placeholder_routes, worker_kind)
from pangoloop.runners import get_runner

# Three workers at a time, as the delegated children of a dag-ulw lead ran (delegation.max_concurrent_children).
# The workers are top-level Hermes sessions, which Hermes counts against max_concurrent_sessions of the user's
# general settings; raise that limit at least to the width of a run.
# SG_MAX_PARALLEL overrides the width for a control run (added 2026-11-14: the six-node cut with one worker at a
# time, to separate the number of nodes from the width); without it the width is three, as in every earlier run.
# The settings of step 3: the configuration file (pangoloop.toml) and the environment, read once here; the runner, the
# routes and the numeric limits come from it (pangoloop/config.py), each as it was when there is neither.
CONFIG = config.load()
LIMITS = CONFIG.limits
ROUTES, DEFAULT_ROUTE = CONFIG.routes_table()   # the very objects of pangoloop.plan when nothing changed them
MAX_PARALLEL = LIMITS['max_parallel']
PRICES = CONFIG.prices                   # dollars per million tokens per model (pangoloop/costs.py)
MAX_COST_USD = LIMITS['max_cost_usd']    # None: no limit
# Where the runs are kept: PANGOLOOP_RUNS, or <current folder>/.pangoloop/runs (the same default as the viewer's).
RUNS = paths.runs_folder()
# The runner builds the model's command line and reads its event stream (added with the package, 2026-11-18).
RUNNER = get_runner(CONFIG.runner, CONFIG.settings())   # the refusal and its wait loop belong to the runner
START_GAP_SECONDS = 4        # sessions that start in the same instant refuse each other
# Switches for a larger task. Each defaults to the value every earlier run had, so a run that sets none of them
# behaves as before.
#   SG_MAX_FIX_ROUNDS      fix rounds after a review that requests changes
#   SG_NODE_LIMIT_SECONDS  the longest one worker session may run
#   SG_MAX_TURNS           the turn cap of one worker session
MAX_FIX_ROUNDS = LIMITS['max_fix_rounds']
NODE_LIMIT_SECONDS = LIMITS['node_limit_seconds']
MAX_TURNS = str(LIMITS['max_turns'])   # the same cap for every worker, so a coarse node is not cut short
# The routes, the tool sets, the node fields and the patterns are in pangoloop/plan.py (imported above).


NODE_WORKTREES = os.environ.get('SG_NODE_WORKTREES', '0') == '1'
INTEGRATION_CHECK = os.environ.get('SG_INTEGRATION_CHECK', '').strip()   # a shell command, or nothing
INTEGRATION_CHECK_SECONDS = LIMITS['integration_check_seconds']
ACCEPTANCE_SECONDS = LIMITS['acceptance_seconds']   # limit of one command of an acceptance node
RULINGS_CHARS = LIMITS['rulings_chars']   # up to this many characters every ruling goes into a prompt in full
# With SG_NODE_SCRATCH=1 (added 2026-11-17) every prompt names an own, empty scratch folder of the node, made under
# <runs folder>/<run name>/scratch/<node>; a second prompt for the same node gets a fresh folder with a suffix.
NODE_SCRATCH = os.environ.get('SG_NODE_SCRATCH', '0') == '1'
# With SG_HUMAN=1 (added 2026-11-17) a person can step into the run. A worker whose final answer has a line
# `NEEDS HUMAN: <request>` does not finish its node: the request is written to runs/<run name>/human/<node>-<k>.json and
# the node waits until runs/<run name>/human/<node>-<k>.answer.txt appears (the viewer's buttons write it); the node is
# then continued by a new worker that is given the request and the answer. A first answer line `ABORT` ends the run. A
# plan node with `role: human` has no worker: its TASK is the request, any answer but ABORT accepts it. Without the
# switch none of this exists: prompts, statuses and result.json are what they were.
#   SG_HUMAN_WAIT_SECONDS  an open request older than this ends the run as "no answer from the human" (default 86400)
HUMAN = os.environ.get('SG_HUMAN', '0') == '1'
HUMAN_WAIT_SECONDS = LIMITS['human_wait_seconds']
HUMAN_POLL_SECONDS = 2
# A request for the coordinator (`NEEDS COORDINATOR:`) that nobody answers within this many seconds becomes a request
# for the person, with the note "no coordinator answered in N minutes" (added in step 2).
COORDINATOR_WAIT_SECONDS = float(LIMITS['coordinator_wait_seconds'])
# SG_HUMAN_OPEN=1 (opt-in): when a request opens, open the run's page of the viewer in the default browser, once per
# request; with `terminal-notifier` installed, raise a notification whose click opens it instead. SG_VIEW_PORT is the
# viewer's port (default 5420); if nothing listens there, nothing happens.
HUMAN_OPEN = os.environ.get('SG_HUMAN_OPEN', '0') == '1'
VIEW_PORT = int(os.environ.get('SG_VIEW_PORT', '5420'))
HUMAN_PARAGRAPH = (
    'If something stops you, stop and ask; never look for a way around a refusal. A command that the tool refuses '
    '(it says BLOCKED, or that permission was denied) is such a stop: do not look for another way to the same result. '
    'When you are blocked, and only then, finish your answer with one last line of the form below, and you will be '
    'called again with the answer. A node that is not blocked writes no such line at all: not "NEEDS COORDINATOR: '
    'none", not a line that says nothing is needed. There are two kinds of question, and you choose by what is '
    'needed.\n'
    '- `NEEDS COORDINATOR: <what and why>`: ask the coordinator, the one who wrote the plan, when you need a fact you '
    'cannot find, a measurement you cannot take, a clarification of the plan, or an ordinary operation the plan requires '
    'that your session is not allowed to do (for example deleting folders when the tool refuses recursive deletes). No '
    'person is called.\n'
    '- `NEEDS HUMAN: <what and why>`: ask the person, and only the person, when a command that the plan marks as the '
    "person's has to be run, when something outside this repository or on a live system is involved, or when something "
    'cannot be undone. If you are not sure which of the two it is, ask the person.\n'
    'In both cases do not work around it and do not guess.'
)
PERSON_COMMANDS_INTRO = ("The plan marks these as the person's commands; do not run them yourself, ask the person for "
                         'them with `NEEDS HUMAN:`:')


COST_LIMIT = 'cost limit'   # the reason of a run (and of a node) that no new worker was started for, over max_cost_usd


class CostLimit(Exception):
    """A worker was about to start, and the run's known cost is over max_cost_usd: no new worker starts."""


class HumanStop(Exception):
    """The run ends here: the human aborted it, or an open request got no answer in time."""


INTEGRATION_LOCK = threading.Lock()   # one node at a time takes a copy of, or adds to, the accepted work
SHARED_FOLDER_LINE = r'(?m)^All nodes work in the same folder.*$'
OWN_COPY_LINES = (
    'Every build node works in its own copy of the repository, made from all the work accepted before it started. '
    'No other node edits your copy: a check that fails there fails because of your work or of accepted work, never '
    'because a neighbour is in the middle of an edit. You leave your changes uncommitted, as the rules above say; '
    'when you finish, the coordinator itself records them and puts them on top of the work accepted meanwhile. If '
    'they conflict, your node is called once more to resolve the conflict. Never edit a file outside your write '
    'scope.',
    'Build nodes worked each in its own copy of the repository. Their accepted work is gathered in this folder as '
    'one uncommitted change.',
)
LIVE: set = set()  # workers running now; each is the leader of its own process group
START_LOCK = threading.Lock()
CURRENT = None     # the Run in progress, set by main, so that stop_workers can leave its facts


NO_CHANGE = 'no change'   # the reason of a build node that finished with an empty change (see plan.must_change)


SPAWN_LOCK = threading.Lock()   # a child starts, or the stop sweep runs, never both at once
STOPPING = False                # set by stop_workers: from then on no child may start (a node's thread that was still
                                # going when the sweep ran started a repair worker and left it behind, found by a test)


def start_process(*args, live: bool = False, **kwargs) -> subprocess.Popen:
    """Starts a child unless the run is being stopped, and registers it at once (LIVE for a worker, COMMANDS for a command)
    so that the stop sweep, which takes the same lock, cannot miss it."""
    with SPAWN_LOCK:
        if STOPPING:
            raise RuntimeError('the run is being stopped: nothing new is started')
        process = subprocess.Popen(*args, **kwargs)
        if live:
            LIVE.add(process)
        else:
            COMMANDS.add(process.pid)
        return process


COMMANDS: set = set()  # process ids of acceptance commands the script itself is running now


def kill_tree(root: int) -> None:
    """Stops a process and everything under it, children first found by walking the process table."""
    for sign in (signal.SIGTERM, signal.SIGKILL):
        listing = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True).stdout
        children: dict[int, list[int]] = {}
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
        tree, queue = [], [root]
        while queue:
            pid = queue.pop()
            tree.append(pid)
            queue.extend(children.get(pid, []))
        alive = False
        for pid in reversed(tree):
            try:
                os.kill(pid, sign)
                alive = True
            except (ProcessLookupError, PermissionError):
                pass
        if not alive:
            return
        time.sleep(2)


def copy_packages(source: str, target: str) -> None:
    """Copies a folder of installed packages into a node's copy, as cheaply as the system allows: on macOS a clone
    (`cp -Rc`: no data is copied until a file changes); elsewhere `cp -R --reflink=auto`, which clones where the file
    system can and copies where it cannot, and a plain `cp -R` for a `cp` that does not know that option. Fails with
    CalledProcessError only when the plain copy fails too. (Until November 24, 2026 it was `cp -Rc` everywhere,
    which GNU cp refuses: on Linux every node of a project with node_modules failed.)"""
    if sys.platform == 'darwin':
        subprocess.run(['cp', '-Rc', source, target], check=True)
        return
    if subprocess.run(['cp', '-R', '--reflink=auto', source, target], capture_output=True).returncode == 0:
        return
    shutil.rmtree(target, ignore_errors=True)   # whatever the refused command left
    subprocess.run(['cp', '-R', source, target], check=True)


def kill_command(pid: int) -> None:
    """Stops a command the engine runs (the leader of a group of its own) with everything under it: the tree found by the
    process table (a child that left the group is found that way) and then the group itself."""
    try:
        kill_tree(pid)
    except BaseException:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def stop_workers(signum: int | None = None, frame: object = None) -> None:
    """When the run itself is stopped (SIGTERM, the attempt's time limit; or SIGINT, Ctrl-C), no worker may outlive it.

    Before exiting it leaves what the run knew (finished_at, result.json with the verdict 'stopped'), so that the
    evaluator keeps the attempt's cost, calls and verdicts (added 2026-11-14). It must never fail to exit."""
    global STOPPING
    if SPAWN_LOCK.acquire(timeout=5):   # a thread that is starting a child finishes first; nothing starts after this
        STOPPING = True
        SPAWN_LOCK.release()
    else:
        STOPPING = True
    for process in list(LIVE):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in list(COMMANDS):
        kill_command(pid)
    try:
        run = CURRENT
        if run is not None and not os.path.exists(f'{run.out}/result.json'):
            if not os.path.exists(f'{run.out}/finished_at'):
                with open(f'{run.out}/finished_at', 'w') as handle:
                    handle.write(stamp() + '\n')
            status = dict(getattr(run, 'status', None) or {node['id']: 'waiting' for node in run.nodes})
            run.end_segment('stopped')
            write_result(run, 'stopped', status)
    except BaseException:
        pass
    try:
        if CURRENT is not None:
            resume.release_lock(CURRENT.out)
    except BaseException:
        pass
    try:
        if CURRENT is not None:
            CURRENT.leave_as_uncommitted()   # the evaluator saves and grades what is in the repository folder
    except BaseException:
        pass
    os._exit(130 if signum == signal.SIGINT else 124)


def write_result(run: 'Run', verdict: str, status: dict) -> None:
    result = {'run': run.name, 'lead': 'script', 'runner': RUNNER.name, 'verdict': verdict, 'review_verdicts': list(run.verdicts),
              'review_findings': list(run.findings), 'max_parallel': MAX_PARALLEL, 'max_fix_rounds': MAX_FIX_ROUNDS,
              'node_limit_seconds': NODE_LIMIT_SECONDS, 'max_turns': MAX_TURNS, 'nodes': status,
              'node_worktrees': NODE_WORKTREES, 'integration_check': INTEGRATION_CHECK,
              'own_copies': list(run.copies), 'acceptance': list(run.acceptance),
              'workers': [r if r.get('model') == 'script' or 'cost_usd' in r else dict(r, cost_usd=costs.cost_of(r, PRICES))
                          for r in run.records],
              'usage_totals': usage_totals(run.records), 'cost': costs.run_cost(run.records, PRICES, MAX_COST_USD),
              'segments': copy.deepcopy(run.segments),
              'resumes': len(run.segments) - 1}
    if run.stop_reason:
        result['reason'] = run.stop_reason
    reasons = {node_id: record['reason'] for node_id, record in list(run.progress.items())
               if record.get('reason') and status.get(node_id) == 'failed'}
    if reasons:
        result['node_reasons'] = reasons
    if run.plan_changes:
        result['plan_changes_accepted'] = list(run.plan_changes)
    if HUMAN:
        result['human'] = list(run.human_log)
    with open(f'{run.out}/result.json.tmp', 'w') as handle:
        json.dump(result, handle, indent=1)
    os.replace(f'{run.out}/result.json.tmp', f'{run.out}/result.json')   # never a half-written file


def usage_totals(records: list[dict]) -> dict:
    """Sums of what the workers' records state, nothing computed from a price list: the seconds of every worker, and the
    tokens and `cost_usd` of those whose runner reported usage (`workers_with_usage`; `cost_usd` is None when no runner
    reported a cost). A worker without usage adds only its seconds."""
    tokens = {key: 0 for key in ('input', 'output', 'cache_read', 'cache_write')}
    cost, with_usage, with_cost = 0.0, 0, 0
    for record in records:
        usage = record.get('usage') or {}
        if usage:
            with_usage += 1
        for key in tokens:
            tokens[key] += int(usage.get(key) or 0)
        if isinstance(usage.get('cost_usd'), (int, float)):
            cost += usage['cost_usd']
            with_cost += 1
    return {'workers': len(records), 'workers_with_usage': with_usage, 'workers_with_cost': with_cost,
            'seconds': sum(int(record.get('seconds') or 0) for record in records), 'tokens': tokens,
            'cost_usd': round(cost, 6) if with_cost else None}


def escalation_note(seconds: float) -> str:
    return f'no coordinator answered in {seconds / 60:g} minutes'


def _indent(text: str) -> str:
    return '\n'.join('      ' + line for line in text.rstrip().splitlines())


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Run:
    def __init__(self, plan_source: str, repo: str, name: str, resume_options: dict | None = None) -> None:
        """`resume_options` (None for a new run) continues the run <name> in its folder: {'reset_rounds': bool,
        'accept_plan_changes': bool}; see pangoloop/resume.py. A refusal is a SystemExit with a message."""
        self.repo, self.name = os.path.realpath(repo), name
        self.out = f'{RUNS}/{name}'
        branch = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=self.repo, capture_output=True,
                                text=True).stdout.strip()
        self.branch = branch
        self.plan_path = f'{self.out}/plan.md'
        self.plan_source = open(plan_source).read()
        self.person_commands = person_commands(self.plan_source)   # the optional section `## The person's commands`
        self.copies: list[dict] = []   # one fact line per build node that worked in its own copy
        self.acceptance: list[dict] = []   # one entry per round of a scripted acceptance node
        self.base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, capture_output=True,
                                   text=True).stdout.strip()
        plan = self.plan_text(self.repo, branch, own_copy=False)
        self.roles = role_files.load(paths.roles_folder())
        self.nodes = parse_nodes(plan, roles=self.roles)   # a plan fault stops here, before the run's folder exists
        for node in self.nodes:
            if node.get('other_word'):
                print(f"warning: node {node['id']} says role {node['role']!r}, which is not a kind and has no file "
                      f"{node['role']}.md in {self.roles.folder}: it runs as a plain session, as plans written before kinds did. "
                      'Give it a `kind:` and a role file to have a checked role.', flush=True)
        unset = placeholder_routes(self.nodes, ROUTES, DEFAULT_ROUTE)   # a placeholder route stops here too
        if unset:
            raise SystemExit('pangoloop: ' + ('the route ' if len(unset) == 1 else 'the routes ') + ', '.join(f'"{key}"' for key in unset)
                             + (' is' if len(unset) == 1 else ' are') + ' still a placeholder, and this plan has nodes that need '
                             + ('it' if len(unset) == 1 else 'them') + '. Write ' + ' and '.join(f'{key} = "model@provider"' for key in unset)
                             + ' under [routes] in the configuration file (see docs/SETUP.md and examples/presets/), then run again; '
                             'nothing was started.')
        # the state file's content, live (pangoloop/resume.py); `progress` is per node and may gain fields
        self.state_lock = threading.RLock()
        self.segments: list[dict] = []
        self.plan_changes: list[dict] = []
        self.progress: dict[str, dict] = {node['id']: resume.fresh_node() for node in self.nodes}
        self.tip: str | None = None
        self.carried: set[str] = set()
        self.records: list[dict] = []
        self.stop_reason: str | None = None   # why the run ended not verified, when the engine itself stopped it (`cost limit`)
        self.children: dict[str, dict] = {}   # what the engine started and is waiting for (pangoloop/children.py)
        self.verdicts: list[str] = []
        # findings in each review, in order; None for an unreadable review, so it is as long as the verdicts
        self.findings: list[int | None] = []
        self.sequence = 0
        self.resume_notes: list[str] = []
        seed = None
        if resume_options is None:
            os.makedirs(f'{self.out}/workers')   # a run name already used stops here (FileExistsError)
            resume.acquire_lock(self.out)
            with open(self.plan_path, 'w') as handle:
                handle.write(plan)
            with open(f'{self.out}/{resume.SOURCE_FILE}', 'w') as handle:
                handle.write(self.plan_source)
            with open(f'{self.out}/runner', 'w') as handle:   # the live page reads this before result.json exists
                handle.write(RUNNER.name + '\n')
        else:
            seed, self.carried, self.resume_notes = resume.begin(self, resume_options)   # refuses before it changes anything
            self.base = seed['base']
            with open(self.plan_path, 'w') as handle:
                handle.write(plan)
            with open(f'{self.out}/{resume.SOURCE_FILE}', 'w') as handle:
                handle.write(self.plan_source)
            with open(f'{self.out}/runner', 'w') as handle:
                handle.write(RUNNER.name + '\n')
            self.tip = seed['tip']
            self.progress = seed['nodes']
            self.segments = seed['segments']
            self.plan_changes = seed['plan_changes']
            self.records = seed['records']
            self.verdicts, self.findings = seed['verdicts'], seed['findings']
            self.sequence = seed['sequence']
            self.acceptance, self.copies = seed['acceptance'], seed['copies']
        self.tip = self.tip or self.base
        # Session names carry the run's start time: a run that reuses the name of a stopped one must never
        # continue that run's sessions (that has happened).
        self.born = format(int(time.time()), 'x')
        self.lock = threading.Lock()
        # SG_HUMAN state: requests made so far (for result.json), the number of the next request per node, the reason
        # the run was halted, the answers that accepted a `role: human` node, and the slot bookkeeping
        self.human_log: list[dict] = []
        self.human_asks: dict[str, int] = {}
        self.halt: str | None = None
        self.handoffs: dict[str, str] = {}
        self.slots = threading.BoundedSemaphore(MAX_PARALLEL)
        self.holder = threading.local()
        if seed is not None:
            self.human_log = seed['human_log']
            self.human_asks = dict(seed['human_asks'])
            human = f'{self.out}/human'
            for entry in sorted(os.listdir(human)) if os.path.isdir(human) else []:   # numbering of requests continues
                found = re.match(r'^(.+)-(\d+)\.json$', entry)
                if found:
                    self.human_asks[found.group(1)] = max(self.human_asks.get(found.group(1), 0), int(found.group(2)))
            self.segments.append({'n': len(self.segments) + 1, 'started_at': stamp(), 'ended_at': None, 'ended': None,
                                  'verdict': None, 'nodes_run': [], 'carried': sorted(self.carried),
                                  'first_worker': self.sequence + 1, 'last_worker': None, 'pid': os.getpid(),
                                  'notes': list(self.resume_notes)})
        else:
            self.segments.append({'n': 1, 'started_at': stamp(), 'ended_at': None, 'ended': None, 'verdict': None,
                                  'nodes_run': [], 'first_worker': 1, 'last_worker': None, 'pid': os.getpid()})
        for node_id, record in self.progress.items():
            record.setdefault('worktrees', [])
        self.save_state()

    # ---- the state file and the checkpoints (pangoloop/resume.py)

    def state_dict(self) -> dict:
        with self.state_lock:
            for _ in range(5):
                try:
                    return copy.deepcopy({
                        'version': resume.STATE_VERSION, 'run': self.name, 'repo': self.repo, 'branch': self.branch,
                        'base': self.base, 'runner': RUNNER.name, 'node_worktrees': NODE_WORKTREES,
                        'updated_at': stamp(), 'segment': len(self.segments), 'sequence': self.sequence,
                        'tip': self.tip, 'segments': self.segments, 'nodes': self.progress, 'records': self.records,
                        'verdicts': self.verdicts, 'findings': self.findings, 'acceptance': self.acceptance,
                        'copies': self.copies, 'human_log': self.human_log, 'human_asks': self.human_asks,
                        'plan_changes': self.plan_changes, 'children': self.children,
                        'cost': costs.run_cost(self.records, PRICES, MAX_COST_USD)})
                except RuntimeError:   # another thread changed a dict while it was copied
                    time.sleep(0.01)
            raise RuntimeError('state changing too fast to save')

    def save_state(self) -> None:
        """Rewrites <run>/state.json whole and atomically. Never fails the run: a write error is printed."""
        try:
            with self.state_lock:
                resume.write_state(self.out, self.state_dict())
        except Exception as error:
            print(f'state file not written: {error!r}', flush=True)

    def child_started(self, pid: int, kind: str, what: str) -> str:
        """Records a child just started (its pid and start time, what it is) in the state, until child_ended."""
        key = str(pid)
        with self.state_lock:
            self.children[key] = procs.entry(pid, kind, what)
        self.save_state()
        return key

    def child_ended(self, key: str) -> None:
        with self.state_lock:
            self.children.pop(key, None)
        self.save_state()

    def node_progress(self, node_id: str) -> dict:
        with self.state_lock:
            return self.progress.setdefault(self.base_node(node_id), resume.fresh_node())

    def set_node_status(self, node_id: str, status: str) -> None:
        with self.state_lock:
            self.node_progress(node_id)['status'] = status
        self.save_state()

    def checkpoint(self, node_id: str, label: str) -> str | None:
        """Records the work as it stands (see resume.checkpoint) for the node; the commit is kept as its `checkpoint`."""
        with self.state_lock:
            try:
                sha = resume.checkpoint(self.repo, self.out, self.name, self.tip or self.base, f'{label}: {self.name}')
            except Exception as error:
                print(f'{node_id}: no checkpoint of the work: {error!r}', flush=True)
                return None
            progress = self.node_progress(node_id)
            if sha != self.tip:
                progress['changed'] = True
            self.tip = sha
            progress['checkpoint'] = sha
        self.save_state()
        return sha

    def end_segment(self, verdict: str) -> None:
        with self.state_lock:
            last = self.segments[-1]
            last.update(ended_at=stamp(), ended=resume.segment_end(verdict), verdict=verdict, last_worker=self.sequence)
        self.save_state()

    def next_tag(self, node_id: str, name: str | None = None) -> str:
        """The next worker's tag `<nn>-<node>`; the counter and the node's worker list are saved at once, so that a
        resumed run's first new worker has the next number even when the engine was killed during this one."""
        with self.lock:
            self.sequence += 1
            tag = f'{self.sequence:02d}-{name or node_id}'
        with self.state_lock:
            self.node_progress(node_id)['workers'].append(tag)
        self.save_state()
        return tag

    def plan_text(self, repo: str, branch: str, own_copy: bool) -> str:
        """The plan for one folder. With node worktrees, the line about the shared folder is replaced."""
        plan = self.plan_source.replace('{{REPO}}', repo).replace('{{BRANCH}}', branch)
        if NODE_WORKTREES:
            line = OWN_COPY_LINES[0 if own_copy else 1]
            if re.search(SHARED_FOLDER_LINE, plan):
                plan = re.sub(SHARED_FOLDER_LINE, lambda _: line, plan, count=1)
            elif line not in plan:   # (a plan file that is already a run's plan.md has it)
                plan = plan.replace('\n## Nodes\n', f'\n{line}\n\n## Nodes\n', 1)
        return plan

    def prompt(self, node: dict, task_line: str | None = None, plan_path: str | None = None, repo: str | None = None,
               branch: str | None = None) -> str:
        lines = [f'Before anything else read {plan_path or self.plan_path} in full: the repository rules, the '
                 f'background and your node.', '', f"Your node is `{node['id']}`: {node['label']}. Do only this node.", '']
        role_text = role_files.section(node, repo or self.repo, branch or self.branch)   # the role's place: pangoloop/roles.py
        if role_text:
            lines += [role_text, '']
        for field in CONTRACT:
            lines.append(f"{field}: {task_line if (field == 'TASK' and task_line) else node[field]}")
        if NODE_SCRATCH:
            scratch = self.scratch_folder(node['id'])
            self.holder.scratch = scratch   # read by `worker` (same thread) to point TMPDIR at it
            lines.append(f"Your scratch folder is {scratch}, empty and yours alone; apart from "
                         f'what the plan allows, do not read or write any other folder outside the repository. '
                         f'Temporary files go there (TMPDIR points at it), never into /tmp.')
        if HUMAN:
            lines += ['', HUMAN_PARAGRAPH]
            if self.person_commands:
                lines += ['', PERSON_COMMANDS_INTRO, self.person_commands]
        return '\n'.join(lines) + '\n'

    def scratch_folder(self, node_id: str) -> str:
        """Creates a fresh empty scratch folder for the node (a suffix when an earlier try of it left one)."""
        with self.lock:
            base = f'{self.out}/scratch/{node_id}'
            path, number = base, 1
            while os.path.exists(path):
                number += 1
                path = f'{base}-{number}'
            os.makedirs(path)
            return path

    # ---- node worktrees (SG_NODE_WORKTREES=1) ----

    def git(self, cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(['git', '-c', 'user.name=sg', '-c', 'user.email=sg@localhost', '-c', 'core.editor=true',
                               *args], cwd=cwd, capture_output=True, text=True)
        if check and done.returncode:
            raise RuntimeError(f"git {' '.join(args)} in {cwd}: {(done.stderr or done.stdout).strip()[-400:]}")
        return done

    def commit_all(self, folder: str, message: str) -> bool:
        """Commits everything in the folder that git does not ignore. False when there was nothing to commit."""
        self.git(folder, 'add', '-A')
        if self.git(folder, 'diff', '--cached', '--quiet', check=False).returncode == 0:
            return False
        self.git(folder, 'commit', '-q', '-m', message)
        return True

    def leave_as_uncommitted(self) -> None:
        """Shows the accepted work as one uncommitted change against the commit the run started from."""
        if NODE_WORKTREES:
            self.git(self.repo, 'reset', '-q', '--mixed', self.base, check=False)

    def run_in_repository(self, node: dict, job) -> bool:
        """A node that is not a build node: it runs alone in the repository folder, on the accepted work shown as
        the uncommitted change; what it leaves is committed as accepted work again."""
        with INTEGRATION_LOCK:
            self.leave_as_uncommitted()
            try:
                return job(node)
            finally:
                self.commit_all(self.repo, f"{node['id']}: as left")
                self.checkpoint(node['id'], f"{node['id']} as left")

    def in_shared_folder(self, node: dict, job) -> bool:
        """A node that runs in the repository folder with no node worktrees: when its job ends, the work as it stands is
        recorded (a checkpoint; the folder and the branch are not touched)."""
        try:
            return job(node)
        finally:
            self.checkpoint(node['id'], f"{node['id']} as left")

    def rebase_stopped(self, copy: str) -> bool:
        folder = self.git(copy, 'rev-parse', '--git-path', 'rebase-merge').stdout.strip()
        return os.path.isdir(folder if os.path.isabs(folder) else os.path.join(copy, folder))

    def files_with_markers(self, copy: str) -> list[str]:
        """Files of the stopped rebase that still carry conflict markers, whether git calls them unmerged or not."""
        unmerged = [path for path in self.git(copy, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines() if path]
        staged = [path for path in self.git(copy, 'diff', '--cached', '--name-only').stdout.splitlines() if path]
        found = []
        for path in dict.fromkeys(unmerged + staged):
            try:
                text = open(os.path.join(copy, path), errors='replace').read()
            except OSError:
                continue
            if re.search(r'(?m)^<{7} ', text) and re.search(r'(?m)^>{7} ', text):
                found.append(path)
        return found

    def run_in_own_copy(self, node: dict) -> bool:
        copy = f"{self.out}/wt/{node['id']}"
        branch = f"sg/{node['id']}"
        fact = {'node': node['id'], 'base_moved': False, 'conflict_files': 0, 'check_failures': 0,
                'repair_sessions': 0, 'integrate_seconds': 0, 'integrated': False}
        self.copies.append(fact)
        with INTEGRATION_LOCK:
            started_from = self.git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            self.set_aside_old_branch(branch)
            self.git(self.repo, 'worktree', 'add', '-q', '-b', branch, copy, 'HEAD')
        with self.state_lock:
            self.node_progress(node['id'])['worktrees'].append({'path': copy, 'branch': branch})
        self.save_state()
        # Outside the lock (2026-11-16): copying the packages touches nothing of git, and under the lock the nodes
        # of one layer would start one after another instead of together.
        began = time.time()
        if os.path.isdir(f'{self.repo}/node_modules'):
            copy_packages(f'{self.repo}/node_modules', f'{copy}/node_modules')
        fact['setup_seconds'] = round(time.time() - began, 1)
        os.makedirs(f'{self.out}/plans', exist_ok=True)
        plan_path = f"{self.out}/plans/{node['id']}.md"
        plan = self.plan_text(copy, branch, own_copy=True)
        with open(plan_path, 'w') as handle:
            handle.write(plan)
        own = next(found for found in parse_nodes(plan, roles=self.roles) if found['id'] == node['id'])
        if kind_of(node) == 'script':
            if not self.run_script(own, cwd=copy):
                return False   # its copy stays on disk for a look; nothing of it is accepted
        else:
            for _ in range(1 + MAX_REPAIRS):
                if self.ask(node['id'], node['category'], self.prompt(own, plan_path=plan_path, repo=copy, branch=branch),
                            cwd=copy)['ok']:
                    break
            else:
                return False   # its copy stays on disk for a look; nothing of it is accepted
            if must_change(own) and self.empty_change(copy, started_from):
                self.fail_node(node['id'], NO_CHANGE)   # the worker said it was done, and the copy is as it began
                return False
        self.set_node_status(node['id'], 'integrating')
        with INTEGRATION_LOCK:
            began = time.time()
            try:
                fact['integrated'] = self.integrate(own, copy, branch, plan_path, started_from, fact)
            finally:
                fact['integrate_seconds'] = round(time.time() - began)
            if fact['integrated']:
                self.node_progress(node['id'])['branch_commit'] = self.git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
                self.checkpoint(node['id'], f"{node['id']} integrated")
        return fact['integrated']

    def set_aside_old_branch(self, branch: str) -> None:
        """A node's branch is `sg/<node id>`, and a project runs plans with the same node ids again and again: a branch of that
        name left by an earlier run (a run ends with the accepted work shown as an uncommitted change, so its branches are
        not in the history) is kept under `<name>-earlier-<n>`, as a resume sets a node's old branch aside, never deleted and
        never reused. `git branch -D 'sg/*'` clears them when you do not want them."""
        if self.git(self.repo, 'rev-parse', '--verify', '-q', branch, check=False).returncode:
            return
        number = 1
        while self.git(self.repo, 'rev-parse', '--verify', '-q', f'{branch}-earlier-{number}', check=False).returncode == 0:
            number += 1
        self.git(self.repo, 'branch', '-m', branch, f'{branch}-earlier-{number}')

    def empty_change(self, copy: str, started_from: str) -> bool:
        """True when the node's copy holds no change against the commit it was made from (everything git does not
        ignore, untracked files included; a worker that committed anyway is looked at too)."""
        self.git(copy, 'add', '-A')
        return self.git(copy, 'diff', '--cached', '--quiet', started_from, check=False).returncode == 0

    def fail_node(self, node_id: str, reason: str) -> None:
        """Records why a node failed: in the state (the page and a resume read it) and, at the end, in result.json."""
        with self.state_lock:
            self.node_progress(node_id)['reason'] = reason
        print(f'{node_id}: failed: {reason}', flush=True)
        self.save_state()

    def integrate(self, node: dict, copy: str, branch: str, plan_path: str, started_from: str, fact: dict) -> bool:
        """Commit, rebase onto the accepted work, check, move the accepted work forward. Under INTEGRATION_LOCK."""
        self.commit_all(copy, node['id'])
        target = self.git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
        fact['base_moved'] = target != started_from
        self.git(copy, 'rebase', target, check=False)
        while True:
            if self.rebase_stopped(copy):
                files = self.files_with_markers(copy)
                if not files:
                    # every conflict is resolved in the files: the script, not the worker, continues the rebase
                    self.git(copy, 'add', '-A')
                    self.git(copy, 'rebase', '--continue', check=False)
                    if self.rebase_stopped(copy) and not self.files_with_markers(copy):
                        self.git(copy, 'rebase', '--abort', check=False)
                        print(f"{node['id']}: the rebase could not be continued", flush=True)
                        return False
                    continue
                fact['conflict_files'] = max(fact['conflict_files'], len(files))
                task = ('Your node is finished. While you worked, other nodes were accepted, and the coordinator is '
                        'rebasing your work onto theirs. The rebase stopped on conflicts in: '
                        + ', '.join(f'`{path}`' for path in files) + '. These files now contain conflict markers '
                        '(<<<<<<<, =======, >>>>>>>): above the ======= line is the accepted work of the other nodes, '
                        'below it is yours. Resolve every conflict so that both hold: the accepted work keeps working, '
                        'and the rules of your node are still met. You may edit these files inside the conflicting '
                        'regions even where they are outside your write scope. Leave no marker behind. Do not run any '
                        'git command that changes state (no add, commit, rebase, checkout, reset, stash): the '
                        'coordinator continues the rebase after you. Then run the VERIFY of your node again.')
                deliverable = 'the files without conflict markers; in the handoff, each conflict and how you resolved it.'
            else:
                if self.git(copy, 'merge-base', '--is-ancestor', target, 'HEAD', check=False).returncode != 0:
                    print(f"{node['id']}: its work is not on top of the accepted work after the rebase", flush=True)
                    return False
                self.commit_all(copy, f"{node['id']}: after the rebase")
                failure = self.integration_check(copy, node['id']) if fact['base_moved'] else None
                if failure is None:
                    break
                fact['check_failures'] += 1
                task = ('Your node is finished, and the coordinator has rebased your work onto the work other nodes '
                        'had accepted meanwhile. There was no conflict in the text, but together the two do not pass '
                        f'the check `{INTEGRATION_CHECK}`. The end of its output:\n\n{failure}\n\nFind what in your '
                        'work and the accepted work does not fit together and repair it in your files; the accepted '
                        'work of the other nodes is not yours to change. Do not run any git command that changes '
                        'state. Then run that check and the VERIFY of your node again.')
                deliverable = 'the repair; in the handoff, what did not fit together and what you changed.'
            if fact['repair_sessions'] > MAX_REPAIRS:
                self.git(copy, 'rebase', '--abort', check=False)
                print(f"{node['id']}: not integrated after {fact['repair_sessions']} repair sessions", flush=True)
                return False
            fact['repair_sessions'] += 1
            repair = dict(node, id=f"{node['id']}-rebase{fact['repair_sessions']}", label='Fit your work onto the accepted work')
            repair.update({'TASK': task, 'DELIVERABLE': deliverable,
                           'SCOPE': 'only what the fit needs: the conflicting regions, and your own files.',
                           'STOP WHEN': 'the check and the VERIFY commands pass, or the two cannot be fitted without '
                                        'breaking a rule (then report which and why).'})
            self.ask(repair['id'], node['category'], self.prompt(repair, plan_path=plan_path, repo=copy, branch=branch), cwd=copy)
        self.git(self.repo, 'merge', '-q', '--ff-only', branch)
        self.git(self.repo, 'worktree', 'remove', '--force', copy, check=False)
        return True

    def integration_check(self, copy: str, node_id: str | None = None) -> str | None:
        """Runs SG_INTEGRATION_CHECK in the node's copy. None when it passes or none is set, else the output's end."""
        if not INTEGRATION_CHECK:
            return None
        process = start_process(INTEGRATION_CHECK, shell=True, cwd=copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        key = self.child_started(process.pid, 'integration check', INTEGRATION_CHECK)
        began = time.time()
        try:
            try:
                stdout, stderr = process.communicate(timeout=INTEGRATION_CHECK_SECONDS)
            except subprocess.TimeoutExpired:
                kill_command(process.pid)
                stdout, stderr = process.communicate()
                self.note_spend((stdout or '') + '\n' + (stderr or ''), node_id, 'integration check', INTEGRATION_CHECK, 124,
                                round(time.time() - began))
                return f'(no result after {INTEGRATION_CHECK_SECONDS} seconds; the command was stopped)'
        finally:
            COMMANDS.discard(process.pid)
            self.child_ended(key)
        self.note_spend((stdout or '') + '\n' + (stderr or ''), node_id, 'integration check', INTEGRATION_CHECK,
                        process.returncode, round(time.time() - began))
        if process.returncode == 0:
            return None
        return ((stdout or '') + (stderr or ''))[-3000:]

    # ---- a human in the run (SG_HUMAN=1) ----

    def base_node(self, node_id: str) -> str:
        """The plan node a worker's name belongs to: `a-rebase1` and `review-fix2` belong to `a` and `review`."""
        known = {node['id'] for node in self.nodes}
        if node_id not in known:
            sub = re.match(r'^(.*)-(?:fix|rebase)\d+$', node_id)
            if sub and sub.group(1) in known:
                return sub.group(1)
        return node_id

    def check_cost_limit(self, node_id: str) -> None:
        """Before each worker starts: when the run's known cost (all segments: the records are carried over by a resume) is
        over max_cost_usd, no new worker starts. The run then ends not verified with the reason `cost limit`; workers that
        are running are not stopped, and nodes that are running fail as they ask for their next worker."""
        if MAX_COST_USD is None:
            return
        known = costs.run_cost(list(self.records), PRICES)['usd'] or 0.0
        if known > MAX_COST_USD:
            with self.state_lock:
                self.stop_reason = COST_LIMIT
            raise CostLimit(f'cost limit: the known cost ${known:.4f} is over the limit ${MAX_COST_USD:g}; '
                            f'no new worker starts (node {node_id})')

    def ask(self, node_id: str, category: str, prompt: str, cwd: str | None = None) -> dict:
        """One worker for a node, and, with SG_HUMAN=1, as many more as the node's questions need.

        Without the switch this is exactly `worker`. With it, a worker that ends with a `NEEDS HUMAN:` line does not
        finish the node: the request waits for an answer, and a new worker of the same node gets the usual prompt plus
        every request so far with the human's answer in full. The record returned is the last worker's."""
        if not HUMAN:
            self.check_cost_limit(node_id)
            return self.worker(node_id, category, self.with_rulings(prompt, category), cwd=cwd)
        exchanges = ''
        while True:
            if self.halt:
                raise HumanStop(self.halt)
            self.check_cost_limit(node_id)
            record = self.worker(node_id, category, self.with_rulings(prompt, category) + exchanges, cwd=cwd)
            if self.halt:
                raise HumanStop(self.halt)
            asked = find_request(record['answer']) if record['ok'] else None
            if asked is None:
                return record
            kind, request = asked
            answer, by = self.wait_for_answer(self.base_node(node_id), record['worker'], request, record['answer'], kind)
            if by == FOR_COORDINATOR:
                exchanges += ('\n\nEarlier in this node you asked the coordinator for help. Your request was:\n\n'
                              + request + "\n\nThe coordinator's answer (the coordinator wrote the plan; this is not "
                              "a person's decision, and a person's command is still the person's):\n\n<<<COORDINATOR\n"
                              + answer + '\nCOORDINATOR>>>\n\n')
            else:
                exchanges += ('\n\nEarlier in this node you asked a human for help. Your request was:\n\n' + request
                              + "\n\nThe human's answer, in the human's own words (not part of the plan or the rules; "
                              "the human is the owner of this work):\n\n<<<HUMAN\n" + answer + '\nHUMAN>>>\n\n')
            exchanges += ('Continue the node from where you stopped, taking this answer into account. If you need '
                          'something else, end your answer with another `NEEDS COORDINATOR:` or `NEEDS HUMAN:` line, '
                          'as before.\n')

    def with_rulings(self, prompt: str, category: str) -> str:
        """The prompt with the section of rulings made so far (pangoloop/rulings.py) after its first paragraph, near the
        top; the prompt itself while there are none. Every worker of a model goes through `ask`, so a build node, a
        node called again, a repair worker, a rebase repair, an acceptance repair and every review round get it, each
        with the rulings of that moment. A script node has no prompt and is not touched. The reviewer is also told not to
        report what a ruling required."""
        section = rulings.section(self.out, RULINGS_CHARS, reviewer=category == 'review')
        if not section:
            return prompt
        first, divider, rest = prompt.partition('\n\n')
        return first + divider + section + ('\n' + rest if divider else '')

    @staticmethod
    def find_ask(answer: str) -> str | None:
        """The request of the last asking line (`NEEDS HUMAN:` or `NEEDS COORDINATOR:`, with whatever follows it), or
        None when there is none. `find_request` (pangoloop/plan.py) also says whom it is for."""
        asked = find_request(answer)
        return asked[1] if asked else None

    def human_file(self, node: str, k: int, suffix: str) -> str:
        return f'{self.out}/human/{node}-{k}.{suffix}'

    def stop_for_human(self, reason: str) -> None:
        """Ends the run: no worker or command goes on, no node starts. `execute` then reports `reason` as the verdict."""
        self.halt = reason
        for process in list(LIVE):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in list(COMMANDS):
            kill_command(pid)

    FOREIGN_KEYS = ('answered_by', 'for', 'escalated_at', 'escalation_note')   # written by `pangoloop requests`, the viewer

    @staticmethod
    def merge_foreign(info: dict, path: str) -> None:
        """Takes into `info` what others wrote into the request file since the engine last looked: who answered, and an
        escalation (`pangoloop requests escalate`): a request for the coordinator may become one for the person, never
        the other way."""
        try:
            with open(path) as handle:
                disk = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(disk, dict):
            return
        if 'answered_by' in disk:
            info['answered_by'] = disk['answered_by']
        if disk.get('for') == FOR_PERSON and info.get('for') != FOR_PERSON:
            for key in ('for', 'escalated_at', 'escalation_note'):
                if key in disk:
                    info[key] = disk[key]

    def wait_for_human(self, node: str, worker: str | None, request: str, answer_tail: str) -> str:
        """A request for the person; returns the answer's text (see wait_for_answer)."""
        return self.wait_for_answer(node, worker, request, answer_tail, FOR_PERSON)[0]

    def wait_for_answer(self, node: str, worker: str | None, request: str, answer_tail: str,
                        for_: str = FOR_PERSON) -> tuple[str, str]:
        """Opens a request (human/<node>-<k>.json), waits for human/<node>-<k>.answer.txt and returns (its text, who
        answered: 'person' or 'coordinator').

        `for_` says whom the request is for. Only a request for the person calls the person (SG_HUMAN_OPEN, and the
        viewer's desktop notification). A request for the coordinator becomes one for the person when somebody runs
        `pangoloop requests escalate` (the engine notices on its next look at the file, and then calls the person as if
        the node had asked the person), or by itself after PANGOLOOP_COORDINATOR_WAIT_SECONDS.

        The node shows as `waiting for human` or `waiting for coordinator`; its slot among the MAX_PARALLEL goes to
        another node meanwhile. The wait counts neither against a worker's time limit nor against the fix rounds. An
        answer that starts with the line ABORT, or no answer within SG_HUMAN_WAIT_SECONDS (counted from the first
        asking), ends the run (HumanStop)."""
        with self.lock:
            k = self.human_asks.get(node, 0) + 1
            self.human_asks[node] = k
        os.makedirs(f'{self.out}/human', exist_ok=True)
        asked_at = stamp()
        path = self.human_file(node, k, 'json')
        info = {'node': node, 'worker': worker, 'worker_kind': worker_kind(worker, self.nodes), 'asked_at': asked_at,
                'request': request, 'for': for_,
                'answer_tail': (answer_tail or '')[-2000:], 'state': 'open'}
        log = {'node': node, 'k': k, 'asked_at': asked_at, 'state': 'open', 'for': for_}
        self.human_log.append(log)
        self.save_state()

        def save() -> None:
            self.merge_foreign(info, path)   # keep what others wrote meanwhile
            with open(path + '.tmp', 'w') as handle:
                json.dump(info, handle, indent=1)
            os.replace(path + '.tmp', path)   # the viewer never reads half a file
        save()

        def waiting_status() -> str:
            return 'waiting for human' if info['for'] == FOR_PERSON else 'waiting for coordinator'

        def call_person() -> None:
            print(f'{node}: waits for a human (request {k})', flush=True)
            if HUMAN_OPEN:
                self.open_page(node, request)
        if for_ == FOR_PERSON:
            call_person()
        else:
            print(f'{node}: waits for the coordinator (request {k})', flush=True)
        status = getattr(self, 'status', None)
        if status is not None:
            status[node] = waiting_status()
        held = getattr(self.holder, 'held', False)
        if held:
            self.holder.held = False
            self.slots.release()
        began = time.time()
        try:
            while True:
                try:
                    text = open(self.human_file(node, k, 'answer.txt'), errors='replace').read()
                except OSError:
                    text = ''
                before = info['for']
                self.merge_foreign(info, path)
                if info['for'] != before:   # escalated by the command
                    log.update({'for': info['for'], 'escalated_at': info.get('escalated_at')})
                    if status is not None and status.get(node) == 'waiting for coordinator':
                        status[node] = waiting_status()
                    call_person()
                if text.strip():
                    break
                if self.halt:
                    raise HumanStop(self.halt)
                if info['for'] == FOR_COORDINATOR and time.time() - began > COORDINATOR_WAIT_SECONDS:
                    info.update({'for': FOR_PERSON, 'escalated_at': stamp(),
                                 'escalation_note': escalation_note(COORDINATOR_WAIT_SECONDS)})
                    log.update({'for': FOR_PERSON, 'escalated_at': info['escalated_at']})
                    save()
                    if status is not None and status.get(node) == 'waiting for coordinator':
                        status[node] = waiting_status()
                    call_person()
                if time.time() - began > HUMAN_WAIT_SECONDS:
                    info['state'] = log['state'] = 'no answer'
                    save()
                    self.stop_for_human('no_answer_from_human')
                    raise HumanStop(self.halt)
                time.sleep(HUMAN_POLL_SECONDS)
            self.merge_foreign(info, path)
            answered_by = info.get('answered_by') or info['for']   # an answer nobody signed counts for whom it was asked
            info.update(answered_at=stamp(), state='answered', answered_by=answered_by)
            log.update(answered_at=info['answered_at'], state='answered', answered_by=answered_by)
            save()
            self.save_state()
            if text.split('\n', 1)[0].rstrip('\r') == 'ABORT':
                self.stop_for_human('stopped_by_human')
                raise HumanStop(self.halt)
            try:   # every answered request is a ruling, kept for every later prompt
                rulings.append(self.out, {'id': f'{node}-{k}', 'node': node, 'worker': worker, 'by': answered_by,
                                          'at': info['answered_at'], 'question': request, 'answer': text.rstrip('\n')})
            except OSError as error:
                print(f'{node}: cannot record the ruling of request {k}: {error}', flush=True)
            return text.rstrip('\n'), answered_by
        finally:
            if held:
                self.slots.acquire()
                self.holder.held = True
            if status is not None and status.get(node) in ('waiting for human', 'waiting for coordinator'):
                status[node] = 'running'

    def open_page(self, node: str, request: str) -> None:
        """SG_HUMAN_OPEN: shows the person the run's page. Silent when nothing listens on the viewer's port; every
        failure is ignored. Arguments only, never a shell string made from the request's text."""
        try:
            with socket.create_connection(('127.0.0.1', VIEW_PORT), timeout=1):
                pass
        except OSError:
            return
        url = f'http://127.0.0.1:{VIEW_PORT}/?run={urllib.parse.quote(self.name)}'
        try:
            tool = shutil.which('terminal-notifier')
            if tool:   # a click on its notification opens the page (osascript notifications cannot do that)
                subprocess.run([tool, '-title', f'{self.name}: {node} needs you', '-message',
                                ' '.join(request.split())[:120] + f' Answer at {url}', '-open', url],
                               timeout=10, capture_output=True, stdin=subprocess.DEVNULL)
            else:
                subprocess.run(['open', url], timeout=10, capture_output=True, stdin=subprocess.DEVNULL)
        except Exception:
            pass

    def run_human(self, node: dict) -> bool:
        """A node with the role `human` has no worker: its TASK is the request, and any answer but ABORT accepts it."""
        answer = self.wait_for_human(node['id'], None, node['TASK'], '')
        self.handoffs[node['id']] = answer
        return True

    def guarded(self, job, *args):
        """Runs a node's job holding one of the MAX_PARALLEL slots (SG_HUMAN only: a node that waits for a human gives
        its slot back, which the plain pool could not do)."""
        self.slots.acquire()
        self.holder.held = True
        try:
            return job(*args)
        finally:
            if getattr(self.holder, 'held', False):
                self.slots.release()
            self.holder.held = False

    def worker(self, node_id: str, category: str, prompt: str, cwd: str | None = None) -> dict:
        """One one-shot Hermes session. Returns its record, with the final answer."""
        tag = self.next_tag(node_id)
        folder = f'{self.out}/workers/{tag}'
        os.makedirs(folder)
        with open(f'{folder}/prompt.txt', 'w') as handle:
            handle.write(prompt)
        model, provider = ROUTES.get(category, DEFAULT_ROUTE)
        command = RUNNER.command(session=f'sg-{self.name}-{self.born}-{tag}', model=model, provider=provider,
                                 prompt_file=f'{folder}/prompt.txt',
                                 toolsets=CATEGORY_TOOLSETS.get(category, TOOLSETS), max_turns=MAX_TURNS,
                                 extra_dirs=[self.out])   # the run folder holds the plan, the scratch folders and the copies
        self.holder.env = RUNNER.env(f'sg-{self.name}-{self.born}-{tag}', model, provider)
        scratch = getattr(self.holder, 'scratch', None) if NODE_SCRATCH else None
        if scratch:   # temporary files of the worker and of its tools go into the node's scratch folder, not /tmp
            self.holder.env = dict(self.holder.env or {}, TMPDIR=scratch)
        started = time.time()
        waits = 0
        while True:
            result = self.launch(command, folder, cwd)
            refused = RUNNER.retry_after_refusal(self.stderr_text(folder), result.exit_code, result.text)
            if not refused or waits >= RUNNER.refusal_max_waits:
                break
            waits += 1
            time.sleep(RUNNER.refusal_wait_seconds)
        code, answer, ran_on = result.exit_code, result.text, result.reported_model
        with open(f'{folder}/answer.md', 'w') as handle:
            handle.write(answer)
        record = {'worker': tag, 'node': node_id, 'category': category, 'model': model, 'reported_model': ran_on,
                  'started': round(started), 'seconds': round(time.time() - started), 'exit': code,
                  'limit_waits': waits, 'runner': RUNNER.name, 'provider': provider,
                  'worker_kind': worker_kind(tag, self.nodes),
                  'ok': code == 0 and bool(answer.strip()) and not refused, 'answer': answer}
        if record['ok'] and ignored_request(answer) is not None:
            # a closing `NEEDS ...: none` line is not a request (the node was not blocked): seen, ignored, recorded
            record['ignored_ask'] = ignored_request(answer)[:200]
        if result.usage:
            record['usage'] = result.usage
        record['cost_usd'], source = costs.worker_cost(record, PRICES)   # None when the model has no price (and no cost was reported)
        if source:
            record['cost_source'] = source
        if result.notes:
            record['notes'] = list(result.notes)
        self.records.append({key: value for key, value in record.items() if key != 'answer'})
        self.save_state()
        print(f"{tag:<28}{model:<16}exit={code:<4}{record['seconds']:>6}s" + (f'  waited {waits}x for a slot' if waits else ''),
              flush=True)
        return record

    @staticmethod
    def stderr_text(folder: str) -> str:
        try:
            with open(f'{folder}/stderr.log', errors='replace') as handle:
                return handle.read()[-4000:]
        except OSError:
            return ''

    def launch(self, command: list[str], folder: str, cwd: str | None = None):
        """Starts the session once and waits for it. Returns the runner's RunResult (exit code, final answer, reported
        model, usage and notes)."""
        with open(f'{folder}/events.jsonl', 'w') as events, open(f'{folder}/stderr.log', 'w') as errors:
            with START_LOCK:
                process = self.spawn(command, events, errors, cwd)
                key = self.child_started(process.pid, 'worker', os.path.basename(folder))
                time.sleep(START_GAP_SECONDS)
            try:
                code = process.wait(timeout=NODE_LIMIT_SECONDS)
            except subprocess.TimeoutExpired:
                for sign in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sign)
                    except ProcessLookupError:
                        break
                    time.sleep(5)
                code = 124
            finally:
                LIVE.discard(process)
                self.child_ended(key)
        return RUNNER.read_events(f'{folder}/events.jsonl', code)

    def spawn(self, command: list[str], events, errors, cwd: str | None = None) -> subprocess.Popen:
        added = getattr(self.holder, 'env', None)   # the runner's environment additions for this worker's thread
        return start_process(command, live=True, cwd=cwd or self.repo, stdout=events, stderr=errors,
                             start_new_session=True, env=dict(os.environ, **added) if added else None,
                             stdin=subprocess.DEVNULL if RUNNER.stdin_closed else None)

    def run_plain(self, node: dict) -> bool:
        for _ in range(1 + MAX_REPAIRS):
            if self.ask(node['id'], node['category'], self.prompt(node))['ok']:
                return True
        return False

    def note_spend(self, output: str, node_id: str | None, kind: str, command: str, code: int, seconds: int) -> None:
        """Money a command reported itself (`PANGOLOOP-SPEND: <usd> [label]`, scanned in the whole output, also of a command
        that timed out): kept as a record with the model `script`, `cost_usd` and `cost_source` `reported`, so that the
        run's cost and the check before each worker count it. A bad line is ignored and kept as a warning in the record."""
        spend = costs.parse_spend(output)
        if spend['usd'] is None and not spend['warnings']:
            return
        for warning in spend['warnings'][:costs.MAX_STORED]:
            print(f"{node_id or '-'}: {kind} `{command[:44]}`: PANGOLOOP-SPEND {warning}", flush=True)
        record = {'worker': f"spend-{node_id or 'run'}-{len(self.records) + 1}", 'node': node_id, 'category': 'script',
                  'model': 'script', 'reported_model': None, 'started': round(time.time()) - seconds, 'seconds': seconds,
                  'exit': code, 'limit_waits': 0, 'ok': code == 0, 'command_kind': kind, 'command': command[:200]}
        if spend['usd'] is not None:
            record.update(cost_usd=spend['usd'], cost_source='reported', spend_lines=spend['lines'][:costs.MAX_STORED])
            if len(spend['lines']) > costs.MAX_STORED:
                record['lines_dropped'] = len(spend['lines']) - costs.MAX_STORED
        if spend['warnings']:
            record['warnings'] = spend['warnings'][:costs.MAX_STORED]
            if len(spend['warnings']) > costs.MAX_STORED:
                record['warnings_dropped'] = len(spend['warnings']) - costs.MAX_STORED
        self.records.append(record)
        self.save_state()

    def shell(self, command: str, cwd: str, kind: str = 'command', node_id: str | None = None) -> tuple[int, str, int]:
        """One shell command in `cwd`, with the limit SG_ACCEPTANCE_SECONDS and its output (stdout and stderr together)
        captured. Returns (exit code, output, seconds). A command past the limit is stopped with everything under it and
        exits 124. Used by the acceptance node and by the `run` step of a script node."""
        began = time.time()
        process = start_process(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)   # a group of its own: the leader of it is the shell;
        key = self.child_started(process.pid, kind, command)
        try:
            output, _ = process.communicate(timeout=ACCEPTANCE_SECONDS)
            code = process.returncode
        except subprocess.TimeoutExpired:
            kill_command(process.pid)   # the whole tree and group: a test runner has children
            output, _ = process.communicate()
            code = 124
            output = (output or '') + f'\n(no result after {ACCEPTANCE_SECONDS} seconds; the command was stopped)'
        finally:
            COMMANDS.discard(process.pid)
            self.child_ended(key)
        seconds = round(time.time() - began)
        self.note_spend(output or '', node_id, kind, command, code, seconds)   # the whole output, not the stored tail
        return code, output or '', seconds

    def run_script(self, node: dict, cwd: str | None = None) -> bool:
        """A node with `role: script`: no model. The steps of its STEPS field are performed in order in `cwd` (the node's
        own copy, or the repository folder when the node runs there); the first step that is refused or fails fails the
        node, and the later steps do not run. It is recorded like a worker (folder workers/<nn>-<node>, a row in
        result.json with the model `script`), so that the page shows it, and its handoff lists every step with what it
        did (kept as workers/<nn>-<node>/answer.md and in `handoffs`)."""
        cwd = cwd or self.repo
        tag = self.next_tag(node['id'])
        folder = f'{self.out}/workers/{tag}'
        os.makedirs(folder)
        with open(f'{folder}/prompt.txt', 'w') as handle:
            handle.write(f"STEPS: {node['STEPS']}\n")
            if node.get('steps_file'):
                handle.write(f"steps_file: {node['steps_file']}\n")
                if node.get('steps_file_format'):
                    handle.write(f"steps_file_format: {node['steps_file_format']}\n")
        started = time.time()
        events = open(f'{folder}/events.jsonl', 'w')

        def event(**fields: object) -> None:
            # the viewer finds an event's type in the first bytes of its line: `type` goes first
            kind = fields.pop('type')
            events.write(json.dumps({'type': kind, 'timestamp': int(time.time() * 1000), **fields}) + '\n')
            events.flush()
        event(type='system', subtype='init', model='script')
        lines: list[str] = []
        failure: str | None = None
        completed = 0
        step_list = list(node['steps'])   # the field's steps first, then the file's
        try:
            if node.get('steps_file'):
                step_list += steps.load_steps_file(cwd, node['steps_file'], node.get('steps_file_format', ''))
            # What is wrong without looking at the files is found first, so that a refused step changes nothing.
            for step in step_list:
                for raw in step.get('paths', []) + [step[key] for key in ('src', 'dst') if key in step]:
                    steps.normalize(raw)
        except steps.StepRefused as refusal:
            failure = f'refused before any step ran: {refusal}'
        if failure is None and any(step['word'] == 'move' for step in step_list):
            # A list with a move is also tried on a copy of the file tree in memory (steps.simulate): a plan with 300
            # moves starts clean or does not start. (Lists without a move keep the old checking: each delete is
            # checked at its own step.)
            wrong = [item for item in steps.simulate(cwd, step_list, stop_at_first=False) if item['status'] == 'refused']
            if wrong:
                failure = (f"refused before any step ran: {len(wrong)} step{'' if len(wrong) == 1 else 's'} would be refused:"
                           + ''.join(f"\n  step {item['number']} ({item['text']}): {item['note']}" for item in wrong[:10])
                           + (f'\n  ... and {len(wrong) - 10} more' if len(wrong) > 10 else ''))
        for number, step in enumerate(step_list if failure is None else [], 1):
            event(type='tool_use', name=step['word'], input={'command': step['text']})
            problem = None
            reported = False
            try:
                if step['word'] == 'run':
                    code, output, seconds = self.shell(step['command'], cwd, 'script step', node['id'])
                    print(f"{node['id'] + ' ' + step['command'][:44]:<60}exit={code:<4}{seconds:>6}s", flush=True)
                    summary = f'exit {code}, {seconds}s'
                    event(type='tool_result', name='run', output=output[-2000:], is_error=code != 0)
                    reported = True
                    if code != 0:
                        problem = f'exit {code}, {seconds}s; the end of its output:\n{_indent(output[-1500:] or "(no output)")}'
                elif step['word'] == 'move':
                    summary = steps.describe_move(steps.move_step(cwd, step['src'], step['dst']))
                    event(type='tool_result', name='move', output=summary, is_error=False)
                else:
                    done = steps.delete_step(cwd, step['paths'], optional=step['word'] == 'delete?')
                    parts = [f"{path} ({count} file{'' if count == 1 else 's'})" for path, count in done['deleted']]
                    parts += [f'{path}: nothing matched, skipped' for path in done['skipped']]
                    summary = ('deleted ' if done['deleted'] else 'deleted nothing; ') + ', '.join(parts)
                    event(type='tool_result', name=step['word'], output=summary, is_error=False)
            except steps.StepRefused as refusal:
                problem = f"REFUSED, nothing {'moved' if step['word'] == 'move' else 'deleted'} by this step: {refusal}"
            except Exception as error:   # a broken step fails its node, not the run's bookkeeping
                problem = f'ERROR: {error!r}'
            if problem is not None:
                failure = problem
                if not reported:
                    event(type='tool_result', name=step['word'], output=problem, is_error=True)
                lines.append(f"{number}. {step['text']}\n    -> FAILED: {problem}")
                break
            completed = number
            lines.append(f"{number}. {step['text']}\n    -> {summary}")
        total = len(step_list)
        head = f"Script node `{node['id']}`: {total} step{'' if total == 1 else 's'}, "
        if failure is None:
            head += 'all done.'
        elif lines:
            head += f'{completed} done, step {completed + 1} failed' + (f', {total - completed - 1} not run.' if total - completed - 1 else '.')
        else:
            head += f'none run: {failure}'
        handoff = head + ('\n' + '\n'.join(lines) if lines else '') + '\n'
        ok = failure is None
        event(type='result', text=handoff, exit_code=0 if ok else 1)
        events.close()
        with open(f'{folder}/answer.md', 'w') as handle:
            handle.write(handoff)
        self.handoffs[node['id']] = handoff
        seconds = round(time.time() - started)
        self.records.append({'worker': tag, 'node': node['id'], 'category': 'script', 'model': 'script',
                             'reported_model': None, 'started': round(started), 'seconds': seconds,
                             'exit': 0 if ok else 1, 'limit_waits': 0, 'ok': ok})
        self.save_state()
        print(f"{tag:<28}{'script':<16}exit={0 if ok else 1:<4}{seconds:>6}s", flush=True)
        if not ok:
            print(f"{node['id']}: script failed: {failure.splitlines()[0]}", flush=True)
        return ok

    def run_acceptance(self, node: dict) -> bool:
        """A node with the role `acceptance` is done by the script, with no model (added 2026-11-16 at the owner's
        instruction: nodes check themselves cheaply, the full acceptance runs once, at the end, and a failure goes
        back to one node that may change anything).

        The commands are the backticked pieces of the node's VERIFY line, in order, each run through the shell in
        the repository folder with a limit of SG_ACCEPTANCE_SECONDS. All exit 0: accepted. Otherwise a fix worker
        gets the failing commands with the end of their output, then every command runs again; at most
        MAX_FIX_ROUNDS fix rounds. Each round is kept in result.json (`acceptance`)."""
        commands = re.findall(r'`([^`]+)`', node['VERIFY'])
        if not commands:
            print(f"{node['id']}: no command in backticks in its VERIFY line", flush=True)
            return False
        progress = self.node_progress(node['id'])
        while True:
            fixes = progress['fix_rounds']   # the rounds done so far, over all segments (they number the files)
            entry = {'node': node['id'], 'round': fixes, 'commands': []}
            failures = []
            for command in commands:
                code, output, seconds = self.shell(command, self.repo, 'acceptance command', node['id'])
                # `tail` (the last 2,000 characters of the output) is a record for the viewer; no prompt reads it
                entry['commands'].append({'command': command, 'exit': code, 'seconds': seconds, 'tail': output[-2000:]})
                print(f"{node['id'] + ' ' + command[:44]:<60}exit={code:<4}{seconds:>6}s", flush=True)
                if code != 0:
                    failures.append((command, code, output[-3000:]))
            self.acceptance.append(entry)
            with open(f"{self.out}/acceptance-{node['id']}-{fixes}.json", 'w') as handle:
                json.dump({'entry': entry, 'failures': failures}, handle, indent=1)
            progress['acceptance_rounds'] += 1
            self.save_state()
            if not failures:
                return True
            if fixes - progress['fix_base'] >= MAX_FIX_ROUNDS:
                return False
            fixes += 1
            progress['fix_rounds'] = fixes
            report = '\n\n'.join(f'COMMAND: {command}\nEXIT CODE: {code}\nEND OF OUTPUT:\n{output}'
                                 for command, code, output in failures)
            fix = {'id': f"{node['id']}-fix{fixes}", 'label': 'Repair what the full acceptance found',
                   'TASK': ('All build nodes are finished and their work is gathered in the repository as one '
                            'uncommitted change. The coordinator ran the full acceptance of the whole change and it '
                            'failed. Find the cause and repair it under the rules of the task statement: you may '
                            'change any file the task statement allows, whichever node wrote it. Do not weaken, '
                            'skip or delete a test to make it pass. What failed:\n\n' + report + '\n'),
                   'DELIVERABLE': 'the repair; in the handoff, the cause of each failure and what was changed for it.',
                   'SCOPE': 'only files the task statement allows; change nothing the failures do not need.',
                   'VERIFY': 'the failing commands above exit 0 when you run them again.',
                   'STOP WHEN': 'those commands pass, or a failure cannot be repaired without breaking a rule of '
                                'the task statement (then report which and why).'}
            self.ask(fix['id'], 'deep', self.prompt(fix))
            self.checkpoint(node['id'], f"{fix['id']} done")

    def run_review(self, node: dict) -> bool:
        """Review, and on CHANGES REQUESTED a fix and a second review, as the lead's rule says."""
        progress = self.node_progress(node['id'])
        unreadable = 0
        while True:
            fixes = progress['fix_rounds']   # fix rounds done so far, over all segments (they number the fix workers)
            record = self.ask(node['id'], node.get('route_category') or 'review', self.prompt(node))
            progress['review_rounds'] += 1
            self.save_state()
            found = re.search(VERDICT, record['answer'])
            if not record['ok'] or not found:
                self.verdicts.append('unreadable')
                self.findings.append(None)
                unreadable += 1
                if unreadable > MAX_REPAIRS:
                    return False
                continue
            self.verdicts.append(found.group(1))
            self.findings.append(len(re.findall(FINDING_LINE, record['answer'])))
            if found.group(1) == 'PASS':
                return True
            if fixes - progress['fix_base'] >= MAX_FIX_ROUNDS:
                return False
            fixes += 1
            progress['fix_rounds'] = fixes
            self.save_state()
            fix = {'id': f"{node['id']}-fix{fixes}", 'label': 'Fix the review findings',
                   'TASK': ('An independent reviewer read the whole change and requested changes. Fix every finding '
                            'below, under the rules of the task statement. The review, word for word:\n\n'
                            + record['answer'].strip() + '\n'),
                   'DELIVERABLE': 'the fixes; in the handoff, each finding and what was changed for it.',
                   'SCOPE': 'only files the task statement allows; change nothing the findings do not need.',
                   'VERIFY': 'every command under "Done means" in the task statement exits 0.',
                   'STOP WHEN': 'those commands pass, or a finding cannot be fixed as written (then report which and why).'}
            self.ask(fix['id'], 'deep', self.prompt(fix))
            self.checkpoint(node['id'], f"{fix['id']} done")

    def execute(self) -> str:
        # (a resumed run starts with the nodes that git confirmed as accepted; they are not run again)
        status: dict[str, str] = {node['id']: 'accepted' if node['id'] in self.carried else 'waiting'
                                  for node in self.nodes}
        self.status = status   # the same dict, so a stop can report the statuses reached so far
        # With SG_HUMAN a node that waits for a human gives its slot back (see `guarded`), so the pool has a thread for
        # every node and the slots are counted by the semaphore; without it the pool is the old MAX_PARALLEL.
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(self.nodes)) if HUMAN else MAX_PARALLEL) as pool:
            running: dict[concurrent.futures.Future, str] = {}
            submit = (lambda fn, *args: pool.submit(self.guarded, fn, *args)) if HUMAN else pool.submit
            while True:
                for node in self.nodes:
                    if self.halt or self.stop_reason:
                        break   # the human stopped the run (or did not answer), or the cost limit was reached: no node starts
                    if status[node['id']] == 'waiting' and all(status[dep] == 'accepted' for dep in node['deps']):
                        status[node['id']] = 'running'
                        with self.state_lock:
                            record = self.node_progress(node['id'])
                            record.update(status='running', commit=None, checkpoint=None, changed=False, reason=None)
                            if node['id'] not in self.segments[-1]['nodes_run']:
                                self.segments[-1]['nodes_run'].append(node['id'])
                        self.save_state()
                        job = {'review': self.run_review, 'acceptance': self.run_acceptance,
                               'script': self.run_script}.get(kind_of(node), self.run_plain)
                        if HUMAN and kind_of(node) == 'human':
                            running[pool.submit(self.run_human, node)] = node['id']   # no worker: needs no slot and no lock
                        elif NODE_WORKTREES and kind_of(node) in ('build', 'script'):
                            running[submit(self.run_in_own_copy, node)] = node['id']
                        elif NODE_WORKTREES:
                            running[submit(self.run_in_repository, node, job)] = node['id']
                        else:
                            running[submit(self.in_shared_folder, node, job)] = node['id']
                if not running:
                    break
                done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    node_id = running.pop(future)
                    try:
                        accepted = future.result()
                    except CostLimit as stop:   # the node asked for a worker the limit does not allow
                        print(f'{node_id}: {stop}', flush=True)
                        self.fail_node(node_id, COST_LIMIT)
                        accepted = False
                    except Exception as error:  # a broken worker fails its node, not the run's bookkeeping
                        print(f'{node_id}: {error!r}', flush=True)
                        accepted = False
                    status[node_id] = 'accepted' if accepted else 'failed'
                    with self.state_lock:
                        record = self.node_progress(node_id)
                        record['status'] = status[node_id]
                        if accepted:
                            record.update(commit=record.get('checkpoint') or self.tip,
                                          segment=len(self.segments))
                    self.save_state()
        if self.halt:
            return self.halt   # 'stopped_by_human' or 'no_answer_from_human'
        reviewed = [node['id'] for node in self.nodes if kind_of(node) == 'review']
        passed_review = not reviewed or (bool(self.verdicts) and self.verdicts[-1] == 'PASS')
        return 'verified' if all(value == 'accepted' for value in status.values()) and passed_review else 'not_verified'


# Exit codes of `pangoloop run` (also in `run --help`). SIGTERM stops a run with 124 as before.
EXIT_VERIFIED, EXIT_NOT_VERIFIED, EXIT_CONFIG, EXIT_ABORTED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


def exit_code(verdict: str) -> int:
    """0 verified; 3 a person's ABORT; 1 every other ending (a node failed, the review did not pass, the acceptance
    failed, nobody answered a request in time)."""
    return {'verified': EXIT_VERIFIED, 'stopped_by_human': EXIT_ABORTED}.get(verdict, EXIT_NOT_VERIFIED)


def main(argv: list[str] | None = None) -> int:
    """Runs the plan; returns the process's exit code (see exit_code). 2 when the plan or the setup is wrong, which is
    found before any node starts."""
    argv = sys.argv[1:] if argv is None else argv
    signal.signal(signal.SIGTERM, stop_workers)
    signal.signal(signal.SIGINT, stop_workers)   # Ctrl-C ends the run as cleanly as SIGTERM, with exit code 130
    global CURRENT
    options = {'reset_rounds': False, 'accept_plan_changes': False}
    resuming = False
    positional = []
    for item in argv:
        if item == '--resume':
            resuming = True
        elif item == '--reset-rounds':
            options['reset_rounds'] = True
        elif item == '--accept-plan-changes':
            options['accept_plan_changes'] = True
        else:
            positional.append(item)
    try:
        if len(positional) != 3 or any(item.startswith('--') for item in positional):
            raise SystemExit('pangoloop: usage: run <plan file> <repository folder> <run name> '
                             '[--resume [--reset-rounds] [--accept-plan-changes]]')
        if (options['reset_rounds'] or options['accept_plan_changes']) and not resuming:
            raise SystemExit('pangoloop: --reset-rounds and --accept-plan-changes belong to --resume')
        if not os.path.isfile(positional[0]):
            raise SystemExit(f'pangoloop: no such plan file: {positional[0]}')
        if not os.path.isdir(positional[1]):
            raise SystemExit(f'pangoloop: no such repository folder: {positional[1]}')
        if not resuming and os.path.exists(f'{RUNS}/{positional[2]}'):
            raise SystemExit(f'pangoloop: the run folder {RUNS}/{positional[2]} exists already; a run name is never '
                             'reused (give --resume to continue that run)')
        if resuming and not os.path.isdir(f'{RUNS}/{positional[2]}'):
            raise SystemExit(f'pangoloop: cannot resume: there is no run folder {RUNS}/{positional[2]}')
        run = Run(positional[0], positional[1], positional[2], options if resuming else None)
    except SystemExit as stop:
        if isinstance(stop.code, str):
            print(stop.code, file=sys.stderr, flush=True)
            return EXIT_CONFIG
        raise
    except OSError as error:   # a run name already used, an unreadable plan
        print(f'pangoloop: {error}', file=sys.stderr, flush=True)
        return EXIT_CONFIG
    CURRENT = run
    if resuming:
        print(f'resume: segment {len(run.segments)} of {run.name}', flush=True)
        for line in run.resume_notes:
            print(f'resume: {line}', flush=True)
    else:
        with open(f'{run.out}/started_at', 'w') as handle:
            handle.write(stamp() + '\n')
    try:
        verdict = run.execute()
    finally:
        run.leave_as_uncommitted()   # whatever happened, the repository folder shows the accepted work
    with open(f'{run.out}/finished_at', 'w') as handle:
        handle.write(stamp() + '\n')
    run.end_segment(verdict)
    write_result(run, verdict, run.status)
    resume.release_lock(run.out)
    print(f'verdict={verdict} reviews={run.verdicts} nodes={run.status}', flush=True)
    return exit_code(verdict)


if __name__ == '__main__':
    sys.exit(main())
