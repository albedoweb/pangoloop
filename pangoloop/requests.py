"""The requests a node of a run makes, to the coordinator or to the person: list them, wait for one, answer one, pass
one on.

    python3 -m pangoloop requests list <run>
    python3 -m pangoloop requests wait <run> [--for coordinator|person|any]
    python3 -m pangoloop requests answer <run> <request id> --text "..." [--by person]
    python3 -m pangoloop requests escalate <run> <request id> [--note TEXT]
    python3 -m pangoloop requests rulings <run>
    python3 -m pangoloop requests rule <run> --text TEXT | --file FILE [--by person]

A node asks with a last line `NEEDS COORDINATOR: ...` (a fact, a clarification, an ordinary operation its session may not
do: the coordinator, the model that wrote the plan, answers) or `NEEDS HUMAN: ...` (a command the plan marks as the
person's, anything on a live system, anything that cannot be undone: only the person answers). The request file says
which in its field `for`.

Files, as the engine writes and reads them: <run>/human/<node>-<k>.json is a
request, <run>/human/<node>-<k>.answer.txt its answer. A request is open while it has no non-blank answer file and
its state is not a final one the engine recorded; the run is over when <run>/finished_at or <run>/result.json exists.
`<run>` is a run name (looked up in $PANGOLOOP_RUNS, else ./.pangoloop/runs) or the path of a run folder.
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

from pangoloop import paths, resume, rulings
from pangoloop.plan import parse_nodes, worker_kind

ANSWER_LIMIT = 20000   # as the viewer
FOR_CHOICES = ('coordinator', 'person', 'any')
BY_CHOICES = ('coordinator', 'person')
ID = re.compile(r'^[^/\x00]+-\d+$')


def runs_folder() -> str:
    return paths.runs_folder()   # the default shared with the engine and the viewer


def run_folder(run: str, runs: str | None = None) -> str:
    """The folder of a run named `run`, or `run` itself when it is a folder path."""
    if os.sep in run or run in ('.', '..'):
        return os.path.abspath(run)
    return os.path.join(runs or runs_folder(), run)


def run_is_over(folder: str) -> bool:
    return os.path.exists(f'{folder}/finished_at') or os.path.exists(f'{folder}/result.json')


def _read(path: str, limit: int | None = None) -> str | None:
    try:
        with open(path, errors='replace') as handle:
            return handle.read(limit) if limit else handle.read()
    except OSError:
        return None


def stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def request_for(info: dict) -> str:
    """Whom a request is for. A file with no (or an odd) `for`, written before the field existed, is for the person."""
    return 'coordinator' if info.get('for') == 'coordinator' else 'person'


def load_requests(folder: str) -> list[dict]:
    """Every request of the run, oldest first: id, node, k, worker, worker_kind (node, repair, rebase-repair, review,
    acceptance-repair; None for a gate), asked_at, answered_at, request, answer_tail, for
    ('person' or 'coordinator'), answered_by, escalated_at, escalation_note, state (open, answered, or what the engine
    recorded, e.g. 'no answer') and the answer text when there is one."""
    found = []
    try:
        nodes = parse_nodes(_read(f'{folder}/plan.md') or '', strict=False)
    except Exception:   # a plan the parser cannot read only costs the worker kind of an old request file
        nodes = []
    for path in sorted(glob.glob(f'{folder}/human/*.json')):
        match = re.match(r'^(.+)-(\d+)\.json$', os.path.basename(path))
        text = _read(path)
        if not match or text is None:
            continue
        try:
            info = json.loads(text)
        except ValueError:
            continue
        if not isinstance(info, dict):
            continue
        stem = os.path.basename(path)[:-5]
        answer = _read(f'{folder}/human/{stem}.answer.txt', ANSWER_LIMIT * 2)
        answered = answer is not None and answer.strip() != ''
        recorded = str(info.get('state') or 'open')
        worker = info.get('worker') if isinstance(info.get('worker'), str) and info.get('worker') else None
        found.append({'id': stem, 'node': str(info.get('node') or match.group(1)), 'k': int(match.group(2)),
                      'worker': worker, 'worker_kind': info.get('worker_kind') or worker_kind(worker, nodes),
                      'asked_at': str(info.get('asked_at') or ''),
                      'answered_at': info.get('answered_at'), 'request': str(info.get('request') or ''),
                      'answer_tail': str(info.get('answer_tail') or ''), 'for': request_for(info),
                      'answered_by': info.get('answered_by'), 'escalated_at': info.get('escalated_at'),
                      'escalation_note': info.get('escalation_note'),
                      'state': 'answered' if answered else ('open' if recorded in ('open', 'answered') else recorded),
                      'answer': answer if answered else None})
    found.sort(key=lambda item: (item['asked_at'], item['k']))
    return found


def open_requests(folder: str, for_: str = 'any') -> list[dict]:
    """The requests that wait for an answer; with `for_` 'coordinator' or 'person' only those for that one."""
    return [item for item in load_requests(folder)
            if item['state'] == 'open' and for_ in ('any', item['for'])]


def write_request(path: str, info: dict) -> None:
    """Rewrites a request file whole (a temporary file and os.replace), as the engine does."""
    temp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    with open(temp, 'w') as handle:
        json.dump(info, handle, indent=1)
    os.replace(temp, path)


# ---------------------------------------------------------------- list

def request_json(item: dict) -> dict:
    """One request as the `--json` forms print it (a contract, written in docs/SETUP.md): id, node, for ('person' or
    'coordinator'), question (the node's words), asked_at (UTC time, '' when unknown), and state (open, answered, or what the
    engine recorded), worker, worker_kind, answered_at, answered_by."""
    return {'id': item['id'], 'node': item['node'], 'for': item['for'], 'question': item['request'],
            'asked_at': item['asked_at'], 'state': item['state'], 'worker': item['worker'],
            'worker_kind': item['worker_kind'], 'answered_at': item['answered_at'], 'answered_by': item['answered_by']}


def cmd_list(folder: str, out=None, as_json: bool = False) -> int:
    out = out or sys.stdout
    items = load_requests(folder)
    if as_json:   # one JSON object per line, oldest first; nothing at all when there are no requests
        for item in items:
            print(json.dumps(request_json(item), ensure_ascii=False), file=out)
        return 0
    if not items:
        print('(no requests)', file=out)
        return 0
    width = max(len(item['id']) for item in items)
    for item in items:
        first = (item['request'].strip().splitlines() or [''])[0]
        who = f"{item['worker'] or '-'}" + (f" ({item['worker_kind']})" if item['worker_kind'] else '')
        print(f"{item['id']:<{width}}  {item['node']:<16} {item['for']:<12} {item['state']:<10} "
              f"{item['asked_at']:<21} {who:<28} {first}", file=out)
    return 0


# ---------------------------------------------------------------- wait

def format_request(item: dict) -> str:
    return (f"REQUEST {item['id']}.json node={item['node']} worker={item['worker']} asked_at={item['asked_at']} "
            f"for={item['for']} worker_kind={item['worker_kind']}\n"
            f"--- request ---\n{item['request']}\n--- end of the node's message ---\n{item['answer_tail']}")


def engine_alive(run: str, folder: str | None = None) -> bool:
    """Whether the run's engine is still a process. The run's own lock file (pid and start time, pangoloop/resume.py) says
    so when there is one, for this project's run and no other; only a run that has no lock is looked for by the command
    lines of processes (a worker of it, or its engine), which is a search over the whole machine."""
    held = resume.read_json(os.path.join(folder, resume.LOCK_FILE)) if folder else None
    if isinstance(held, dict) and held.get('pid'):
        return resume.process_alive(held.get('pid'), held.get('process_started'))
    name = re.escape(os.path.basename(os.path.normpath(run)))
    for pattern in (f'runs/{name}/', f'pangoloop run .* {name}( |$)', f'script_graph.py .* {name}( |$)'):
        if subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True).stdout.strip():
            return True
    return False


def cmd_wait(folder: str, run: str, poll: float = 2.0, quiet_seconds: float = 60.0, out=None, for_: str = 'any',
             as_json: bool = False) -> int:
    """0 and the request printed when one is open (and for `for_`: 'coordinator', 'person' or 'any'); 3 and `RUN OVER`
    when the run has ended.

    The end is told from the folder (finished_at or result.json, as the viewer does). Only a run that vanished without
    writing either (killed hard) cannot be told that way: then, after `quiet_seconds` with no process of the run,
    it counts as over too (0 turns the check off).

    With `as_json` the one line printed is a JSON object: the open request (request_json), or, when the run is over,
    {"run_over": true, "run": <name>, "verdict": <the engine's word or null>} (exit status 3 as before)."""
    out = out or sys.stdout
    quiet_since = None
    while True:
        items = open_requests(folder, for_)
        if items:
            print(json.dumps(request_json(items[0]), ensure_ascii=False) if as_json else format_request(items[0]),
                  file=out, flush=True)
            return 0
        if run_is_over(folder):
            result = _read(f'{folder}/result.json')
            try:
                verdict = json.loads(result).get('verdict') if result else None
            except ValueError:
                verdict = None
            if as_json:
                print(json.dumps({'run_over': True, 'run': run, 'verdict': verdict}), file=out, flush=True)
            else:
                print(f'RUN OVER {run}' + (f': {verdict}' if verdict else ''), file=out, flush=True)
            return 3
        if quiet_seconds and os.path.isdir(folder):
            if engine_alive(run, folder):
                quiet_since = None
            else:
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since >= quiet_seconds:
                    if as_json:
                        print(json.dumps({'run_over': True, 'run': run, 'verdict': None}), file=out, flush=True)
                    else:
                        print(f'RUN OVER {run}: no finished_at or result.json, and no process of the run for '
                              f'{int(quiet_seconds)} seconds', file=out, flush=True)
                    return 3
        time.sleep(poll)


# ---------------------------------------------------------------- answer

def cmd_answer(folder: str, request_id: str, text: str | None, abort: bool = False, by: str = 'coordinator',
               err=None) -> int:
    """Writes human/<id>.answer.txt for an open request and records who answered (`answered_by`: `by`, 'coordinator'
    from the command line unless it says `--by person`) in the request file. 0 written; 1 refused (not open, answered,
    run over, bad text, or a request for the person answered by anyone but the person); 2 no such request. The file appears whole or not at all (a hard link of a finished temporary
    file), and an existing answer is never overwritten."""
    err = err or sys.stderr
    if not ID.match(request_id) or request_id.startswith('.'):
        print(f'no such request: {request_id!r}', file=err)
        return 2
    hdir = f'{folder}/human'
    raw = _read(f'{hdir}/{request_id}.json')
    try:
        info = json.loads(raw) if raw is not None else None
    except ValueError:
        info = None
    if not isinstance(info, dict):
        print(f'no such request: {request_id} in {folder}', file=err)
        return 2
    if run_is_over(folder):
        print('refused: the run is over', file=err)
        return 1
    if info.get('state') != 'open':
        print(f"refused: the request is not open (state: {info.get('state')})", file=err)
        return 1
    if os.path.exists(f'{hdir}/{request_id}.answer.txt'):
        print('refused: the request already has an answer file', file=err)
        return 1
    if request_for(info) == 'person' and by != 'person':
        # A convention against mistakes, not access control: anyone can type --by person (or use the page).
        print('refused: this request is for the person: a coordinator escalates or leaves it '
              '(only the person answers it; with --by person you say that you are the person)', file=err)
        return 1
    if abort and text is None:
        text = 'ABORT\n'
    if abort and text.split('\n', 1)[0].rstrip('\r') != 'ABORT':
        text = 'ABORT\n' + text
    if not isinstance(text, str) or not text.strip():
        print('refused: the answer is empty', file=err)
        return 1
    if len(text) > ANSWER_LIMIT:
        print(f'refused: the answer is longer than {ANSWER_LIMIT} characters', file=err)
        return 1
    temp = f'{hdir}/.{request_id}.{os.getpid()}.{threading.get_ident()}.tmp'
    request_path = f'{hdir}/{request_id}.json'
    previous = info.get('answered_by')
    # Signed before the answer appears, so that the engine, which reads the file when it sees the answer, finds it.
    write_request(request_path, dict(info, answered_by=by))
    try:
        with open(temp, 'w') as handle:
            handle.write(text)
        os.link(temp, f'{hdir}/{request_id}.answer.txt')   # fails when an answer appeared meanwhile
    except OSError as error:
        _restore_signature(request_path, previous)
        if isinstance(error, FileExistsError):
            print('refused: the request was answered meanwhile', file=err)
        else:
            print(f'cannot write the answer: {error}', file=err)
        return 1
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    print(f'answered {request_id}' + (' (abort)' if abort else ''))
    return 0


def _restore_signature(path: str, previous) -> None:
    """An answer that lost the race must not leave its signature on the request."""
    raw = _read(path)
    try:
        info = json.loads(raw) if raw else None
    except ValueError:
        return
    if isinstance(info, dict):
        if previous is None:
            info.pop('answered_by', None)
        else:
            info['answered_by'] = previous
        write_request(path, info)


# ---------------------------------------------------------------- escalate

def cmd_escalate(folder: str, request_id: str, note: str | None = None, err=None, out=None) -> int:
    """Turns an open request for the coordinator into one for the person: `for` becomes 'person', `escalated_at` and
    the note are recorded. The engine notices on its next look at the file and calls the person as if the node had asked
    the person. 0 done; 1 refused (the run is over, the request is not open, answered, or already for the person); 2 no
    such request."""
    err, out = err or sys.stderr, out or sys.stdout
    if not ID.match(request_id) or request_id.startswith('.'):
        print(f'no such request: {request_id!r}', file=err)
        return 2
    hdir = f'{folder}/human'
    path = f'{hdir}/{request_id}.json'
    raw = _read(path)
    try:
        info = json.loads(raw) if raw is not None else None
    except ValueError:
        info = None
    if not isinstance(info, dict):
        print(f'no such request: {request_id} in {folder}', file=err)
        return 2
    if run_is_over(folder):
        print('refused: the run is over', file=err)
        return 1
    if info.get('state') != 'open' or os.path.exists(f'{hdir}/{request_id}.answer.txt'):
        print(f"refused: the request is not open (state: {info.get('state')})", file=err)
        return 1
    if request_for(info) != 'coordinator':
        print('refused: the request is already for the person', file=err)
        return 1
    info.update({'for': 'person', 'escalated_at': stamp()})
    if note:
        info['escalation_note'] = note
    write_request(path, info)
    print(f'escalated {request_id} to the person', file=out)
    return 0


# ---------------------------------------------------------------- rulings

def cmd_rulings(folder: str, out=None) -> int:
    """Prints the run's rulings (rulings.md, oldest first): what the coordinator or the person decided while the run
    went on. A run recorded before rulings existed is read from its request files instead."""
    out = out or sys.stdout
    items = rulings.load(folder) or rulings.from_requests(folder)
    if not items:
        print('(no rulings)', file=out)
        return 0
    print(''.join(rulings.render(item) for item in items), end='', file=out)
    return 0


def cmd_rule(folder: str, text: str | None, by: str = 'coordinator', err=None, out=None) -> int:
    """Appends a ruling that answers no question (id `ruling-<n>`) to <run>/rulings.md. Running nodes do not see it until
    their next prompt. 0 added; 1 refused (the run is over, an empty text, too long)."""
    err, out = err or sys.stderr, out or sys.stdout
    if run_is_over(folder):
        print('refused: the run is over', file=err)
        return 1
    if not isinstance(text, str) or not text.strip():
        print('refused: the ruling is empty', file=err)
        return 1
    if len(text) > ANSWER_LIMIT:
        print(f'refused: the ruling is longer than {ANSWER_LIMIT} characters', file=err)
        return 1
    ident = rulings.next_manual_id(folder)
    rulings.append(folder, {'id': ident, 'node': None, 'worker': None, 'by': by, 'at': stamp(), 'question': '',
                            'answer': text})
    print(f'added {ident}', file=out)
    return 0


# ---------------------------------------------------------------- command line

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='pangoloop requests', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='The requests a node of a run makes, to the coordinator or to the person: list them, wait for one,\n'
                    'answer one, pass one on to the person.\n'
                    'A run is a name (looked up in $PANGOLOOP_RUNS, else the project\'s runs, else ./.pangoloop/runs) or a folder path.',
        epilog='example, the coordinator\'s loop:\n  python3 -m pangoloop requests wait my-run --for coordinator\n'
               '  python3 -m pangoloop requests answer my-run a-1 --text "use the work account"\n'
               '  python3 -m pangoloop requests escalate my-run a-1 --note "needs the person\'s credentials"\n'
               '  python3 -m pangoloop requests rulings my-run\n'
               '  python3 -m pangoloop requests rule my-run --text "the config is /etc/app.conf, in every node"\n\n'
               'exit codes: wait 0 = a request is open and printed, 3 = RUN OVER; answer and escalate 0 = done, 1 = refused, '
               '2 = no such request')
    sub = parser.add_subparsers(dest='action', required=True)
    for name, help_text in (('list', 'every request of the run: id, node, for (person or coordinator), state, asked at, first line'),
                            ('wait', 'wait until a request is open (print it, exit 0) or the run is over (RUN OVER, exit 3)'),
                            ('answer', 'answer an open request with --text, --file or --abort'),
                            ('escalate', 'turn an open request for the coordinator into one for the person'),
                            ('rulings', 'print the rulings of the run: every answer to a request, and the rulings added '
                                        'with `rule`, oldest first (the file is <run>/rulings.md)'),
                            ('rule', 'add a ruling that answers no question, with --text or --file; the nodes that are '
                                     'running do not see it until their next prompt (every prompt the engine builds '
                                     'afterwards carries all rulings)')):
        item = sub.add_parser(name, help=help_text, description=help_text)
        item.add_argument('run')
        item.add_argument('--runs', help='the folder of runs (default: $PANGOLOOP_RUNS, else ./.pangoloop/runs)')
        if name in ('list', 'wait'):
            item.add_argument('--json', action='store_true', dest='as_json',
                              help='print JSON: one object per request (list: one per line; wait: the one open request, or '
                                   '{"run_over": true, ...}); the fields are in docs/SETUP.md')
        if name == 'wait':
            item.add_argument('--for', dest='for_', choices=FOR_CHOICES, default='any',
                              help='which requests to wait for: coordinator, person or any (default any)')
            item.add_argument('--poll', type=float, default=2.0, help='seconds between looks (default 2)')
            item.add_argument('--quiet-seconds', type=float, default=60.0,
                              help='for a run that left no finished_at or result.json: call it over after this many seconds '
                                   'without a process of it (default 60; 0 = never)')
        if name == 'escalate':
            item.add_argument('request', help='the request id, e.g. a-1 (the file name without .json)')
            item.add_argument('--note', help='why: recorded with the request and shown to the person')
        if name == 'rule':
            item.add_argument('--by', choices=BY_CHOICES, default='coordinator', help='who rules (default: coordinator)')
            group = item.add_mutually_exclusive_group(required=True)
            group.add_argument('--text', help='the ruling')
            group.add_argument('--file', help='read the ruling from this file')
        if name == 'answer':
            item.add_argument('request', help='the request id, e.g. a-1 (the file name without .json)')
            item.add_argument('--by', choices=BY_CHOICES, default=None,
                              help='who answers, recorded as answered_by. Default: coordinator, which is refused for a '
                                   'request that is for the person; --by person must be given explicitly')
            group = item.add_mutually_exclusive_group(required=True)
            group.add_argument('--text', help='the answer')
            group.add_argument('--file', help='read the answer from this file')
            group.add_argument('--abort', action='store_true', help='answer ABORT: ends the run')
            item.add_argument('--reason', help='with --abort: a line of explanation after ABORT')
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    folder = run_folder(args.run, args.runs)
    if not os.path.isdir(folder):
        print(f'no such run folder: {folder}', file=sys.stderr)
        return 2
    if args.action == 'list':
        return cmd_list(folder, as_json=args.as_json)
    if args.action == 'rulings':
        return cmd_rulings(folder)
    if args.action == 'rule':
        text = args.text
        if args.file:
            try:
                text = open(args.file).read()
            except OSError as error:
                print(f'cannot read {args.file}: {error}', file=sys.stderr)
                return 2
        return cmd_rule(folder, text, args.by)
    if args.action == 'wait':
        return cmd_wait(folder, args.run, args.poll, args.quiet_seconds, for_=args.for_, as_json=args.as_json)
    if args.action == 'escalate':
        return cmd_escalate(folder, args.request, args.note)
    text = args.text
    if args.file:
        try:
            text = open(args.file).read()
        except OSError as error:
            print(f'cannot read {args.file}: {error}', file=sys.stderr)
            return 2
    if args.abort:
        text = 'ABORT\n' + (args.reason + '\n' if args.reason else '')
    return cmd_answer(folder, args.request, text, abort=args.abort, by=args.by or 'coordinator')
