"""`pangoloop status [<run>] [--json]`: what the runs of the project are doing, for a person or for a program.

With no run: the project's runs, newest first. With a run: its nodes, its open requests, its cost so far and its verdict.
`--json` prints ONE JSON object; its fields are a contract (docs/SETUP.md says them, tests/test_runs_and_pages.py and tests/test_docs.py check them).

No run (`status --json`):

    {"project": "<root>" | null, "page": "http://127.0.0.1:5420/" | null, "runs_folder": "<path>",
     "runs": [{"name", "state", "verdict", "started", "ended", "waiting_for", "open_requests"}, ...]}

One run (`status <run> --json`):

    {"name", "state", "verdict", "started", "ended", "folder", "plan", "repo", "branch", "runner", "page",
     "waiting_for", "reviews", "cost", "nodes": [{"id", "kind", "role", "label", "state", "depends_on", "workers"}],
     "requests": [{"id", "node", "for", "question", "asked_at"}]}

`state` of a run is one of: running, verified, not verified, aborted, stopped. `running` means the engine's process is alive
(a run that waits for an answer is running; `waiting_for` says `person` or `coordinator` then, else null). `stopped` is a run
that has no result and no engine: it was stopped (SIGTERM, Ctrl-C) or killed, and `pangoloop resume <run>` can continue it.
`verdict` is the engine's own word (verified, not_verified, stopped_by_human, no_answer_from_human, stopped) or null while
the run has none. `started` and `ended` are UTC times as `2026-11-20T10:00:00Z`, or null. `state` of a node is one of: waiting,
running, integrating, accepted, failed, waiting for human, waiting for coordinator. `cost` is the object the engine keeps in
state.json: `usd` (null when no worker's cost is known), `partial`, `workers`, `workers_with_cost`, `limit_usd`, and more.
`reviews` lists the verdicts of the review rounds so far. Exit status: 0; 2 when the run does not exist.
"""
import argparse
import json
import os
import sys
import time

from pangoloop import costs, paths, requests, resume, viewfile
from pangoloop.plan import parse_nodes

VERDICT_STATE = {'verified': 'verified', 'stopped_by_human': 'aborted', 'stopped': 'stopped'}
RECENT_SECONDS = 10   # a folder this young with no lock yet is a run that is starting


def _text(path: str) -> str | None:
    try:
        with open(path, errors='replace') as handle:
            return handle.read().strip()
    except OSError:
        return None


def engine_alive(folder: str) -> bool:
    held = resume.read_json(os.path.join(folder, resume.LOCK_FILE))
    return isinstance(held, dict) and resume.process_alive(held.get('pid'), held.get('process_started'))


def run_state(folder: str) -> dict:
    """The facts of a run folder that both forms of the answer need."""
    result = resume.read_json(os.path.join(folder, 'result.json'))
    result = result if isinstance(result, dict) else None
    state = resume.read_state(folder)
    verdict = result.get('verdict') if result else None
    started = _text(os.path.join(folder, 'started_at'))
    if not started and state and state.get('segments'):
        started = state['segments'][0].get('started_at')
    ended = _text(os.path.join(folder, 'finished_at'))
    if result:
        word = VERDICT_STATE.get(str(verdict), 'not verified')
    elif engine_alive(folder):
        word = 'running'
    elif ended:
        word = 'not verified'
    elif not os.path.exists(os.path.join(folder, resume.LOCK_FILE)) and time.time() - os.stat(folder).st_mtime < RECENT_SECONDS:
        word = 'running'
    else:
        word = 'stopped'
    open_here = [item for item in requests.load_requests(folder) if item['state'] == 'open'] if word == 'running' else []
    waiting = 'person' if any(i['for'] == 'person' for i in open_here) else ('coordinator' if open_here else None)
    return {'state': word, 'verdict': verdict, 'started': started, 'ended': ended, 'result': result, 'saved': state,
            'open': open_here, 'waiting_for': waiting}


def request_json(item: dict) -> dict:
    return {'id': item['id'], 'node': item['node'], 'for': item['for'], 'question': item['request'],
            'asked_at': item['asked_at']}


def run_names(runs: str) -> list[str]:
    try:
        names = [name for name in os.listdir(runs) if os.path.isdir(os.path.join(runs, name)) and not name.startswith('.')]
    except OSError:
        return []
    return names


def list_json(runs: str, root: str | None) -> dict:
    rows = []
    for name in run_names(runs):
        facts = run_state(os.path.join(runs, name))
        rows.append({'name': name, 'state': facts['state'], 'verdict': facts['verdict'], 'started': facts['started'],
                     'ended': facts['ended'], 'waiting_for': facts['waiting_for'], 'open_requests': len(facts['open'])})
    rows.sort(key=lambda row: (row['started'] or '', row['name']), reverse=True)
    return {'project': root, 'page': viewfile.address(root), 'runs_folder': runs, 'runs': rows}


