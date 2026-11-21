"""Resuming a run that ended before it was verified (added 2026-11-19 at the owner's instruction).

Three things live here, apart from the engine's loop (pangoloop/graph.py):

  - the state file `<run>/state.json`, which the engine rewrites atomically at every transition (a node starts, a worker
    starts or ends, a node is integrated, a review or acceptance round ends, a request opens or is answered), and the
    lock `<run>/lock.json` (process id and start time) that says a process is running the folder;
  - the checkpoints: after a node's work is accepted (and after every fix round of a review or an acceptance) the
    work as it stands is recorded as a commit in a chain of its own, `refs/pangoloop/<run>/tip`, made with a temporary
    index so that neither the branch, nor the real index, nor the working files are touched. A node's `commit` in the
    state is such a commit; git, not the state file, says whether it still stands (it must be an ancestor of the tip);
  - `begin`: what a resume checks, what it trusts, what it sets aside and what it hands to the engine.

Per-node state is a dictionary that may gain fields; the folders and branches a node used are lists (`worktrees`).
"""
import datetime
import json
import os
import re
import shutil
import subprocess

from pangoloop import children
from pangoloop.plan import kind_of

STATE_FILE = 'state.json'
LOCK_FILE = 'lock.json'
SOURCE_FILE = 'plan.source.md'    # the plan file as given, before the placeholders were replaced (`pangoloop resume` reads it)
STATE_VERSION = 1
NODE_STATUSES = ('pending', 'running', 'integrating', 'accepted', 'failed')
CHECK_EXCLUDED_ROLES = ('build', 'script', 'human')   # every other role only checks (or repairs) the accepted work
GUARDED_FIELDS = ('TASK', 'SCOPE', 'VERIFY', 'write_scope', 'STEPS', 'steps_file', 'steps_file_format')


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def refuse(message: str):
    """A refusal of a resume: the message goes to standard error and the exit status is 2 (cli.main)."""
    raise SystemExit(f'pangoloop: cannot resume: {message}')


def read_json(path: str):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json(path: str, data) -> None:
    temp = f'{path}.tmp'
    with open(temp, 'w') as handle:
        json.dump(data, handle, indent=1)
    os.replace(temp, path)   # never a half-written file, even when the process is killed in the middle


def read_state(folder: str) -> dict | None:
    data = read_json(os.path.join(folder, STATE_FILE))
    return data if isinstance(data, dict) and data.get('version') == STATE_VERSION else None


def write_state(folder: str, state: dict) -> None:
    write_json(os.path.join(folder, STATE_FILE), state)


# ---------------------------------------------------------------- the lock

def _ps(pid: int, field: str) -> str:
    try:
        return subprocess.run(['ps', '-o', f'{field}=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ''


def process_alive(pid, started: str | None = None) -> bool:
    """True when `pid` is a live process (not a zombie) and, if its start time was recorded, the same one."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    status = _ps(pid, 'stat')
    if not status or status.startswith('Z'):
        return False
    return not (started and _ps(pid, 'lstart') not in ('', started))   # a reused process id is another process


def acquire_lock(folder: str) -> str | None:
    """Takes the folder's lock. Refuses (exit 2) when a live process holds it; a lock whose process is gone is stale and
    is taken over, and the returned sentence says so (None when there was none). A lock of this very process is a
    leftover of an earlier engine object in it and is taken over too."""
    path = os.path.join(folder, LOCK_FILE)
    info = {'pid': os.getpid(), 'started_at': stamp(), 'process_started': _ps(os.getpid(), 'lstart')}
    note = None
    for _ in range(3):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            held = read_json(path)
            held = held if isinstance(held, dict) else {}
            pid = held.get('pid')
            if pid != os.getpid() and process_alive(pid, held.get('process_started')):
                refuse(f"the run is being run by process {pid} (started {held.get('started_at')}); "
                       'stop it first, or wait until it ends')
            note = (f"the lock of process {pid} (started {held.get('started_at')}) is stale, that process is gone; "
                    'taken over')
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(handle, 'w') as out:
            json.dump(info, out)
        return note
    refuse('the run folder is locked by a process that keeps appearing; try again')


def release_lock(folder: str) -> None:
    path = os.path.join(folder, LOCK_FILE)
    held = read_json(path)
    if isinstance(held, dict) and held.get('pid') == os.getpid():
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------- git

def git(cwd: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-c', 'user.name=sg', '-c', 'user.email=sg@localhost', '-c', 'core.editor=true', *args],
                          cwd=cwd, capture_output=True, text=True, env=env)


def out(cwd: str, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def ref_name(run: str, repo: str | None = None) -> str:
    """The run's own chain `refs/pangoloop/<run>/tip`. (`repo` is accepted for the callers' sake and not used.)"""
    tail = re.sub(r'[^A-Za-z0-9._-]', '_', run) + '/tip'
    return 'refs/pangoloop/' + tail


def exists(repo: str, sha: str | None) -> bool:
    return bool(sha) and git(repo, 'cat-file', '-e', f'{sha}^{{commit}}').returncode == 0


def is_ancestor(repo: str, older: str | None, newer: str | None) -> bool:
    return bool(older and newer) and git(repo, 'merge-base', '--is-ancestor', older, newer).returncode == 0


def tree_of(repo: str, sha: str) -> str:
    return out(repo, 'rev-parse', f'{sha}^{{tree}}')


def work_tree(repo: str, folder: str, exclude: str | None) -> str:
    """The id of a tree of everything in the repository folder that git does not ignore, made through a temporary
    index (kept in the run folder, so that unchanged files are not hashed again). The real index is not touched."""
    index = os.path.join(folder, 'snapshot.index')
    env = dict(os.environ, GIT_INDEX_FILE=index)
    if not os.path.exists(index):
        git(repo, 'read-tree', 'HEAD', env=env)
    spec = ['--', '.'] + ([f':(exclude){exclude}'] if exclude else [])
    git(repo, 'add', '-A', *spec, env=env)
    return git(repo, 'write-tree', env=env).stdout.strip()


def exclusion(repo: str, folder: str) -> str | None:
    """The run folder as a path relative to the repository, when it lies inside it (it is never part of a checkpoint)."""
    relative = os.path.relpath(os.path.realpath(folder), repo)
    return None if relative.startswith('..') else relative


def checkpoint(repo: str, folder: str, run: str, parent: str, message: str) -> str:
    """Records the work as it stands as a commit after `parent` and moves the run's tip to it. When nothing changed since
    `parent` that is `parent` itself. Never touches the branch, the real index or the files."""
    tree = work_tree(repo, folder, exclusion(repo, folder))
    if parent and tree_of(repo, parent) == tree:
        return parent
    done = git(repo, 'commit-tree', tree, '-p', parent, '-m', message)
    sha = done.stdout.strip()
    if done.returncode or not sha:
        raise RuntimeError(f'git commit-tree: {done.stderr.strip()[-300:]}')
    git(repo, 'update-ref', ref_name(run, repo), sha)
    return sha


def abort_in_progress(repo: str) -> list[str]:
    """Aborts a rebase, a merge or a cherry-pick left in progress in the repository; what was aborted."""
    found: list[str] = []
    for name, command in (('rebase-merge', ['rebase', '--abort']), ('rebase-apply', ['rebase', '--abort']),
                          ('MERGE_HEAD', ['merge', '--abort']), ('CHERRY_PICK_HEAD', ['cherry-pick', '--abort'])):
        path = out(repo, 'rev-parse', '--git-path', name)
        path = path if os.path.isabs(path) else os.path.join(repo, path)
        if os.path.exists(path) and command[0] not in found:
            git(repo, *command)
            found.append(command[0])
    return [f'a {item} left in progress in the repository was aborted' for item in found]


# ---------------------------------------------------------------- reading a run folder

def segment_end(verdict: str | None) -> str:
    """Why a segment ended, from the verdict the engine wrote."""
    return {'verified': 'verified', 'stopped_by_human': 'aborted', 'stopped': 'interrupted'}.get(verdict or '', 'not_verified')


def fresh_node() -> dict:
    return {'status': 'pending', 'commit': None, 'segment': None, 'workers': [], 'worktrees': [], 'review_rounds': 0,
            'fix_rounds': 0, 'fix_base': 0, 'acceptance_rounds': 0, 'changed': False, 'checkpoint': None}


def highest_worker(folder: str) -> int:
    top = 0
    path = os.path.join(folder, 'workers')
    for entry in os.listdir(path) if os.path.isdir(path) else []:
        found = re.match(r'^(\d+)-', entry)
        if found:
            top = max(top, int(found.group(1)))
    return top


def read_text(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def reconstruct(run, folder: str, old_nodes: list[dict], notes: list[str]) -> dict:
    """The state of a run folder written before the state file existed, as far as the folder and git prove it. `run`
    gives the repository. Nothing provable: refuse."""
    repo = run.repo
    result = read_json(os.path.join(folder, 'result.json')) or {}
    head = out(repo, 'rev-parse', 'HEAD')
    dirty = work_tree(repo, folder, exclusion(repo, folder)) != tree_of(repo, head)
    in_copies = bool(result.get('node_worktrees')) or os.path.isdir(os.path.join(folder, 'wt')) or any(
        out(repo, 'branch', '--list', f"sg/{node['id']}") for node in old_nodes)
    recorded = result.get('nodes') or {}
    state = {'version': STATE_VERSION, 'run': run.name, 'repo': repo, 'branch': run.branch, 'base': head,
             'node_worktrees': in_copies, 'sequence': highest_worker(folder), 'tip': None, 'segments': [], 'nodes': {},
             'records': [r for r in (result.get('workers') or []) if isinstance(r, dict)],
             'verdicts': list(result.get('review_verdicts') or []), 'findings': list(result.get('review_findings') or []),
             'acceptance': list(result.get('acceptance') or []), 'copies': list(result.get('own_copies') or []),
             'human_log': list(result.get('human') or []), 'human_asks': {}, 'plan_changes': [], 'reconstructed': True}
    if not state['records']:
        for entry in sorted(os.listdir(os.path.join(folder, 'workers'))):
            found = re.match(r'^(\d+)-(.+)$', entry)
            if found:
                state['records'].append({'worker': entry, 'node': found.group(2), 'seconds': 0, 'ok': True})
    proven = []
    for node in old_nodes:
        node_id = node['id']
        branch_tip = out(repo, 'rev-parse', '--verify', '-q', f'sg/{node_id}')
        if kind_of(node) in ('build', 'script') and in_copies:
            if recorded:
                ok = recorded.get(node_id) == 'accepted' and bool(branch_tip)
            else:   # no result.json: only a node whose branch is in the history, with no copy left behind
                ok = bool(branch_tip) and is_ancestor(repo, branch_tip, head) and not os.path.isdir(
                    os.path.join(folder, 'wt', node_id))
        else:
            ok = recorded.get(node_id) == 'accepted' and dirty
        if ok:
            proven.append(node_id)
    if not proven:
        refuse('this run folder has no state file (it was written before resume existed) and nothing in it can be '
               'proven from the folder and from git: no accepted node can be confirmed. Start a new run')
    tip = checkpoint(repo, folder, run.name, head, f'resume: work of {run.name} as found')
    state['tip'] = tip
    for node in old_nodes:
        record = fresh_node()
        if node['id'] in proven:
            record.update(status='accepted', commit=tip, segment=1)
        elif recorded.get(node['id']) == 'failed':
            record['status'] = 'failed'
        state['nodes'][node['id']] = record
    state['segments'] = [{'n': 1, 'started_at': read_text(os.path.join(folder, 'started_at')),
                          'ended_at': read_text(os.path.join(folder, 'finished_at')),
                          'ended': segment_end(result.get('verdict')) if result else 'killed',
                          'verdict': result.get('verdict'), 'nodes_run': [n['id'] for n in old_nodes],
                          'first_worker': 1, 'last_worker': state['sequence'], 'reconstructed': True}]
    notes.append(f"no state file: reconstructed from the folder and git; proven accepted: {', '.join(proven)}")
    return state


# ---------------------------------------------------------------- begin

def descendants(nodes: list[dict], node_id: str) -> set[str]:
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node['id'] not in found and (node_id in node['deps'] or found & set(node['deps'])):
                found.add(node['id'])
                changed = True
    return found


def begin(run, options: dict) -> tuple[dict, set[str], list[str]]:
    """Checks that the folder can be resumed, decides what is carried over, sets aside what must be, and returns the
    state to continue from, the ids of the accepted nodes that are skipped, and the report lines.

    Every refusal comes before the first change, except that the lock is taken (and given back on a refusal)."""
    folder = run.out
    notes: list[str] = []
    if not (os.path.isdir(folder) and os.path.isfile(os.path.join(folder, 'plan.md'))
            and os.path.isdir(os.path.join(folder, 'workers'))):
        refuse(f'{folder} is not a run folder (no plan.md and workers/ in it)')
    result = read_json(os.path.join(folder, 'result.json')) or {}
    state = read_state(folder)
    if result.get('verdict') == 'verified' or (state and state['segments']
                                               and state['segments'][-1].get('ended') == 'verified'):
        refuse(f'the run {run.name} is verified; there is nothing to resume')
    taken = acquire_lock(folder)
    try:
        if taken:
            notes.append(taken)
        # before anything else: what the engine that died had started and left running (pangoloop/children.py)
        notes += ['orphan ' + line for line in children.stop_recorded((state or {}).get('children'))]
        return _begin(run, options, state, notes)
    except BaseException:
        release_lock(folder)
        raise


def _begin(run, options: dict, state: dict | None, notes: list[str]) -> tuple[dict, set[str], list[str]]:
    folder, repo = run.out, run.repo
    from pangoloop.plan import parse_nodes
    old_text = open(os.path.join(folder, 'plan.md')).read()
    old_nodes = parse_nodes(old_text, strict=False)
    if state is None:
        state = reconstruct(run, folder, old_nodes, notes)
    # ---- the repository and the branch are the run's
    if os.path.realpath(state['repo']) != repo:
        refuse(f"the repository is {repo}, the run's is {state['repo']}")
    if run.branch != state['branch']:
        refuse(f"the repository is on branch {run.branch}, the run's branch is {state['branch']}")
    head = out(repo, 'rev-parse', 'HEAD')
    base = state['base']
    if not exists(repo, base) or not is_ancestor(repo, base, head):
        refuse(f'the branch {run.branch} no longer contains the commit {base[:10]} the run started from')
    foreign = [name for name in git(repo, 'log', '--format=%an', f'{base}..{head}').stdout.split() if name != 'sg']
    if foreign:
        refuse(f"the branch {run.branch} has {len(foreign)} commit(s) since the run started that are not the run's")
    tip = state.get('tip') if exists(repo, state.get('tip')) else None
    if tip and not is_ancestor(repo, base, tip):
        tip = None
    # ---- what git confirms
    n = len(state['segments'])        # the number of this resume; the new segment is n + 1
    new_nodes = {node['id']: node for node in run.nodes}
    carried: set[str] = set()
    reasons: dict[str, str] = {}
    for node in run.nodes:
        record = state['nodes'].get(node['id'])
        status = record['status'] if record else 'pending'
        if status == 'accepted':
            if tip and is_ancestor(repo, record.get('commit'), tip):
                carried.add(node['id'])
            else:
                reasons[node['id']] = (f"recorded as accepted, but its integration commit {str(record.get('commit'))[:10]} "
                                       "is not an ancestor of the run's history now; it runs again")
        elif status in ('running', 'integrating'):
            reasons[node['id']] = f'was {status} when the run stopped; it runs again from the start'
        elif status == 'failed':
            reasons[node['id']] = 'failed before; it runs again'
    # a check that passed is not repeated unless work was integrated after it by a node that does not follow it
    for node in run.nodes:
        if node['id'] in carried and kind_of(node) not in CHECK_EXCLUDED_ROLES:
            record, after = state['nodes'][node['id']], descendants(run.nodes, node['id'])
            for other in sorted(carried):
                theirs = state['nodes'][other]
                if (other != node['id'] and other not in after and theirs.get('changed')
                        and record['commit'] != theirs['commit'] and is_ancestor(repo, record['commit'], theirs['commit'])):
                    reasons[node['id']] = f'work was integrated after it passed (node {other}); it runs again'
                    carried.discard(node['id'])
                    break
    changed = True
    while changed:   # a node cannot stay accepted when a node it depends on runs again
        changed = False
        for node in run.nodes:
            missing = [dep for dep in node['deps'] if dep not in carried]
            if node['id'] in carried and missing:
                reasons[node['id']] = f"its dependency {', '.join(missing)} runs again, so it does too"
                carried.discard(node['id'])
                changed = True
    # ---- the plan may change, but not under an accepted node
    old_by_id = {node['id']: node for node in old_nodes}
    problems = []
    for node_id in sorted(carried):
        before = old_by_id.get(node_id)
        for field in GUARDED_FIELDS if before else ():
            if (before.get(field) or '') != (new_nodes[node_id].get(field) or ''):
                problems.append({'node': node_id, 'field': field})
    for node_id, record in sorted(state['nodes'].items()):
        if record['status'] == 'accepted' and node_id not in new_nodes and tip \
                and is_ancestor(repo, record.get('commit'), tip):
            problems.append({'node': node_id, 'field': 'the node itself (removed from the plan)'})
    if problems and not options.get('accept_plan_changes'):
        text = '; '.join(f"node {p['node']}: {p['field']}" for p in problems)
        refuse(f'the plan changed under accepted nodes ({text}). Restore it, or give --accept-plan-changes to record '
               'the change and keep their work')
    # ---- from here on things change
    notes += abort_in_progress(repo)
    if tip:
        _restore_work(run, state, tip, head, bool(state.get('node_worktrees')), n, notes)
    git(repo, 'worktree', 'prune')
    for node in run.nodes:
        if node['id'] not in carried:
            _set_aside(run, node['id'], n, notes)
    last = state['segments'][-1] if state['segments'] else None
    if last and not last.get('ended_at'):   # no finished_at, no result.json, no live process
        last.update(ended_at=state.get('updated_at') or stamp(), ended='killed', verdict=None,
                    note='no finished_at and no result.json: killed or lost, as far as can be told')
    shutil.copyfile(os.path.join(folder, 'plan.md'), os.path.join(folder, f'plan.before-resume-{n}.md'))
    if os.path.exists(os.path.join(folder, 'result.json')):
        os.replace(os.path.join(folder, 'result.json'), os.path.join(folder, f'result.segment-{n}.json'))
    if os.path.exists(os.path.join(folder, 'finished_at')):
        os.remove(os.path.join(folder, 'finished_at'))
    for item in problems:
        state['plan_changes'].append({'segment': n + 1, **item})
        notes.append(f"plan change under accepted node {item['node']} ({item['field']}) accepted and recorded")
    notes += _supersede_requests(run, state, n + 1)
    nodes: dict[str, dict] = {}
    for node in run.nodes:
        record = state['nodes'].get(node['id']) or fresh_node()
        if node['id'] in carried:
            record['status'] = 'accepted'
        else:
            record.update(status='pending', commit=None, segment=None, changed=False, checkpoint=None, reason=None)
            if options.get('reset_rounds'):
                record.update(fix_base=record['fix_rounds'], review_rounds=0, acceptance_rounds=0)
        nodes[node['id']] = record
    state['nodes'] = nodes
    state['tip'] = tip
    for node in run.nodes:
        if node['id'] in carried:
            notes.append(f"{node['id']}: accepted in segment {nodes[node['id']]['segment']}, skipped")
        elif node['id'] in reasons:
            notes.append(f"{node['id']}: {reasons[node['id']]}")
    return state, carried, notes


def _restore_work(run, state: dict, tip: str, head: str, worktree_mode: bool, number: int, notes: list[str]) -> None:
    """Makes the repository folder show the work of the tip and, with node worktrees, makes the branch hold it (the
    nodes' copies are made from the branch). What was in the folder and is not in the tip (the half-done work of a node
    that was stopped) is saved as a patch in the run folder first."""
    repo, folder = run.repo, run.out
    wanted = tree_of(repo, tip)
    current = work_tree(repo, folder, exclusion(repo, folder))
    if current != wanted:
        os.makedirs(os.path.join(folder, 'interrupted'), exist_ok=True)
        patch = git(repo, 'diff', '--binary', wanted, current).stdout
        with open(os.path.join(folder, 'interrupted', f'resume-{number}-tree.patch'), 'w') as handle:
            handle.write(patch)
        added = git(repo, 'diff', '--name-only', '--diff-filter=A', wanted, current).stdout.split('\n')
        git(repo, 'read-tree', '--reset', '-u', wanted)
        for name in added:
            if name and os.path.isfile(os.path.join(repo, name)):
                os.remove(os.path.join(repo, name))
        notes.append(f'the folder held work that is not in the accepted work; saved as '
                     f'interrupted/resume-{number}-tree.patch and removed from the folder')
    if worktree_mode:
        if tree_of(repo, head) != wanted:
            made = git(repo, 'commit-tree', wanted, '-p', head, '-m', f'resume: accepted work of {run.name}').stdout.strip()
            git(repo, 'reset', '-q', '--hard', made)
        else:
            git(repo, 'reset', '-q', '--hard', head)
    else:
        git(repo, 'reset', '-q', '--mixed', head)


def _set_aside(run, node_id: str, number: int, notes: list[str]) -> None:
    """A node that runs again gets a fresh worktree and branch: the old ones are kept under a name that says so."""
    repo, folder = run.repo, run.out
    copy = os.path.join(folder, 'wt', node_id)
    branch = f'sg/{node_id}'
    if os.path.isdir(copy):
        target = os.path.join(folder, 'wt', f'{node_id}-interrupted-{number}')
        while os.path.exists(target):
            target += '+'
        if git(repo, 'worktree', 'move', copy, target).returncode:
            os.rename(copy, target)
            git(repo, 'worktree', 'repair', target)
        notes.append(f'{node_id}: its old copy is kept as wt/{os.path.basename(target)}')
    if git(repo, 'rev-parse', '--verify', '-q', branch).returncode == 0:
        name = f'{branch}-interrupted-{number}'
        while git(repo, 'rev-parse', '--verify', '-q', name).returncode == 0:
            name += '+'
        git(repo, 'branch', '-m', branch, name)


def _supersede_requests(run, state: dict, segment: int) -> list[str]:
    """A request an interrupted worker left open is closed as `superseded`, not answered. An answer that was written but
    never read by the engine is made a ruling, so that the next prompts carry it."""
    from pangoloop import rulings
    folder = os.path.join(run.out, 'human')
    notes = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        found = re.match(r'^(.+)-(\d+)\.json$', name)
        info = read_json(os.path.join(folder, name))
        if not found or not isinstance(info, dict) or info.get('state') != 'open':
            continue
        stem = name[:-5]
        try:
            answer = open(os.path.join(folder, stem + '.answer.txt'), errors='replace').read()
        except OSError:
            answer = ''
        if answer.strip():
            if answer.split('\n', 1)[0].rstrip('\r') != 'ABORT' and stem not in {r['id'] for r in rulings.load(run.out)}:
                rulings.append(run.out, {'id': stem, 'node': info.get('node'), 'worker': info.get('worker'),
                                         'by': info.get('answered_by') or info.get('for') or 'person', 'at': stamp(),
                                         'question': info.get('request') or '', 'answer': answer.rstrip('\n')})
            info['state'] = 'answered'
            write_json(os.path.join(folder, name), info)
            continue
        info.update(state='superseded', superseded_at=stamp(), superseded_by=f'resume, segment {segment}')
        write_json(os.path.join(folder, name), info)
        for entry in state.get('human_log', []):
            if entry.get('node') == info.get('node') and entry.get('k') == int(found.group(2)):
                entry['state'] = 'superseded'
        notes.append(f'request {stem} of an interrupted worker is closed as superseded')
    return notes