def run_json(runs: str, name: str, root: str | None) -> dict | None:
    folder = os.path.join(runs, name)
    if not os.path.isdir(folder):
        return None
    facts = run_state(folder)
    saved, result = facts['saved'] or {}, facts['result'] or {}
    try:
        plan = parse_nodes(_text(os.path.join(folder, 'plan.md')) or '', strict=False)
    except Exception:
        plan = []
    progress = saved.get('nodes') or {}
    final = result.get('nodes') or {}
    asked = {}
    for item in facts['open']:
        asked.setdefault(item['node'], item['for'])
    nodes = []
    for node in plan:
        base = progress.get(node['id']) or {}
        word = final.get(node['id']) or {'pending': 'waiting'}.get(base.get('status'), base.get('status')) or 'waiting'
        if node['id'] in asked and word == 'running':
            word = f"waiting for {'human' if asked[node['id']] == 'person' else 'coordinator'}"
        nodes.append({'id': node['id'], 'kind': node.get('kind') or node.get('role'), 'role': node.get('role') or None,
                      'label': node.get('label'), 'state': word, 'depends_on': node.get('deps', []),
                      'workers': len(base.get('workers') or [])})
    cost = saved.get('cost') or result.get('cost') or costs.run_cost(saved.get('records') or [], {})
    return {'name': name, 'state': facts['state'], 'verdict': facts['verdict'], 'started': facts['started'],
            'ended': facts['ended'], 'folder': folder, 'plan': os.path.join(folder, 'plan.md'), 'repo': saved.get('repo'),
            'branch': saved.get('branch'), 'runner': _text(os.path.join(folder, 'runner')) or saved.get('runner'),
            'page': viewfile.address(root), 'waiting_for': facts['waiting_for'],
            'reviews': list(saved.get('verdicts') or result.get('review_verdicts') or []), 'cost': cost, 'nodes': nodes,
            'requests': [request_json(item) for item in facts['open']]}


def show_list(data: dict) -> None:
    print(f"project: {data['project'] or '(none; runs folder ' + data['runs_folder'] + ')'}")
    print(f"page: {data['page'] or 'not serving (pangoloop view)'}")
    if not data['runs']:
        print('(no runs)')
        return
    width = max(len(row['name']) for row in data['runs'])
    for row in data['runs']:
        extra = f"  waiting for the {row['waiting_for']}" if row['waiting_for'] else ''
        print(f"{row['name']:<{width}}  {row['state']:<13} started {row['started'] or '-':<21} ended {row['ended'] or '-'}{extra}")


def show_run(data: dict) -> None:
    print(f"run {data['name']}: {data['state']}" + (f" (verdict {data['verdict']})" if data['verdict'] else ''))
    print(f"started {data['started'] or '-'}, ended {data['ended'] or '-'}; runner {data['runner'] or '-'}; folder {data['folder']}")
    if data['page']:
        print(f"page: {data['page']}?run={data['name']}")
    for node in data['nodes']:
        who = node['role'] or '-'
        print(f"  {node['id']:<18} {node['state']:<22} {node['kind'] or '-':<11} {who}")
    cost = data['cost'] or {}
    usd = cost.get('usd')
    print('cost so far: ' + ('unknown' if usd is None else f"${usd:.4f}" + (' (partial)' if cost.get('partial') else '')))
    if data['reviews']:
        print('reviews: ' + ', '.join(data['reviews']))
    for item in data['requests']:
        first = (item['question'].strip().splitlines() or [''])[0]
        print(f"open request {item['id']} for the {item['for']} (node {item['node']}): {first}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog='pangoloop status', description='The project\'s runs, or one run in detail.',
                                     epilog='example:\n  pangoloop status --json\n  pangoloop status plan-20261120-101500 --json',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', nargs='?', help='a run name (or the path of a run folder); omitted: every run of the project')
    parser.add_argument('--json', action='store_true', help='one JSON object; the fields are written in docs/SETUP.md')
    args = parser.parse_args(argv)
    runs = paths.runs_folder()
    root = os.environ.get('PANGOLOOP_PROJECT') or None
    if args.run is None:
        data = list_json(runs, root)
        print(json.dumps(data, indent=1)) if args.json else show_list(data)
        return 0
    if os.sep in args.run:
        runs, name = os.path.split(os.path.abspath(args.run).rstrip(os.sep))
    else:
        name = args.run
    data = run_json(runs, name, root)
    if data is None:
        print(f'pangoloop: no run {name} in {runs}', file=sys.stderr)
        return 2
    print(json.dumps(data, indent=1)) if args.json else show_run(data)
    return 0
