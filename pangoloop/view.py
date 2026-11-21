"""A live, read-only view of one script_graph.py run, served on 127.0.0.1.

    python3 -m pangoloop view [<run name>] [--port 5420]

(Moved from run_view.py into the package on November 18, 2026. The runs folder is $RUN_VIEW_RUNS, else $PANGOLOOP_RUNS,
else <current folder>/.pangoloop/runs, the same default as the engine's (pangoloop/paths.py). The plan format, the
routes and the patterns are read from pangoloop/plan.py, shared with the engine.)

It READS <runs folder>/<run name>/ (and $RUN_VIEW_REPORTS/<TASK>/ for the grade, when set). It starts no session and runs no
command. The one thing it writes (added 2026-11-17, SG_HUMAN runs): a person's answer to an open request of a node,
as human/<node>-<k>.answer.txt, through POST /api/run/<name>/human/<node>-<k> (same-origin only, only for a request
that is open, at most 20,000 characters; the text is stored, never executed). When a request opens the server also
raises one desktop notification whose body ends with the page's address (terminal-notifier if installed, so a click
opens it; else macOS osascript, whose click does not). RUN_VIEW_NOTIFY=0 or SG_HUMAN_NOTIFY=0 turns it off. Everything the page shows comes from /api/run/<name>, built fresh from the files on each request:

  plan.md                      nodes: label, category, role, depends_on, TASK, VERIFY (the commands)
  started_at / finished_at     the run's clock (UTC); no finished_at = still running
  result.json                  written when the run ends: verdict, node statuses, worker records
  workers/<nn>-<node>/         one Hermes session: prompt.txt (its mtime = start), events.jsonl (the stream),
                               answer.md (written when it ended), stderr.log
      <nn>-<node>-fixN / -rebaseN   repair workers, attached to <node>
  acceptance-<node>-<n>.json   one round of the scripted acceptance node (commands with exit and seconds)
  wt/<node>/                   a build node's own copy; present until its work is integrated
  $RUN_VIEW_REPORTS/<TASK>/<run>/grade/grade.json and $RUN_VIEW_REPORTS/<TASK>/results.jsonl   after grading
  $RUN_VIEW_REPORTS/<TASK>/<run>/regrade-*/regrade.json (or only REGRADE.md)   a later regrade by hand
The page keeps three facts apart, each with its own label: `run` (the engine's verdict), `grade` (the grader's) and
`regrade`; /api/runs and /api/run/<name> carry them as `facts`.

The worker's tail comes from /api/run/<name>/worker/<tag> (only for the node whose panel is open).
human/<node>-<k>.json        (SG_HUMAN runs) a request a node made to the person; <node>-<k>.answer.txt is the answer
Large events.jsonl files are summarised incrementally (cached per file; only appended bytes are read again) and the
live tail reads only the end of the file.
"""
import calendar
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pangoloop import config, paths, rulings
from pangoloop.plan import (ASK_LINE, DEFAULT_MAX_FIX_ROUNDS, FINDING_LINE, VERDICT, kind_of, parse_nodes, person_commands,
                            worker_kind)
from pangoloop.runners import LEGACY_RUNNER, get_runner

# The viewer's own routes (for a node that has no worker yet) come from the same configuration as the engine's; a run's
# stream is read by the runner that run recorded (`runner_of`), never by anything here.
ROUTES, DEFAULT_ROUTE = config.load().routes_table()

# The runs folder: the same default as the engine's. The grading reports are the using project's (not part of the
# package): a folder `<task>/<run>/grade/grade.json` and `<task>/results.jsonl`, named by $RUN_VIEW_REPORTS. With the
# variable unset REPORTS is empty and the page's facts about a grade are absent (no default folder is guessed).
RUNS = paths.runs_folder(viewer=True)
REPORTS = os.environ.get('RUN_VIEW_REPORTS', '')
ANSWER_LIMIT = 20000
NOTIFY = os.environ.get('RUN_VIEW_NOTIFY', '1') != '0' and os.environ.get('SG_HUMAN_NOTIFY', '1') != '0'
PORT = 5420   # set by main(); the notification names this address
LOCK = threading.Lock()


# ---------------------------------------------------------------- small helpers

def rd(path, limit=None):
    """Text of a file, or None when it is missing or cannot be read (locked folders count as 'not available yet')."""
    try:
        with open(path, errors='replace') as handle:
            return handle.read(limit) if limit else handle.read()
    except OSError:
        return None


def rd_json(path):
    text = rd(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def iso_epoch(text):
    try:
        return calendar.timegm(time.strptime(text.strip(), '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, AttributeError):
        return None


def listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def short(value, limit):
    text = ' '.join(str(value if value is not None else '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def readable_dir(path):
    return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


# ---------------------------------------------------------------- events.jsonl summaries (incremental, cached)

class Summary:
    def __init__(self, inode, runner):
        self.inode = inode
        self.runner = runner
        self.offset = 0
        self.events = 0
        self.texts = 0
        self.tools = collections.Counter()
        self.tool_errors = 0
        self.shot_runs = 0
        self.shots = 0
        self.model = None
        self.first_ts = None
        self.result = None   # the final result record: {'exit_code', 'tokens'}

    def feed(self, line):
        if not line.strip():
            return
        self.events += 1
        for record in self.runner.parse_events(line):   # the runner knows its stream; the page sees neutral records
            kind = record['kind']
            if kind == 'text':
                self.texts += 1
            elif kind == 'tool_result':
                if record.get('error'):
                    self.tool_errors += 1
            elif kind == 'tool':
                self.tools[str(record.get('tool'))] += 1
                command = record.get('command')
                if isinstance(command, str) and 'shot.mjs' in command:
                    self.shot_runs += 1
                    self.shots += max(1, command.count('shot:'))
            elif kind == 'start' and record.get('init', True):
                self.model = record.get('model') or self.model
                ts = record.get('ts')
                self.first_ts = ts / 1000 if isinstance(ts, (int, float)) else self.first_ts
            elif kind == 'result':
                self.result = {'exit_code': record.get('exit_code'), 'tokens': record.get('tokens')}


CACHE: dict = {}


def summarize(path, runner):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    with LOCK:
        summary = CACHE.get(path)
        if summary is None or summary.inode != stat.st_ino or stat.st_size < summary.offset:
            summary = Summary(stat.st_ino, runner)
        if stat.st_size > summary.offset:
            try:
                with open(path, 'rb') as handle:
                    handle.seek(summary.offset)
                    chunk = handle.read(stat.st_size - summary.offset)
            except OSError:
                return summary
            end = chunk.rfind(b'\n')
            if end >= 0:   # only whole lines; a half-written one is read next time
                for line in chunk[:end].split(b'\n'):
                    summary.feed(line)
                summary.offset += end + 1
        CACHE[path] = summary
        return summary


ITEM_KINDS = {'text': 'says', 'tool': 'call', 'tool_result': 'result', 'start': 'start', 'result': 'end'}


def tail_events(path, runner, count=40):
    """The last `count` events in plain words, reading only the end of the file; the runner turns each line into a
    neutral record and this only lays them out."""
    try:
        size = os.stat(path).st_size
        handle = open(path, 'rb')
    except OSError:
        return []
    window = 256 * 1024
    with handle:
        while True:
            start = max(0, size - window)
            handle.seek(start)
            data = handle.read(size - start)
            lines = data.split(b'\n')
            if not data.endswith(b'\n'):
                lines = lines[:-1]            # half-written last line
            if start > 0:
                lines = lines[1:]             # cut in the middle of a line
            items = []
            for line in lines:
                if not line.strip():
                    continue
                for record in runner.parse_events(line):
                    kind, ts = record['kind'], record.get('ts')
                    if kind == 'text':
                        if items and items[-1]['kind'] == 'says':
                            items[-1]['text'] += record['text']
                        else:
                            items.append({'kind': 'says', 'ts': ts, 'text': record['text']})
                    elif kind == 'tool':
                        items.append({'kind': 'call', 'ts': ts, 'name': record.get('tool'), 'text': record.get('input', '')})
                    elif kind == 'tool_result':
                        items.append({'kind': 'result', 'ts': ts, 'name': record.get('tool'),
                                      'error': bool(record.get('error')), 'ms': record.get('ms'),
                                      'text': record.get('text', '')})
                    elif kind in ('start', 'result'):
                        items.append({'kind': ITEM_KINDS[kind], 'ts': ts, 'text': record.get('text', '')})
            if len(items) > count or start == 0 or window >= 32 * 1024 * 1024:
                break
            window *= 4
    items = items[-count:]
    for item in items:
        if item['kind'] == 'says':
            item['text'] = short(item['text'], 400)
    return items


RUNNERS: dict = {}


def worker_runner(name, tag):
    """What reads a worker's events: the run's runner, except for a script node, whose engine-written stream is the
    `script` one."""
    folder = os.path.join(RUNS, name)
    _, nodes = load_plan(os.path.join(folder, 'plan.md'))
    node_id = re.sub(r'^\d+-', '', tag)
    if any(node['id'] == node_id and kind_of(node) == 'script' for node in nodes):
        return get_runner('script')
    return runner_of(folder)[1]


def runner_of(folder):
    """The runner a run folder says it used (the file `runner`, else `runner` of result.json), else LEGACY_RUNNER (the one every
    run before step 3 used). A name that is not a runner here also reads as that (the page then shows what it can)."""
    name = (rd(os.path.join(folder, 'runner')) or '').strip() or str((rd_json(os.path.join(folder, 'result.json')) or {}).get('runner') or '')
    name = name or LEGACY_RUNNER
    if name not in RUNNERS:
        try:
            RUNNERS[name] = get_runner(name)
        except SystemExit:
            RUNNERS[name] = get_runner(LEGACY_RUNNER)
    return name, RUNNERS[name]


# ---------------------------------------------------------------- plan

PLAN_CACHE: dict = {}


def load_plan(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None, []
    key = (stat.st_mtime, stat.st_size)
    cached = PLAN_CACHE.get(path)
    if cached and cached[0] == key:
        return cached[1], cached[2]
    text = rd(path) or ''
    nodes = parse_nodes(text, strict=False)
    PLAN_CACHE[path] = (key, text, nodes)
    return text, nodes


# ---------------------------------------------------------------- results.jsonl index (name -> row)

INDEX_CACHE: dict = {}


def results_index():
    index = {}
    for task in (listdir(REPORTS) if REPORTS else []):
        path = os.path.join(REPORTS, task, 'results.jsonl')
        key = mtime(path)
        if key is None:
            continue
        cached = INDEX_CACHE.get(path)
        if not cached or cached[0] != key:
            rows = []
            for line in (rd(path) or '').splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
            INDEX_CACHE[path] = cached = (key, rows)
        for row in cached[1]:
            if isinstance(row, dict) and row.get('name'):
                index[row['name']] = row
    return index


# ---------------------------------------------------------------- three facts about a run: run, grade, regrade
# `run` is the engine's verdict (what the nodes' colours mean); `grade` is what the grader said afterwards (results.jsonl,
# the count from grade/grade.json); `regrade` is a later correction by hand (regrade-*/regrade.json, or only REGRADE.md).
# A missing or unreadable reports folder is "no grade", never an error.

GRADE_CACHE: dict = {}


def run_word(state):
    text = str(state or '').replace('finished: ', '').replace('stopped_by_human', 'stopped by the human')
    return text.replace('no_answer_from_human', 'no answer from the human').replace('_', ' ')


def hidden_count(task, name):
    """'83/86' from the 'hidden tests' check of the grade record, or None."""
    if not REPORTS:
        return None
    path = os.path.join(REPORTS, str(task), name, 'grade', 'grade.json')
    key = mtime(path)
    if key is None:
        return None
    cached = GRADE_CACHE.get(path)
    if not cached or cached[0] != key:
        count = None
        data = rd_json(path)
        for check in (data.get('checks') or []) if isinstance(data, dict) else []:
            if isinstance(check, dict) and check.get('check') == 'hidden tests':
                found = re.search(r'(\d+) passed \((\d+)\)', str(check.get('detail') or ''))
                count = f'{found.group(1)}/{found.group(2)}' if found else None
        GRADE_CACHE[path] = cached = (key, count)
    return cached[1]


def grader_commands(task, name):
    """The counts the grader's own run of the project's commands printed, from grade.json: [{'label','passed','total'}].
    A check `command <label>` whose detail ends in `N passed (M)` has one (the last such pair: the tests, not the files)."""
    data = rd_json(os.path.join(REPORTS, str(task), name, 'grade', 'grade.json')) if REPORTS else None
    out = []
    for check in (data.get('checks') or []) if isinstance(data, dict) else []:
        label = str(check.get('check') or '') if isinstance(check, dict) else ''
        if label.startswith('command '):
            pairs = re.findall(r'(\d+) passed \((\d+)\)', str(check.get('detail') or ''))
            if pairs:
                out.append({'label': label[8:], 'passed': int(pairs[-1][0]), 'total': int(pairs[-1][1])})
    return out


def find_regrade(task, name):
    """The newest `regrade-*` folder (by name) of the run's report folder that has a record, as a dict; or None."""
    if not REPORTS:
        return None
    base = os.path.join(REPORTS, str(task), name)
    for folder in sorted((d for d in listdir(base) if d.startswith('regrade-')), reverse=True):
        path = os.path.join(base, folder)
        jpath, mpath = os.path.join(path, 'regrade.json'), os.path.join(path, 'REGRADE.md')
        if os.path.exists(jpath):
            data = rd_json(jpath)
            if isinstance(data, dict) and isinstance(data.get('passed'), bool):
                return {'status': 'passed' if data['passed'] else 'failed', 'tests': data.get('hidden_tests'),
                        'original': data.get('original'), 'date': data.get('date'), 'summary': data.get('summary'),
                        'note': data.get('note'), 'folder': folder}
            return {'status': 'unreadable', 'folder': folder}
        if os.path.exists(mpath):
            lines = [x.strip() for x in (rd(mpath, 4000) or '').splitlines() if x.strip() and not x.lstrip().startswith('#')]
            return {'status': 'note', 'summary': lines[0] if lines else None, 'note': 'REGRADE.md', 'folder': folder}
    return None


# --- the line of what the run's own acceptance counted. One small function per output pattern.

def parse_vitest(text):
    """vitest's summary: `Tests  2 failed | 1299 passed (1301)`. The last such line wins."""
    found = re.findall(r'^\s*Tests\s+(?:(\d+) failed \| )?(?:(\d+) passed)?.*?\((\d+)\)', text or '', re.M)
    if not found:
        return None
    failed, passed, _ = found[-1]
    return {'passed': int(passed or 0), 'failed': int(failed or 0)}


def parse_playwright(text):
    """Playwright's summary lines at the start of a line: `1 failed`, `1 flaky`, `5 did not run`, `12 passed (1.3m)`."""
    counts = {}
    for number, word in re.findall(r'^\s*(\d+) (passed|failed|flaky|skipped|did not run)\b', text or '', re.M):
        counts[word] = int(number)
    if not ({'passed', 'failed', 'flaky'} & set(counts)):
        return None
    return {'passed': counts.get('passed', 0), 'failed': counts.get('failed', 0), 'flaky': counts.get('flaky', 0)}


def parse_generic(text):
    """Any `N passed` / `N failed` in the text (the last of each)."""
    passed, failed = re.findall(r'(\d+) passed', text or ''), re.findall(r'(\d+) failed', text or '')
    if not passed and not failed:
        return None
    return {'passed': int(passed[-1]) if passed else 0, 'failed': int(failed[-1]) if failed else 0}


def command_label(command):
    """(label, kind) derived from the command: kind is 'runner' for test runners (always named), else 'check' (grouped)."""
    words = str(command or '').split()
    if not words:
        return '?', 'check'
    if 'playwright' in words:
        for i, word in enumerate(words):
            if word in ('-c', '--config') and i + 1 < len(words):
                parts = [p for p in os.path.basename(words[i + 1]).split('.')[:-1] if p not in ('playwright', 'config')]
                return (parts[-1] if parts else 'browser'), 'runner'
        paths = [w for w in words[words.index('test') + 1:] if not w.startswith('-')] if 'test' in words else []
        return (os.path.basename(paths[-1].rstrip('/')) if paths else 'browser'), 'runner'
    if (words[:2] == ['npm', 'test'] or words[:3] == ['npm', 'run', 'test'] or any(w in ('vitest', 'jest', 'pytest') for w in words[:3])
            or (words[0].startswith('python') and words[1:3] == ['-m', 'unittest'])):
        return 'tests', 'runner'
    if words[:2] == ['npm', 'run'] and len(words) > 2:
        return words[2], 'check'
    return os.path.basename(words[1] if len(words) > 1 and words[0] in ('node', 'python', 'python3', 'sh', 'bash') else words[0]), 'check'


def command_counts(command, tail):
    if not tail:
        return None
    tail = re.sub(r'\x1b\[[0-9;]*m', '', str(tail))
    if 'playwright' in str(command):
        return parse_playwright(tail) or parse_generic(tail)
    return parse_vitest(tail) or parse_generic(tail)


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def review_phrase(verdicts):
    verdicts = [str(v) for v in (verdicts or [])]
    if not verdicts:
        return None
    returns = sum(1 for v in verdicts if v != 'PASS')
    if not returns:
        return 'review: pass'
    noun = 'return' if returns == 1 else 'returns'
    last = verdicts[-1].lower().replace('_', ' ')
    return f'review: {returns} {noun}, then pass' if verdicts[-1] == 'PASS' else f'review: {returns} {noun}, last: {last}'


def acceptance_summary(folder, verdicts=None):
    """What the last acceptance round counted, from acceptance-<node>-<round>.json (the engine stores the output end only
    for failed commands, so a passing command has a name and a tick but, unless a record carries its output, no number).
    None when nothing is stored."""
    nodes: dict = {}
    for fname in listdir(folder):
        found = re.match(r'^acceptance-(.+)-(\d+)\.json$', fname)
        if found:
            nodes.setdefault(found.group(1), []).append((int(found.group(2)), fname))
    latest = None
    for node, items in nodes.items():
        stamp = max(mtime(os.path.join(folder, f)) or 0 for _, f in items)
        if latest is None or stamp >= latest[0]:
            latest = (stamp, node, sorted(items))
    segments, rounds = [], 0
    if latest:
        rounds = len(latest[2])
        data = rd_json(os.path.join(folder, latest[2][-1][1]))
        entry = data.get('entry') if isinstance(data, dict) else None
        commands = entry.get('commands') if isinstance(entry, dict) else None
        tails = {f[0]: f[2] for f in (data.get('failures') or []) if isinstance(f, (list, tuple)) and len(f) > 2} if isinstance(data, dict) else {}
        for c in commands or []:
            if isinstance(c, dict):
                label, kind = command_label(c.get('command'))
                counts = command_counts(c.get('command'), tails.get(c.get('command')) or c.get('output') or c.get('tail'))
                segments.append({'label': label, 'kind': kind, 'ok': c.get('exit') == 0, 'exit': c.get('exit'), 'counts': counts})
    parts, plain = [], 0
    for s in segments:   # the failed first
        if not s['ok']:
            c = s['counts']
            parts.append(f"{s['label']} ✗ {c['failed']} failed" + (f", {c['passed']} passed" if c.get('passed') else '') if c and c.get('failed')
                         else f"{s['label']} ✗ exit {s['exit']}")
    for s in segments:
        if not s['ok']:
            continue
        if s['kind'] == 'runner':
            c = s['counts']
            text = f"{s['label']} {c['passed']:,} ✓" if c and c.get('passed') else f"{s['label']} ✓"
            parts.append(text + (f", {c['flaky']} flaky" if c and c.get('flaky') else ''))
        else:
            plain += 1
    if plain:
        parts.append(f'{plain} checks ✓' if plain > 1 else '1 check ✓')
    if rounds > 1:
        parts.append(f'{ordinal(rounds)} round')
    review = review_phrase(verdicts)
    if review:
        parts.append(review)
    if not parts:
        return None
    return {'text': ' · '.join(parts), 'ok': all(s['ok'] for s in segments) if segments else None, 'rounds': rounds, 'review': review,
            'commands': [{'label': s['label'], 'ok': s['ok'], 'counts': s['counts']} for s in segments]}


def run_facts(name, task, row, state, folder=None, result=None):
    """The three facts, their one label and the overall tone ('ok', 'bad', 'run', 'warn'), and the tests line."""
    word = run_word(state)
    facts = {'run': word, 'grade': None, 'regrade': None, 'tests': None,
             'tone': 'ok' if word == 'verified' else 'run' if word.startswith(('running', 'waiting')) else 'bad'}
    parts = [f'run {word}']
    if row and 'code_grader_passed' in row:
        count = hidden_count(task, name) if task else None
        passed = bool(row['code_grader_passed'])
        failed_checks = [str(c) for c in row['failed_checks']] if isinstance(row.get('failed_checks'), list) else []
        own = grader_commands(task, name) if task else []
        facts['grade'] = {'result': 'passed' if passed else 'failed', 'tests': count, 'failed_checks': failed_checks, 'commands': own}
        parts.append(f"grader {facts['grade']['result']}" + (f' {count} hidden' if count else (f": {', '.join(failed_checks)}" if failed_checks else ''))
                     + (' (its own run: ' + ', '.join(f"{c['passed']:,} {c['label']}" for c in own) + ')' if own else ''))
        facts['tone'] = 'ok' if passed else 'bad'
    regrade = find_regrade(task, name) if task else None
    if regrade:
        facts['regrade'] = regrade
        if regrade['status'] in ('passed', 'failed'):
            parts.append('regraded ' + (f"{regrade['tests']} " if regrade.get('tests') else '') + regrade['status'])
            facts['tone'] = 'ok' if regrade['status'] == 'passed' else 'bad'
        else:
            parts.append('regrade record unreadable' if regrade['status'] == 'unreadable' else 'regraded: see REGRADE.md')
            facts['tone'] = 'warn'
    facts['label'] = ' · '.join(parts)
    facts['judged'] = bool(facts['grade'] or facts['regrade'])
    if folder:
        try:
            facts['tests'] = acceptance_summary(folder, result.get('review_verdicts') if isinstance(result, dict) else None)
        except Exception:   # a record in an odd shape never breaks the page
            facts['tests'] = None
    return facts


# ---------------------------------------------------------------- human requests (SG_HUMAN runs)

HUMAN_NAME = re.compile(r'^(.+)-(\d+)\.json$')


def human_requests(folder):
    """The requests nodes made to the person, oldest first: human/<node>-<k>.json, with the answer when its file exists.
    state: open (no answer file yet), answered, or what the script recorded ('no answer')."""
    hdir = os.path.join(folder, 'human')
    out = []
    try:
        nodes = parse_nodes(rd(os.path.join(folder, 'plan.md')) or '', strict=False)
    except Exception:
        nodes = []
    for entry in listdir(hdir):
        found = HUMAN_NAME.match(entry)
        info = rd_json(os.path.join(hdir, entry)) if found else None
        if not isinstance(info, dict):
            continue
        stem = entry[:-5]
        answer = rd(os.path.join(hdir, stem + '.answer.txt'), ANSWER_LIMIT * 2)
        answered = answer is not None and answer.strip() != ''
        recorded = str(info.get('state') or 'open')
        worker = info.get('worker') if isinstance(info.get('worker'), str) and info.get('worker') else None
        out.append({'name': stem, 'node': str(info.get('node') or found.group(1)), 'k': int(found.group(2)),
                    'worker': worker, 'worker_kind': info.get('worker_kind') or worker_kind(worker, nodes),
                    'asked_at': iso_epoch(info.get('asked_at')),
                    'answered_at': iso_epoch(info.get('answered_at')), 'request': str(info.get('request') or ''),
                    'answer_tail': str(info.get('answer_tail') or ''),
                    'for': 'coordinator' if info.get('for') == 'coordinator' else 'person',   # no field: for the person
                    'answered_by': info.get('answered_by'), 'escalated_at': iso_epoch(info.get('escalated_at')),
                    'escalation_note': info.get('escalation_note'),
                    'state': 'answered' if answered else ('open' if recorded in ('open', 'answered') else recorded),
                    'answer': answer if answered else None})
    out.sort(key=lambda item: (item['asked_at'] or 0, item['k']))
    return out


def run_rulings(folder):
    """The rulings of a run for the page, newest first (rulings.md; a run recorded before rulings existed is read from its
    request files). Each: id (the id of the request it answers, or ruling-<n>), node, worker, by, at, question, answer."""
    found = rulings.load(folder) or rulings.from_requests(folder)
    return [dict(item, at=iso_epoch(item['at'])) for item in reversed(found)]


def run_is_over(folder):
    return os.path.exists(os.path.join(folder, 'finished_at')) or os.path.exists(os.path.join(folder, 'result.json'))


def run_waits_for(folder):
    """'person' when a request for the person is open in a run that has not ended, else 'coordinator' when one for the
    coordinator is, else None."""
    if run_is_over(folder):
        return None
    open_here = [r['for'] for r in human_requests(folder) if r['state'] == 'open']
    return 'person' if 'person' in open_here else ('coordinator' if open_here else None)


# ---------------------------------------------------------------- run list

def list_runs():
    index = results_index()
    runs = []
    for name in listdir(RUNS):
        folder = os.path.join(RUNS, name)
        if not os.path.isdir(folder):
            continue
        try:
            folder_time = os.stat(folder).st_mtime
        except OSError:
            continue
        locked = not readable_dir(folder)
        result = None
        started = None if locked else iso_epoch(rd(os.path.join(folder, 'started_at')) or '')
        row = index.get(name)
        entry = {'name': name, 'started': started or folder_time, 'started_known': started is not None,
                 'locked': locked, 'task': (row or {}).get('task'), 'state': None}
        if locked:
            entry['state'] = 'locked'
        else:
            result = rd_json(os.path.join(folder, 'result.json'))
            if result:
                entry['state'] = str(result.get('verdict'))
            elif os.path.exists(os.path.join(folder, 'finished_at')):
                entry['state'] = 'finished'
            elif started is not None:
                waiting = run_waits_for(folder)
                entry['state'] = {'person': 'waiting for you', 'coordinator': 'waiting for the coordinator'}.get(waiting, 'running')
            else:
                entry['state'] = 'no data'
            first = (rd(os.path.join(folder, 'plan.md'), 400) or '').splitlines()[:1]
            found = re.search(r'task (\S+?)[,\s]', (first[0] + ' ') if first else '')
            entry['task'] = entry['task'] or (found.group(1) if found else None)
        if row and 'code_grader_passed' in row:
            entry['graded'] = 'passed' if row['code_grader_passed'] else 'failed'
        entry['facts'] = run_facts(name, entry['task'], row, entry['state'], None if locked else folder, result)
        runs.append(entry)
    runs.sort(key=lambda item: item['started'], reverse=True)
    return runs


def default_run():
    for entry in list_runs():
        if entry['started_known']:
            return entry['name']
    return None


# ---------------------------------------------------------------- one run

def route_role(category, kind=None):
    """The colour class of a worker: the part it played, from what the node is (its kind) and the route's category, never from
    its model. `script` and `reader` are what they say; a review (a node of kind review, or the route `review`) is the
    `reviewer`; the route `ui` is the `interface` model; every other worker (a build, a baseline, a repair, a category of the
    project's own) is the `coder`, so that an ordinary worker never reads as "other"."""
    if kind == 'script' or category in ('script', 'reader'):
        return 'script' if kind == 'script' or category == 'script' else 'reader'
    if kind == 'review' or category == 'review':
        return 'reviewer'
    return 'interface' if category == 'ui' else 'coder'


def route_model(category):
    return ROUTES.get(category, DEFAULT_ROUTE)[0]


def node_model(node):
    """The model a node is routed to; a script node is done by the engine and has none."""
    return 'script' if kind_of(node) == 'script' else route_model(node.get('route_category') or node['category'])


COMMAND_START = re.compile(r'^(?:[A-Z_]+=\S+\s+)*(npm|npx|node|pnpm|yarn|python3?|bash|sh|git|make|pytest|vitest|tsc|eslint|cargo|go|\./)\b')


def commands_of(verify, everything):
    """The backticked pieces of a VERIFY line. The script runs all of them in an acceptance node; in other nodes
    only the pieces that look like a command are shown (the line also quotes options, anchors and words)."""
    pieces = re.findall(r'`([^`]+)`', verify)
    return pieces if everything else [p for p in pieces if COMMAND_START.match(p)]


def build_run(name):
    folder = os.path.join(RUNS, name)
    if '/' in name or name.startswith('.') or not os.path.isdir(folder):
        return None
    now = time.time()
    index = results_index()
    row = index.get(name)
    out = {'name': name, 'now': now, 'results': row, 'task': (row or {}).get('task')}
    if not readable_dir(folder):
        out.update(readable=False, message='The run folder is locked (no read permission): not available.')
        return out
    out['readable'] = True
    started = iso_epoch(rd(os.path.join(folder, 'started_at')) or '')
    finished = iso_epoch(rd(os.path.join(folder, 'finished_at')) or '')
    result = rd_json(os.path.join(folder, 'result.json'))
    # a resumed run (pangoloop/resume.py): the segments of its state file, and the nodes carried over from an earlier one
    run_state = rd_json(os.path.join(folder, 'state.json'))
    segments = [s for s in ((run_state or {}).get('segments') or []) if isinstance(s, dict)]
    state_nodes = (run_state or {}).get('nodes') or {}
    resumed = len(segments) > 1
    plan_text, nodes = load_plan(os.path.join(folder, 'plan.md'))
    if not nodes:
        out.update(readable=False, message='No plan with nodes in this folder (not a script_graph run).')
        return out
    found = re.match(r'# Plan: task (\S+?)[,\s]', plan_text or '')
    out['task'] = out['task'] or (found.group(1) if found else None)
    over = bool(finished or result)
    records = {rec.get('worker'): rec for rec in (result or {}).get('workers', []) if isinstance(rec, dict)}
    cost_records = {rec.get('worker'): rec for rec in ((run_state or {}).get('records') or []) if isinstance(rec, dict)}
    cost_records.update(records)   # the cost of a worker, stored by the engine: from state.json while it runs
    node_ids = [node['id'] for node in nodes]
    by_id = {node['id']: node for node in nodes}
    requests = human_requests(folder)
    human_run = os.path.isdir(os.path.join(folder, 'human'))
    open_requests = [r for r in requests if r['state'] == 'open'] if not over else []

    runner_name, runner = runner_of(folder)
    out['runner'] = runner_name

    # ---- workers
    workers = []
    has_wt_dir = os.path.isdir(os.path.join(folder, 'wt'))
    for tag in listdir(os.path.join(folder, 'workers')):
        match = re.match(r'^(\d+)-(.+)$', tag)
        if not match:
            continue
        rest = match.group(2)
        kind, node_id = 'main', rest
        if rest not in by_id:
            sub = re.match(r'^(.*)-(fix|rebase)(\d+)$', rest)
            if sub and sub.group(1) in by_id:
                node_id, kind = sub.group(1), f'{sub.group(2)}{sub.group(3)}'
            else:
                continue
        wfolder = os.path.join(folder, 'workers', tag)
        events = os.path.join(wfolder, 'events.jsonl')
        summary = summarize(events, get_runner('script') if kind_of(by_id[node_id]) == 'script' else runner)
        record = records.get(tag) or {}
        start = record.get('started') or mtime(os.path.join(wfolder, 'prompt.txt')) or (summary.first_ts if summary else None)
        answer_time = mtime(os.path.join(wfolder, 'answer.md'))
        try:
            answer_size = os.stat(os.path.join(wfolder, 'answer.md')).st_size
        except OSError:
            answer_size = None
        last_event = mtime(events)
        end = None
        if record.get('seconds') is not None and start:
            end = start + record['seconds']
        elif answer_time:
            end = answer_time
        exit_code = record.get('exit')
        if exit_code is None and summary and summary.result:
            exit_code = summary.result.get('exit_code')
        if end is not None:
            ok = record['ok'] if 'ok' in record else (bool(answer_size) and str(exit_code) in ('0', 'None'))
            status = 'ok' if ok else 'failed'
        elif over or (resumed and int(match.group(1)) < (segments[-1].get('first_worker') or 0)):
            status = 'stopped'   # the engine of an earlier segment was killed or stopped while it ran
        else:
            status = 'running'
        model = (summary.model if summary and summary.model else None) or record.get('reported_model') or \
            record.get('model') or node_model(by_id[node_id])
        tokens = (summary.result or {}).get('tokens') if summary else None
        workers.append({
            'tag': tag, 'seq': int(match.group(1)), 'node': node_id, 'kind': kind, 'model': model,
            'role': route_role(record.get('category') or by_id[node_id].get('route_category') or by_id[node_id]['category'],
                               kind_of(by_id[node_id]) if kind == 'main' else None),
            'role_name': '' if (by_id[node_id].get('legacy') or kind != 'main') else (by_id[node_id].get('role') or ''),
            # what the legend and the timeline call the worker: the node's role, else its kind; a repair or rebase worker says so
            'role_label': (((by_id[node_id].get('role') or '') if not by_id[node_id].get('legacy') else '') or kind_of(by_id[node_id]))
                          if kind == 'main' else ('rebase' if kind.startswith('rebase') else 'repair'),
            'status': status, 'start': start, 'end': end, 'last_event': last_event,
            'idle': (now - last_event) if (status == 'running' and last_event) else None,
            'events': summary.events if summary else 0, 'tool_calls': sum(summary.tools.values()) if summary else 0,
            'tools': dict(summary.tools) if summary else {}, 'tool_errors': summary.tool_errors if summary else 0,
            'shot_runs': summary.shot_runs if summary else 0, 'shots': summary.shots if summary else 0,
            'tokens': tokens, 'exit': exit_code, 'has_answer': answer_size is not None,
            'cost_usd': (cost_records.get(tag) or {}).get('cost_usd'),
        })
    workers.sort(key=lambda item: item['seq'])

    # ---- acceptance rounds
    rounds: dict = collections.defaultdict(list)
    for fname in listdir(folder):
        found = re.match(r'^acceptance-(.+)-(\d+)\.json$', fname)
        if not found or found.group(1) not in by_id:
            continue
        data = rd_json(os.path.join(folder, fname))
        if not data or 'entry' not in data:
            continue
        entry = data['entry']
        end = mtime(os.path.join(folder, fname))
        spent = sum(c.get('seconds') or 0 for c in entry.get('commands', []))
        rounds[found.group(1)].append({
            'round': entry.get('round', int(found.group(2))), 'commands': entry.get('commands', []),
            'end': end, 'start': (end - spent) if end else None,
            'ok': all(c.get('exit') == 0 for c in entry.get('commands', [])) and bool(entry.get('commands')),
        })
    for lst in rounds.values():
        lst.sort(key=lambda item: item['round'])

    # ---- reviews
    def review_info(node_id):
        verdicts = []
        for w in workers:
            if w['node'] != node_id or w['kind'] != 'main' or not w['has_answer']:
                continue
            text = rd(os.path.join(folder, 'workers', w['tag'], 'answer.md')) or ''
            if re.search(ASK_LINE, text):
                continue   # a reviewer that asked the person instead of giving a verdict
            found = re.search(VERDICT, text)
            verdicts.append({'tag': w['tag'], 'verdict': found.group(1) if found else 'unreadable',
                             'findings': len(re.findall(FINDING_LINE, text)) if found else None})
        return verdicts

    max_fix = (result or {}).get('max_fix_rounds') or DEFAULT_MAX_FIX_ROUNDS
    status_by_id: dict = {}
    end_by_id: dict = {}
    # statuses need the dependencies' statuses: resolve in dependency order
    ordered, seen, pending = [], set(), list(nodes)
    while pending:
        progressed = False
        for node in list(pending):
            if all(dep in seen or dep not in by_id for dep in node['deps']):
                ordered.append(node)
                seen.add(node['id'])
                pending.remove(node)
                progressed = True
        if not progressed:
            ordered.extend(pending)
            break
    depth: dict = {}
    for node in ordered:
        depth[node['id']] = 1 + max([depth.get(dep, 0) for dep in node['deps']] or [-1])
    res_nodes = (result or {}).get('nodes') or {}
    built = {}
    for node in ordered:
        nid = node['id']
        mine = [w for w in workers if w['node'] == nid]
        mains = [w for w in mine if w['kind'] == 'main']
        fixes = [w for w in mine if w['kind'] != 'main']
        unmet = [dep for dep in node['deps'] if status_by_id.get(dep) != 'accepted']
        role = kind_of(node)
        state, text = 'waiting', ''
        extra = {}
        ends = [w['end'] for w in mine if w['end']]
        starts = [w['start'] for w in mine if w['start']]
        running_main = [w for w in mains if w['status'] == 'running']
        running_fix = [w for w in fixes if w['status'] == 'running']
        if role == 'acceptance':
            lst = rounds.get(nid, [])
            extra['rounds'] = lst
            starts += [r['start'] for r in lst if r['start']]
            ends += [r['end'] for r in lst if r['end']]
            if lst and lst[-1]['ok']:
                state = 'accepted'
            elif lst:
                if running_fix:
                    state, text = 'repairing', f'being repaired ({running_fix[-1]["kind"]})'
                elif fixes and (fixes[-1]['end'] or 0) >= (lst[-1]['end'] or 0) and not over:
                    state, text = 'running', 'commands running again after the repair'
                elif len(lst) > max_fix or over:
                    state, text = 'failed', 'a command still fails'
                else:
                    state, text = 'repairing', 'a command failed; repair worker starting'
            elif unmet:
                state = 'waiting'
            else:
                state, text = 'running', 'commands running'
                if not starts:
                    prior = [end_by_id.get(dep) for dep in node['deps'] if end_by_id.get(dep)]
                    if prior:
                        starts.append(max(prior))
        elif role == 'review':
            verdicts = review_info(nid)
            extra['verdicts'] = verdicts
            if unmet and not mine:
                state = 'waiting'
            elif running_main:
                state, text = 'running', 'reviewing' + (f' (review {len(mains)})' if len(mains) > 1 else '')
            elif running_fix:
                state, text = 'repairing', f'being repaired ({running_fix[-1]["kind"]})'
            elif verdicts and verdicts[-1]['verdict'] == 'PASS':
                state = 'accepted'
            elif verdicts and verdicts[-1]['verdict'] == 'CHANGES REQUESTED':
                if over or len(fixes) >= max_fix:
                    state, text = 'failed', 'changes still requested'
                else:
                    state, text = 'repairing', 'changes requested; fix, then new review'
            elif mine and over:
                state, text = 'failed', 'no readable verdict'
            elif mine:
                state, text = 'retrying', 'verdict unreadable; retrying'
            else:
                state = 'ready'
        else:
            if not mine:
                state = 'waiting' if unmet else 'ready'
            elif running_main:
                state = 'running'
            elif running_fix:
                state, text = 'repairing', f'fitting onto accepted work ({running_fix[-1]["kind"]})'
            elif mains and mains[-1]['status'] == 'ok':
                if role in ('build', 'script') and has_wt_dir and not result and os.path.isdir(os.path.join(folder, 'wt', nid)):
                    state, text = 'integrating', 'finished; being integrated'
                else:
                    state = 'accepted'
            elif mains and mains[-1]['status'] == 'stopped':
                state, text = 'failed', 'stopped'
            elif role == 'script':
                state, text = 'failed', 'a step failed'   # the engine does not retry a script node
            elif over or len(mains) > 2:
                state, text = 'failed', 'worker failed'
            else:
                state, text = 'retrying', 'worker failed; retrying'
        mine_requests = [r for r in requests if r['node'] == nid]
        if human_run and not over:
            if role == 'human' and mine_requests and mine_requests[-1]['state'] == 'answered':
                state, text = 'accepted', ''   # a gate has no worker: the answer accepts it
            open_here = [r['for'] for r in mine_requests if r['state'] == 'open']
            if open_here:
                state, text = ('waiting for human' if 'person' in open_here else 'waiting for coordinator'), ''
            elif role != 'human' and mine and mine[-1]['status'] == 'ok' and mine[-1]['has_answer'] and \
                    re.search(ASK_LINE, rd(os.path.join(folder, 'workers', mine[-1]['tag'], 'answer.md')) or ''):
                state, text = 'running', 'asked the person; continuing'   # answered, the next worker not started yet
        reason = (state_nodes.get(nid) or {}).get('reason')   # why the engine failed the node (e.g. no change)
        if reason and (state_nodes.get(nid) or {}).get('status') == 'failed':
            state, text = 'failed', reason
        if result and nid in res_nodes:   # the run's own record is authoritative
            recorded = res_nodes[nid]
            if recorded == 'accepted':
                state, text = 'accepted', ''
            elif recorded == 'failed':
                state, text = 'failed', ((result.get('node_reasons') or {}).get(nid) or (text if state == 'failed' else ''))
            elif recorded == 'waiting':
                state, text = 'waiting', 'never started'
        earlier = resumed and (state_nodes.get(nid) or {}).get('status') == 'accepted' and \
            0 < ((state_nodes.get(nid) or {}).get('segment') or 0) < len(segments)
        if earlier:
            state, text = 'accepted', f"accepted earlier (segment {state_nodes[nid]['segment']})"
        if state == 'waiting' and unmet:
            text = 'waiting for ' + ', '.join(unmet)
        status_by_id[nid] = state
        start = min(starts) if starts else None
        end = max(ends) if (ends and state in ('accepted', 'failed')) else None
        if end:
            end_by_id[nid] = end
        running = state in ('running', 'repairing', 'retrying', 'integrating')
        live = [w for w in mine if w['status'] == 'running']
        idles = [w['idle'] for w in live if w['idle'] is not None]
        built[nid] = {
            'id': nid, 'label': node['label'], 'category': node['category'], 'role': node.get('role') or role, 'kind': role,
            'role_file': node.get('role_file') or None, 'deps': node['deps'],
            'depth': depth.get(nid, 0), 'state': state, 'state_text': text,
            'model': (mains[-1]['model'] if mains else node_model(node)),
            'color_role': (mains[-1]['role'] if mains else route_role(node.get('route_category') or node['category'], role)),
            'task': node['TASK'] or node['STEPS'] or (f"steps_file: {node['steps_file']}" if node.get('steps_file') else ''), 'verify': node['VERIFY'], 'commands': commands_of(node['VERIFY'], role == 'acceptance'),
            'start': start, 'end': end, 'seconds': (end - start) if (start and end) else None,
            'running_for': (now - start) if (start and running and not over) else None,
            'idle': min(idles) if idles else None,
            'workers': [w['tag'] for w in mine], 'human': mine_requests, 'accepted_earlier': bool(earlier), **extra,
        }
    out['nodes'] = [built[node_id] for node_id in node_ids]
    out['workers'] = workers

    # ---- run level
    all_starts = [w['start'] for w in workers if w['start']] + ([started] if started else [])
    t0 = started or (min(all_starts) if all_starts else None)
    last_activity = max([w['last_event'] or 0 for w in workers] + [0]) or None
    end_time = finished or (last_activity if over else None)
    states = [built[i]['state'] for i in node_ids]
    out.update(started=t0, finished=finished, duration=((end_time or now) - t0) if t0 else None, over=over,
               verdict=(result or {}).get('verdict'), review_verdicts=(result or {}).get('review_verdicts'),
               counts={s: states.count(s) for s in sorted(set(states))}, last_activity=last_activity)
    if not over:
        state = 'running'
    elif (result or {}).get('verdict') == 'verified':
        state = 'finished: verified'
    elif (result or {}).get('verdict') in (None, 'not_verified'):
        state = 'finished: not verified' + (f" ({result['reason']})" if (result or {}).get('reason') else '')
    else:
        state = f"finished: {result['verdict']}"
    if not over and any(r['for'] == 'person' for r in open_requests):
        state = 'waiting for you'
    elif not over and open_requests:
        state = 'waiting for the coordinator'
    out['run_state'] = state.replace('stopped_by_human', 'stopped by the human').replace('no_answer_from_human', 'no answer from the human')
    out['segments'] = segments
    if resumed:
        out['resume_line'] = (f"resumed {len(segments) - 1} time{'' if len(segments) == 2 else 's'}; segment "
                              f"{len(segments)} started {str(segments[-1].get('started_at') or '?').replace('T', ' ')}")
    out['human'] = requests
    out['human_open'] = open_requests
    out['rulings'] = run_rulings(folder)
    out['person_commands'] = person_commands(plan_text or '')

    # ---- tokens by model (workers that have ended)
    by_model: dict = {}
    for w in workers:
        slot = by_model.setdefault(w['model'], {'role': w['role'], 'workers': 0, 'tool_calls': 0, 'input': 0, 'output': 0, 'cache_read': 0,
                                                'cache_write': 0, 'tokens_known': 0, 'cost': 0.0, 'cost_unknown': 0})
        if w['status'] != 'running':
            if w['cost_usd'] is None:
                slot['cost_unknown'] += 1   # no price, or the engine has not recorded the worker yet
            else:
                slot['cost'] = round(slot['cost'] + w['cost_usd'], 6)
        if slot['role'] != w['role']:
            slot['role'] = 'other'   # one model in several roles has no single colour
        slot['workers'] += 1
        slot['tool_calls'] += w['tool_calls']
        if w['tokens']:
            slot['tokens_known'] += 1
            for key in ('input', 'output', 'cache_read', 'cache_write'):
                slot[key] += int(w['tokens'].get(key) or 0)
    out['by_model'] = by_model
    out['cost'] = (result or {}).get('cost') or (run_state or {}).get('cost')   # {usd, partial, models_without_price, limit_usd, ...}
    out['reason'] = (result or {}).get('reason')
    out['reports'] = bool(REPORTS)   # the grading facts (grader, regrade, "until graded") belong to a project that sets RUN_VIEW_REPORTS
    try:
        out['has_prices'] = bool(config.load().prices)
    except SystemExit:
        out['has_prices'] = False

    # ---- grade
    out['grade'] = None
    task = out['task']
    if task and REPORTS:
        gfolder = os.path.join(REPORTS, task, name, 'grade')
        data = rd_json(os.path.join(gfolder, 'grade.json'))
        if data:
            out['grade'] = {'checks': data.get('checks', []), 'task': data.get('task')}
        elif os.path.isdir(gfolder) and not readable_dir(gfolder):
            out['grade_note'] = 'grade folder is locked'
        elif os.path.isdir(os.path.join(REPORTS, task, name)):
            out['grade_note'] = 'not graded yet'
    out['facts'] = run_facts(name, task, row, out['run_state'], folder, result)
    return out


def build_worker(name, tag):
    if '/' in name or '/' in tag or name.startswith('.') or tag.startswith('.'):
        return None
    wfolder = os.path.join(RUNS, name, 'workers', tag)
    if not readable_dir(wfolder):
        return None
    events = os.path.join(wfolder, 'events.jsonl')
    return {'tag': tag, 'now': time.time(), 'tail': tail_events(events, worker_runner(name, tag)),
            'answer': rd(os.path.join(wfolder, 'answer.md')),
            'stderr': short(rd(os.path.join(wfolder, 'stderr.log'), 4000) or '', 400),
            'events_mtime': mtime(events)}


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = 'run_view'

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def json(self, data, code=200):
        self.send(code, json.dumps(data, ensure_ascii=False), 'application/json; charset=utf-8')

    def do_GET(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:   # never take the viewer down because of one odd file
            try:
                self.json({'error': repr(error)}, 500)
            except OSError:
                pass

    def do_POST(self):
        try:
            self.post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            try:
                self.json({'error': repr(error)}, 500)
            except OSError:
                pass

    def post(self):
        """POST /api/run/<name>/human/<node>-<k> with {"answer": "..."}: the person's answer to an open request."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        parts = path.strip('/').split('/')
        if not (parts[:2] == ['api', 'run'] and len(parts) == 5 and parts[3] == 'human'):
            return self.json({'error': 'not found'}, 404)
        port = self.server.server_address[1]
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        # only this page, on this machine: the Host must be the loopback address and an Origin, if sent, the same
        if host not in (f'127.0.0.1:{port}', f'localhost:{port}') or \
                (origin is not None and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}')):
            return self.json({'error': 'cross-origin request refused'}, 403)
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            return self.json({'error': 'send application/json'}, 415)
        try:
            length = int(self.headers.get('Content-Length') or '')
        except ValueError:
            return self.json({'error': 'no Content-Length'}, 411)
        if length < 0 or length > 4 * ANSWER_LIMIT + 1000:
            return self.json({'error': f'the answer is longer than {ANSWER_LIMIT} characters'}, 413)
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
        except ValueError:
            return self.json({'error': 'not valid JSON'}, 400)
        text = body.get('answer') if isinstance(body, dict) else None
        code, message = save_answer(parts[2], parts[4], text)
        self.json({'ok': True, 'message': message} if code == 200 else {'error': message}, code)

    def route(self):
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)
        if path == '/':
            run = (query.get('run') or [None])[0] or self.server.default or default_run() or ''
            page = PAGE.replace('__RUN__', json.dumps(run).replace('<', '\\u003c'))
            return self.send(200, page, 'text/html; charset=utf-8')
        if path == '/api/runs':
            return self.json({'runs': list_runs(), 'now': time.time()})
        parts = path.strip('/').split('/')
        if parts[:2] == ['api', 'run'] and len(parts) == 3:
            data = build_run(parts[2])
            return self.json(data, 200) if data is not None else self.json({'error': 'no such run'}, 404)
        if parts[:2] == ['api', 'run'] and len(parts) == 5 and parts[3] == 'worker':
            data = build_worker(parts[2], parts[4])
            return self.json(data, 200) if data is not None else self.json({'error': 'not available'}, 404)
        self.send(404, 'not found', 'text/plain')


def write_json(path, data):
    """Rewrites a request file whole (temporary file, os.replace), as the engine and `pangoloop requests` do."""
    temp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    with open(temp, 'w') as handle:
        json.dump(data, handle, indent=1)
    os.replace(temp, path)


def save_answer(name, stem, text):
    """Writes human/<stem>.answer.txt of a run for an open request. Returns (HTTP status, message)."""
    folder = os.path.join(RUNS, name)
    if '/' in name or name.startswith('.') or not re.match(r'^[^/\x00]+-\d+$', stem) or stem.startswith('.'):
        return 404, 'no such request'
    hdir = os.path.join(folder, 'human')
    info = rd_json(os.path.join(hdir, stem + '.json'))
    if not isinstance(info, dict):
        return 404, 'no such request'
    if run_is_over(folder) or info.get('state') != 'open':
        return 409, 'this request is not open (answered, expired, or the run is over)'
    if not isinstance(text, str) or not text.strip():
        return 400, 'the answer is empty'
    if len(text) > ANSWER_LIMIT:
        return 413, f'the answer is longer than {ANSWER_LIMIT} characters'
    final = os.path.join(hdir, stem + '.answer.txt')
    temp = os.path.join(hdir, f'.{stem}.{os.getpid()}.{threading.get_ident()}.tmp')
    request_path = os.path.join(hdir, stem + '.json')
    previous = info.get('answered_by')
    write_json(request_path, dict(info, answered_by='person'))   # answered from the page: by the person
    try:
        with open(temp, 'w') as handle:
            handle.write(text)
        os.link(temp, final)   # fails if an answer is already there: the first answer stands, nothing is overwritten
    except OSError as error:
        restored = rd_json(request_path)
        if isinstance(restored, dict):
            if previous is None:
                restored.pop('answered_by', None)
            else:
                restored['answered_by'] = previous
            write_json(request_path, restored)
        if isinstance(error, FileExistsError):
            return 409, 'this request was already answered'
        return 500, f'cannot write the answer: {error}'
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return 200, 'sent'


def notify(title, body, url):
    """One desktop notification (macOS). The body ends with the page's address, because a click on an osascript
    notification opens Script Editor, not the page; with terminal-notifier installed it is used instead and its click
    opens the page (`-open`). The texts travel as separate arguments, never inside a shell string or the AppleScript
    text; a failure is ignored."""
    if not NOTIFY:
        return
    body = f'{body} Answer at {url}'
    try:
        tool = shutil.which('terminal-notifier')
        if tool:
            subprocess.run([tool, '-title', title, '-message', body, '-open', url],
                           timeout=10, capture_output=True, stdin=subprocess.DEVNULL)
        else:
            subprocess.run(['osascript', '-e', 'on run argv', '-e', 'display notification (item 1 of argv) with title (item 2 of argv)',
                            '-e', 'end run', '--', body, title], timeout=10, capture_output=True, stdin=subprocess.DEVNULL)
    except Exception:
        pass


NOTIFIED: set = set()


def notify_new_requests():
    """Raises a notification once for every request for the person that is open in a run that has not ended. A request
    for the coordinator is silent; when it is escalated it becomes one for the person and is notified then."""
    for name in listdir(RUNS):
        folder = os.path.join(RUNS, name)
        if name.startswith('.') or not os.path.isdir(os.path.join(folder, 'human')) or not readable_dir(folder) \
                or run_is_over(folder):
            continue
        for item in human_requests(folder):
            key = (name, item['name'], item['asked_at'])
            if item['state'] == 'open' and item['for'] == 'person' and key not in NOTIFIED:
                NOTIFIED.add(key)
                url = f'http://127.0.0.1:{PORT}/?run={urllib.parse.quote(name)}'
                notify(f"{name}: {item['node']} needs you", ' '.join(item['request'].split())[:120], url)


def notifier_loop():
    while True:
        try:
            notify_new_requests()
        except Exception:
            pass
        time.sleep(2)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    default = None


FIRST_PORT = 5420
PORT_TRIES = 200


def bind(port):
    """The server on 127.0.0.1: on `port` when it was given (an error if it is taken), else on the configuration's port, else
    on the first free port from 5420 upward."""
    if port is not None:
        return Server(('127.0.0.1', port), Handler)
    configured = config.load().view_port
    if configured is not None:
        return Server(('127.0.0.1', configured), Handler)
    for candidate in range(FIRST_PORT, FIRST_PORT + PORT_TRIES):
        try:
            return Server(('127.0.0.1', candidate), Handler)
        except OSError:
            continue
    raise SystemExit(f'pangoloop: no free port from {FIRST_PORT} to {FIRST_PORT + PORT_TRIES - 1}; give one with --port')


def main(argv=None):
    """Serves the page. Prints `http://127.0.0.1:<port>/` as the first line; in a project, records the process in
    <project>/.pangoloop/view.json and does not start a second page for it (`already serving at <address>`, exit 0)."""
    import signal
    from pangoloop import viewfile
    args = list(sys.argv[1:] if argv is None else argv)
    port, run = None, None
    while args:
        arg = args.pop(0)
        if arg == '--port':
            port = int(args.pop(0))
        elif arg.startswith('--port='):
            port = int(arg.split('=', 1)[1])
        elif not arg.startswith('-'):
            run = arg
    root = os.environ.get('PANGOLOOP_PROJECT') or None
    existing = viewfile.read(root) if root else None
    if existing:
        print(f"already serving at {existing['url']}  (process {existing['pid']}; stop it to start another)", flush=True)
        return 0
    try:
        server = bind(port)
    except OSError as error:
        raise SystemExit(f'pangoloop: cannot listen on port {port}: {error.strerror or error}') from None
    port = server.server_address[1]
    server.default = run
    globals()['PORT'] = port
    if root:
        viewfile.write(root, port)
    if NOTIFY:
        threading.Thread(target=notifier_loop, daemon=True).start()
    print(viewfile.url_for(port), flush=True)
    print(f'(run: {run or "newest started"}; reads the run folders in {RUNS}, writes only answers to open requests; '
          'listens on 127.0.0.1 only and has no login: do not expose it)', flush=True)

    def stop(*_):
        raise KeyboardInterrupt   # SIGTERM ends like Ctrl-C, so that view.json is removed
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if root:
            viewfile.clear(root)
    return 0


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Run view</title>
<style>
:root{--bg:#12151b;--panel:#1a1e27;--panel2:#212633;--line:#303747;--text:#e4e8f0;--dim:#9aa4b8;--faint:#6d778c;
--run:#6cb0ff;--ok:#5fd39a;--bad:#ff7b7b;--rep:#f2b45a;--wait:#8b94a8;--int:#52d1d1;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 var(--sans)}
code,.mono{font-family:var(--mono);font-size:.92em}
main{padding:18px 22px 60px}
@media(min-width:1250px){body.open main{margin-right:600px}}
h1{font-size:20px;margin:0}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:26px 0 10px;font-weight:600}
.dim{color:var(--dim)}.faint{color:var(--faint)}
header.top{display:flex;flex-wrap:wrap;gap:10px 24px;align-items:flex-start;justify-content:space-between}
.titlebar{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
select{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:13px var(--mono);max-width:360px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:130px}
.stat .k{font-size:12px;color:var(--dim)}.stat .v{font-size:19px;font-weight:600;margin-top:2px}
.stat.wide{min-width:260px}
.bigstate{font-size:22px;font-weight:700}
.badge{display:inline-block;border-radius:5px;padding:1px 8px;font-size:12.5px;font-weight:600;border:1px solid currentColor;max-width:100%}
.b-running{color:var(--run)}.b-accepted,.b-PASS{color:var(--ok)}.b-failed{color:var(--bad)}.b-repairing,.b-retrying{color:var(--rep)}
.b-waiting,.b-ready{color:var(--wait)}.b-integrating{color:var(--int)}
.b-waiting-for-human{color:#ffd24a;background:#ffd24a1f}.s-waiting-for-human{border-left-color:#ffd24a;box-shadow:0 0 0 1px #ffd24a88}
.ask{background:#2a2410;border:2px solid #ffd24a;border-radius:10px;padding:14px 18px;margin:16px 0 4px}
.ask h3{margin:0 0 6px;font-size:18px;color:#ffd24a}
.ask .when{font-size:13px;color:var(--dim);margin-bottom:8px}
.ask .l{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin:10px 0 3px}
.ask .req{white-space:pre-wrap;word-break:break-word;font-size:16px;line-height:1.45;background:#1a1608;border:1px solid #ffd24a66;border-radius:6px;padding:10px 12px;max-height:240px;overflow:auto}
.ask textarea{width:100%;min-height:90px;background:#0f1218;color:var(--text);border:1px solid #ffd24a88;border-radius:6px;padding:8px 10px;font:14px/1.4 var(--sans);resize:vertical}
.ask .btns{display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap}
.ask button{border-radius:6px;padding:7px 16px;font:600 14px var(--sans);cursor:pointer;border:1px solid var(--line);color:var(--text);background:var(--panel2)}
.ask button.send{background:#ffd24a;color:#1b1500;border-color:#ffd24a}
.ask button.abort{color:var(--bad);border-color:var(--bad)}
.ask button:disabled{opacity:.5;cursor:default}
.ask .msg2{font-size:13px}
.ask.coord{background:#141c2a;border:1px solid #6cb0ff88;padding:10px 14px;margin:8px 0}
.ask.coord h3{font-size:14px;color:var(--run)}
.ask.coord textarea{border-color:#6cb0ff88}
.ask.coord button.send{background:var(--panel2);color:var(--text);border-color:#6cb0ff}
.b-waiting-for-coordinator{color:var(--run);background:#6cb0ff14}.s-waiting-for-coordinator{border-left-color:var(--run)}
.qwrap{position:relative}
.qwrap .req,.qwrap .pre{padding-right:40px}
button.copybtn,.ask button.copybtn{position:absolute;top:6px;right:8px;display:inline-flex;align-items:center;gap:5px;padding:3px 7px;min-height:26px;border-radius:6px;font:600 12px var(--sans);cursor:pointer;border:1px solid var(--line);color:var(--dim);background:var(--panel2)}
button.copybtn:hover{color:var(--text);border-color:#59647c}
button.copybtn:focus-visible{outline:2px solid #8aa4ff;outline-offset:2px}
button.copybtn svg{width:14px;height:14px;flex:none}
button.copybtn.done,.ask button.copybtn.done{color:var(--ok);border-color:var(--ok)}
button.copybtn.fail,.ask button.copybtn.fail{color:var(--bad);border-color:var(--bad)}
.ask button.copybtn:not(.done):not(.fail){color:var(--dim)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.ruling{margin:6px 0;border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.ruling>summary{cursor:pointer}.ruling:target{border-color:var(--accent,#6ea8ff)}
.hreq{margin:6px 0;border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.tok{border-collapse:collapse;font-size:13px;margin-top:4px}
.tok td,.tok th{padding:2px 12px 2px 0;text-align:left;white-space:nowrap}
.tok td.n{text-align:right;font-family:var(--mono)}
#graphwrap{overflow:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel);padding:16px;position:relative}
#graph{position:relative;display:inline-block;min-width:100%}
#edges{position:absolute;left:0;top:0;pointer-events:none;overflow:visible}
.cols{display:flex;gap:40px;align-items:flex-start;position:relative}
.col{display:flex;flex-direction:column;gap:14px;width:236px;flex:none}
.node{background:var(--panel2);border:1px solid var(--line);border-left:5px solid var(--wait);border-radius:8px;padding:8px 10px;cursor:pointer;position:relative}
.node:hover,.node:focus{border-color:#59647c;outline:none}
.node.sel{box-shadow:0 0 0 2px #8aa4ff}
.s-running{border-left-color:var(--run)}.s-accepted{border-left-color:var(--ok)}.s-failed{border-left-color:var(--bad)}
.s-repairing,.s-retrying{border-left-color:var(--rep)}.s-integrating{border-left-color:var(--int)}
.nhead{display:flex;justify-content:space-between;gap:6px;align-items:center;flex-wrap:wrap}
.nid{font-family:var(--mono);font-weight:700;font-size:14px;word-break:break-all}
.nlabel{margin:3px 0 5px;font-size:13.5px;color:#cfd5e2}
.nmeta{font-size:12px;margin-bottom:3px}
.chip{display:inline-flex;align-items:center;gap:5px;font:12px var(--mono);color:var(--text)}
.chip:before{content:"";width:10px;height:10px;border-radius:3px;background:var(--c)}
.ntime{font-size:12.5px;color:var(--dim)}
.life{font-size:12px;color:var(--run)}.life.stale{color:var(--rep);font-weight:600}
.att{margin-top:6px;padding-top:5px;border-top:1px dashed var(--line);font-size:12px;color:var(--dim)}
.att div{display:flex;justify-content:space-between;gap:2px 8px;flex-wrap:wrap}
.cmds{margin-top:6px;font-size:12px}
.cmd{display:flex;gap:6px;align-items:baseline;padding:1px 0}
.cmd code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;color:#cfd5e2}
.cmd .ex{font-family:var(--mono);white-space:nowrap}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--rep)}
.verd{margin-top:5px;font-size:12.5px}
.verd .badge{margin:0 4px 3px 0}
#timeline{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-x:auto}
.tl{min-width:640px}
.tlrow{display:flex;align-items:center;height:24px;cursor:pointer}
.tlrow:hover{background:#ffffff0a}
.tlrow.sel{background:#8aa4ff18}
.tllab{width:200px;flex:none;font:12px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:8px;color:#cfd5e2}
.tlbar{position:relative;flex:1;height:100%;border-left:1px solid var(--line)}
.bar{position:absolute;top:4px;height:16px;border-radius:4px;background:var(--c);font:11px/16px var(--mono);color:#0b0e14;padding:0 5px;white-space:nowrap;overflow:hidden;min-width:3px}
.bar.run{background-image:repeating-linear-gradient(135deg,#0004 0 6px,#0000 6px 12px)}
.bar.fail{outline:2px solid var(--bad)}
.axis{display:flex;margin-left:200px;position:relative;height:18px;border-bottom:1px solid var(--line);margin-bottom:2px}
.axis span{position:absolute;font:11px var(--mono);color:var(--dim);transform:translateX(-50%)}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;font-size:12px}
#panel{position:fixed;top:0;right:0;width:580px;max-width:100vw;height:100vh;background:var(--panel);border-left:1px solid var(--line);
display:none;flex-direction:column;z-index:10;box-shadow:-10px 0 30px #0008}
body.open #panel{display:flex}
#panel .phead{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
#panel .pbody{overflow:auto;padding:12px 16px 40px;flex:1}
#panel button.x{background:none;border:1px solid var(--line);color:var(--text);border-radius:6px;font-size:18px;line-height:1;padding:4px 10px;cursor:pointer}
.pre{white-space:pre-wrap;word-break:break-word;background:#0f1218;border:1px solid var(--line);border-radius:6px;padding:8px 10px;font:12.5px/1.45 var(--mono);max-height:220px;overflow:auto;margin:4px 0}
.pre.big{max-height:520px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.tab{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:6px;padding:3px 9px;font:12px var(--mono);cursor:pointer}
.tab.on{border-color:#8aa4ff;background:#26304d}
.kv{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;margin:6px 0}
.kv b{font-family:var(--mono)}
#tail{max-height:360px;overflow:auto;background:#0f1218;border:1px solid var(--line);border-radius:6px;padding:4px 0}
.ev{display:flex;gap:8px;padding:2px 8px;font-size:12px;align-items:baseline}
.ev .t{font:11px var(--mono);color:var(--faint);flex:none}
.ev .k{font:600 10.5px var(--mono);flex:none;width:56px}
.ev .v{min-width:0;word-break:break-word;color:#cfd5e2}
.ev-says .k{color:#c6a6ff}.ev-call .k{color:var(--run)}.ev-result .k{color:var(--ok)}.ev-result.err .k{color:var(--bad)}.ev-start .k,.ev-end .k{color:var(--dim)}
.ev-says .v{font-style:italic;color:#d9d3ee}
.checks{width:100%;border-collapse:collapse;font-size:13.5px}
.checks td{padding:5px 10px 5px 0;border-bottom:1px solid var(--line);vertical-align:top}
.msg{padding:30px;border:1px dashed var(--line);border-radius:10px;color:var(--dim)}
.sect{margin-top:12px}
.sect>.l{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);margin-bottom:3px}
.tools{display:flex;flex-wrap:wrap;gap:4px 12px;font:12px var(--mono)}
@media(max-width:900px){main{padding:12px}.col{width:220px}.cols{gap:40px}.stat{min-width:110px}.tllab{width:130px}.axis{margin-left:130px}#panel{width:100vw}}
</style></head><body>
<main>
<header class="top"><div><div class="titlebar"><h1 id="title">Run</h1><select id="sel" aria-label="Choose a run"></select></div>
<div id="sub" class="dim" style="margin-top:4px"></div></div><div id="state"></div></header>
<div id="human"></div><div id="copylive" class="sr" aria-live="polite"></div>
<div id="cards" class="cards"></div>
<div id="rulings"></div>
<h2>Graph</h2><div id="graphwrap"><div id="graph"><svg id="edges"></svg><div class="cols" id="cols"></div></div></div>
<h2>Timeline</h2><div id="timeline"></div>
<div id="gradewrap"></div>
<div id="note" class="dim" style="margin-top:24px;font-size:12px"></div>
</main>
<aside id="panel"><div class="phead" id="phead"></div><div class="pbody" id="pbody"></div></aside>
<script>
const RUN = __RUN__;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STATE = {running:['●','running'],accepted:['✓','accepted'],failed:['✗','failed'],repairing:['⟳','being repaired'],
  retrying:['⟳','retrying'],waiting:['…','waiting'],ready:['…','ready'],integrating:['⇄','integrating'],'waiting for human':['✋','waiting for human'],'waiting for coordinator':['…','waiting for the coordinator']};
const slug = s => String(s == null ? '' : s).replace(/[^A-Za-z0-9]+/g, '-');
let data = null, runsList = [], openNode = null, openWorker = {}, wdata = null, lastHTML = {};
const roleColor = {coder:'#6cb0ff', reviewer:'#f0a35e', interface:'#5fd39a', reader:'#c6a6ff', script:'#7f8aa3', other:'#9aa3b5'};
const roleName = {coder:'coder', reviewer:'reviewer', interface:'interface model', reader:'reader', script:'script', other:'several roles'};
function color(role){ return roleColor[role] || roleColor.other; }   // a worker is coloured by the role it played, never by its model
function dur(s){ if (s==null||isNaN(s)) return '—'; s=Math.max(0,Math.round(s)); const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=s%60;
  return h? `${h}h ${String(m).padStart(2,'0')}m` : m? `${m}m ${String(x).padStart(2,'0')}s` : `${x}s`; }
function clock(t){ if (!t) return '—'; return new Date(t*1000).toLocaleTimeString([], {hour12:false}); }
function stamp(t){ if (!t) return '—'; const d=new Date(t*1000); return d.toLocaleDateString([], {month:'short',day:'numeric'})+' '+d.toLocaleTimeString([], {hour12:false}); }
function num(n){ if (n==null) return '—'; if (n>=1e6) return (n/1e6).toFixed(1)+'M'; if (n>=1e3) return (n/1e3).toFixed(0)+'k'; return String(n); }
function setHTML(el, html, key){ if (lastHTML[key] === html) return false; lastHTML[key] = html;
  const a = document.activeElement, fk = a && a.dataset && a.dataset.copy && el.contains(a) ? a.dataset.copy + '|' + (a.dataset.tail || '') : null;   // a re-render must not take focus off a copy button
  el.innerHTML = html;
  if (fk) { const b = [...el.querySelectorAll('button.copybtn')].find(x => x.dataset.copy + '|' + (x.dataset.tail || '') === fk); if (b) b.focus(); }
  return true; }
async function getJSON(url){ const r = await fetch(url, {cache:'no-store'}); if (!r.ok) throw new Error(url+' '+r.status); return r.json(); }

function renderSelector(){
  const sel = $('#sel');
  const html = runsList.map(r => `<option value="${esc(r.name)}">${esc(r.facts&&r.facts.judged?({ok:'✓ ',bad:'✗ ',warn:'? '}[r.facts.tone]||''):'')}${esc(r.name)} — ${esc(r.facts?r.facts.label:(r.state||''))}${r.facts&&r.facts.tests?' · '+esc(r.facts.tests.text):''}</option>`).join('');
  if (setHTML(sel, html, 'sel')) sel.value = RUN;
}
$('#sel').addEventListener('change', e => { location.href = '/?run=' + encodeURIComponent(e.target.value); });

function renderHeader(){
  const d = data;
  document.title = d.name + ' — run view';
  $('#title').textContent = d.name;
  if (!d.readable) {
    $('#sub').textContent = d.task ? 'task ' + d.task : '';   // a run with no task name shows nothing
    setHTML($('#state'), `<div class="bigstate warn">not available</div>`, 'state');
    let h = `<div class="msg">${esc(d.message||'')}</div>`;
    const r = d.results;
    if (r) {
      h += `<div class="stat wide"><div class="k">from reports/${esc(r.task)}/results.jsonl</div><div class="v">${r.code_grader_passed?'grader passed':'grader failed'}${r.failed_checks&&r.failed_checks.length?' ('+esc(r.failed_checks.join(', '))+')':''}</div>
        <div class="dim">${esc(r.workers)} workers · reviews ${esc((r.reviews||[]).join(' → '))} · ${r.wall_seconds!=null?dur(r.wall_seconds)+' wall':''} · ${esc(r.model_calls)} model calls</div></div>`;
    }
    setHTML($('#human'), '', 'human'); setHTML($('#cards'), h, 'cards'); setHTML($('#timeline'), '', 'tl'); setHTML($('#gradewrap'), '', 'grade');
    let cols = '';
    if (r && r.node_status) cols = `<div class="col">${Object.entries(r.node_status).map(([k,v])=>`<div class="node s-${esc(v)}"><div class="nhead"><span class="nid">${esc(k)}</span><span class="badge b-${esc(v)}">${esc(v)}</span></div></div>`).join('')}</div>`;
    setHTML($('#cols'), cols, 'cols');
    $('#edges').innerHTML = '';
    return false;
  }
  const rs = d.run_state || '';
  const waitingYou = rs.startsWith('waiting for you'), calm = rs.startsWith('running') || rs.startsWith('waiting for the coordinator');
  const F = d.facts || {};
  const tone = F.judged ? ({ok:'ok', bad:'bad', warn:'rep'}[F.tone] || 'ok') : null;
  const cls = tone || (waitingYou ? 'rep' : /failed|not verified|stopped|no answer/.test(rs) ? 'bad' : (calm ? 'run' : 'ok'));
  const shown = F.judged ? F.label : rs;
  const tip = [F.label, F.grade && F.grade.failed_checks.length ? 'grader failed: '+F.grade.failed_checks.join(', ') : '', F.regrade && F.regrade.summary ? 'regrade: '+F.regrade.summary : ''].filter(Boolean).join('\n');
  setHTML($('#state'), `<div class="bigstate" style="color:var(--${cls})" title="${esc(tip)}">${waitingYou?'✋ ':calm?'● ':cls==='ok'?'✓ ':cls==='rep'?'? ':'✗ '}${esc(shown)}</div>` +
    (F.tests ? `<div class="dim" style="font-size:13px;margin-top:3px" title="the last acceptance round and the review verdicts, from the run's records; a count appears only where the command's output was stored">${esc(F.tests.text)}</div>` : ''), 'state');
  $('#sub').textContent = [d.task ? 'task ' + d.task : '', d.runner ? 'runner ' + d.runner : '', `started ${stamp(d.started)}`, `${d.over?'ran':'running for'} ${dur(d.duration)}`,
    d.resume_line || '', d.over ? '' : 'last activity '+dur(d.now-(d.last_activity||d.now))+' ago'].filter(Boolean).join(' · ');
  const c = d.counts||{};
  let h = '';
  if (F.grade || F.regrade) {
    const rg = F.regrade;
    h += `<div class="stat wide"><div class="k">three facts, kept apart</div><table class="tok">` +
      `<tr><td class="n">run</td><td>${esc(F.run)} <span class="dim">(the engine's verdict; the nodes' colours)</span></td></tr>` +
      (F.grade ? `<tr><td class="n">grade</td><td class="${F.grade.result==='passed'?'ok':'bad'}">grader ${esc(F.grade.result)}${F.grade.tests?' '+esc(F.grade.tests)+' hidden tests':''}${F.grade.failed_checks.length?' <span class="dim">('+esc(F.grade.failed_checks.join(', '))+')</span>':''}` +
        (F.grade.commands && F.grade.commands.length ? `<div class="dim" style="font-size:12px">the grader's own run of the commands: ${F.grade.commands.map(c=>esc(c.passed.toLocaleString('en-US'))+' of '+esc(c.total.toLocaleString('en-US'))+' '+esc(c.label)).join(' · ')}</div>` : '') + `</td></tr>` : '') +
      (rg ? `<tr><td class="n">regrade</td><td title="${esc(rg.summary||'')}">` + (rg.status==='unreadable' ? `<span class="warn">regrade record unreadable</span> <span class="dim">(${esc(rg.folder)}/regrade.json)</span>`
         : rg.status==='note' ? `<span class="warn">regraded: see REGRADE.md</span> <span class="dim">${esc(rg.summary||'')}</span>`
         : `<span class="${rg.status==='passed'?'ok':'bad'}">regraded ${esc(rg.tests||'')} ${esc(rg.status)}</span> <span class="dim">${esc(rg.date||'')}${rg.original?' · original '+esc(rg.original):''}</span><div class="dim" style="font-size:12px;white-space:normal;max-width:620px">${esc(rg.summary||'')}</div>`) + `</td></tr>` : '') +
      `</table></div>`;
  }
  h += `<div class="stat"><div class="k">nodes</div><div class="v">${d.nodes.length}</div><div class="dim" style="font-size:12px">${Object.entries(c).map(([k,v])=>v+' '+k).join(' · ')}</div></div>`;
  h += `<div class="stat"><div class="k">workers</div><div class="v">${d.workers.length}</div><div class="dim" style="font-size:12px">${d.workers.filter(w=>w.status==='running').length} running</div></div>`;
  const res = d.results;
  let rows = '';
  for (const [m, s] of Object.entries(d.by_model||{})) {
    const tk = res && res.tokens_by_model && res.tokens_by_model['worker:'+m];
    const calls = res && res.by_model && res.by_model['worker:'+m];
    rows += `<tr><td><span class="chip" style="--c:${color(s.role)}">${esc(m)}</span></td><td class="n">${esc(roleName[s.role]||'')}</td><td class="n">${s.workers} workers</td><td class="n">${s.tool_calls} tool calls</td>` +
      (calls ? `<td class="n">${esc(calls[0])} model calls</td>` : d.reports ? `<td class="n faint">model calls n/a</td>` : '') +
      (`<td class="n">${s.cost_unknown?(s.cost?'$'+esc(s.cost.toFixed(4))+' + ':'')+s.cost_unknown+' w/o price':'$'+esc(s.cost.toFixed(4))}</td>`) +
      (tk ? `<td class="n">${num(tk.fresh)} fresh</td><td class="n">${num(tk.cached)} cached</td><td class="n">${num(tk.output)} out</td>`
          : `<td class="n">${s.tokens_known?num(s.input)+' in':'—'}</td><td class="n">${s.tokens_known?num(s.cache_read)+' cached':''}</td><td class="n">${s.tokens_known?num(s.output)+' out':''}</td>`) + `</tr>`;
  }
  const calls = res ? `${res.model_calls} model calls in total (graded)` : d.reports ? 'model calls are not recorded until graded; tokens are of finished workers' : 'tokens are of finished workers';
  h += `<div class="stat wide"><div class="k">by model · ${esc(calls)}</div><table class="tok">${rows||'<tr><td class="faint">no workers yet</td></tr>'}</table></div>`;
  if (res && res.cost_usd != null) h += `<div class="stat"><div class="k">cost${res.cost_partial?' (partial)':''}</div><div class="v">$${esc(res.cost_usd)}</div></div>`;
  const C = d.cost;
  if (C && (C.workers || C.reported_usd)) {
    const noTable = d.has_prices === false, none = C.usd == null;
    const value = none ? (noTable ? 'no price table' : 'unknown') : '$' + esc(C.usd.toFixed(4));
    const onlyReported = C.workers > 0 && !C.workers_with_cost && C.reported_usd > 0;
    const why = onlyReported ? 'only what commands reported; the cost of the workers is unknown' : noTable ? (none ? 'put prices under [prices] in pangoloop.toml to see a cost' : 'as the runner reported it; no price table' + (C.partial ? ', and some workers reported none' : ''))
      : C.partial ? 'no price for ' + (C.models_without_price.length ? esc(C.models_without_price.join(', ')) : 'some workers (no token counts)') : 'every worker priced';
    const reportedNote = C.reported_usd ? ' · $' + esc(C.reported_usd.toFixed(4)) + ' reported by a command' : '';
    h += `<div class="stat"><div class="k">cost of the run${C.partial?' (partial)':''}${C.limit_usd!=null?' · limit $'+esc(C.limit_usd):''}</div><div class="v">${value}</div>` +
      `<div class="dim" style="font-size:12px">${why}${reportedNote}${d.reason==='cost limit'?' · <b class="bad">stopped: cost limit</b>':''}</div></div>`;
  }
  setHTML($('#cards'), h, 'cards');
  return true;
}

function nodeCard(n){
  const st = STATE[n.state] || ['?', n.state];
  let h = `<div class="node s-${slug(n.state)}${openNode===n.id?' sel':''}" data-id="${esc(n.id)}" tabindex="0">`;
  h += `<div class="nhead"><span class="nid">${esc(n.id)}</span><span class="badge b-${slug(n.state)}">${st[0]} ${esc(n.state_text || st[1])}</span></div>`;
  h += `<div class="nlabel">${esc(n.label)}</div>`;
  h += `<div class="nmeta"><span class="chip" style="--c:${color(n.color_role)}">${esc(n.model)}</span> <span class="dim">${esc(n.role)} · ${esc(n.category)}</span></div>`;
  if (n.running_for != null) h += `<div class="ntime">running ${dur(n.running_for)}</div>`;
  else if (n.seconds != null) h += `<div class="ntime">${n.kind === 'acceptance'?'took':'ran'} ${dur(n.seconds)}</div>`;
  if (n.idle != null) h += `<div class="life${n.idle>120?' stale':''}">● last event ${dur(n.idle)} ago${n.idle>120?' — quiet':''}</div>`;
  if (n.kind === 'acceptance') {
    const rounds = n.rounds || [];
    const last = rounds[rounds.length-1];
    if (rounds.length > 1) h += `<div class="ntime">${rounds.length} rounds: ` + rounds.map(r=>r.ok?'pass':'fail').join(' → ') + `</div>`;
    h += `<div class="cmds">` + (last ? last.commands.map(c=>`<div class="cmd"><span class="${c.exit===0?'ok':'bad'}">${c.exit===0?'✓':'✗'}</span><code title="${esc(c.command)}">${esc(c.command)}</code><span class="ex ${c.exit===0?'dim':'bad'}">exit ${esc(c.exit)} · ${esc(c.seconds)}s</span></div>`).join('')
      : n.commands.map(c=>`<div class="cmd"><span class="faint">·</span><code title="${esc(c)}">${esc(c)}</code></div>`).join('')) + `</div>`;
  }
  if (n.kind === 'review' && (n.verdicts||[]).length) {
    h += `<div class="verd">` + n.verdicts.map(v=>`<span class="badge ${v.verdict==='PASS'?'b-accepted':v.verdict==='CHANGES REQUESTED'?'b-repairing':'b-failed'}">${esc(v.verdict)}${v.findings!=null?' · '+v.findings+' finding'+(v.findings===1?'':'s'):''}</span>`).join('→ ') + `</div>`;
  }
  const att = (data.workers||[]).filter(w => w.node === n.id && (w.kind !== 'main' || n.workers.length > 1));
  if (att.length) h += `<div class="att">` + att.map(w=>`<div><span class="mono">${esc(w.kind==='main'?'try '+(n.workers.indexOf(w.tag)+1):w.kind)}</span><span>${esc(w.model)}${w.role_name?' ('+esc(w.role_name)+')':''} · ${w.status==='running'?'running '+dur(data.now-(w.start||data.now)):dur((w.end||0)-(w.start||0))} · ${esc(w.status)}</span></div>`).join('') + `</div>`;
  return h + `</div>`;
}

function renderGraph(){
  const nodes = data.nodes, maxd = Math.max(0, ...nodes.map(n=>n.depth));
  let h = '';
  for (let d=0; d<=maxd; d++) h += `<div class="col">` + nodes.filter(n=>n.depth===d).map(nodeCard).join('') + `</div>`;
  setHTML($('#cols'), h, 'cols');
  drawEdges();
  const wrap = $('#graphwrap');
  if (!window.__scrolled && wrap.scrollWidth > wrap.clientWidth) {   // once: bring the first active node into view
    window.__scrolled = true;
    const act = wrap.querySelector('.s-running, .s-repairing, .s-retrying, .s-integrating');
    if (act) wrap.scrollLeft = Math.max(0, act.offsetLeft - 40);
  }
}
function drawEdges(){
  const g = $('#graph'), svg = $('#edges'), gb = g.getBoundingClientRect();
  svg.setAttribute('width', g.scrollWidth); svg.setAttribute('height', g.scrollHeight);
  const pos = {};
  g.querySelectorAll('.node').forEach(el => { const b = el.getBoundingClientRect(); pos[el.dataset.id] = {l:b.left-gb.left, r:b.right-gb.left, y:b.top-gb.top+b.height/2}; });
  let p = '';
  for (const n of data.nodes) for (const dep of n.deps) {
    const a = pos[dep], b = pos[n.id]; if (!a || !b) continue;
    const mid = (a.r + b.l)/2;
    const waiting = n.state==='waiting'||n.state==='ready';
    p += `<path d="M${a.r} ${a.y} C${mid} ${a.y} ${mid} ${b.y} ${b.l} ${b.y}" fill="none" stroke="${waiting?'#5a6479':'#8a96b0'}" stroke-width="1.6"${waiting?' stroke-dasharray="5 4"':''}/>`;
  }
  svg.innerHTML = p;
}

function renderTimeline(){
  const items = [];
  for (const w of data.workers) items.push({label:w.tag, node:w.node, start:w.start, end:w.end, model:w.model, role:w.role, rname:w.role_label, status:w.status, kind:'w'});
  for (const n of data.nodes) for (const r of (n.rounds||[])) items.push({label:`${n.id} round ${r.round}`, node:n.id, start:r.start, end:r.end, model:'script (no model)', role:'script', rname:'script', status:r.ok?'ok':'failed', kind:'script'});
  items.sort((a,b)=>(a.start||0)-(b.start||0));
  if (!items.length) { setHTML($('#timeline'), '<span class="dim">no worker has started yet</span>', 'tl'); return; }
  const t0 = Math.min(data.started || Infinity, ...items.map(i=>i.start||Infinity));
  const t1 = Math.max(data.over ? 0 : data.now, ...items.map(i=>i.end||0), data.started + (data.duration||0));
  const span = Math.max(60, t1 - t0);
  const pct = t => ((t - t0) / span * 100);
  const step = [30,60,120,300,600,900,1800,3600,7200].find(s => span/s <= 10) || 7200;
  let axis = ''; for (let t=0; t<=span; t+=step) axis += `<span style="left:${t/span*100}%">${dur(t)}</span>`;
  const roles = [...new Map(items.map(i=>[i.rname||roleName[i.role]||i.role, i.role])).entries()];
  let h = `<div class="legend">` + roles.map(([label, r])=>`<span class="chip" style="--c:${color(r)}">${esc(label)}</span>`).join('') + `<span class="dim">hatched = running now · red outline = failed · time counted from run start</span></div>`;
  h += `<div class="tl"><div class="axis">${axis}</div>`;
  for (const i of items) {
    if (!i.start) continue;
    const end = i.end || data.now, running = !i.end && i.status==='running';
    const w = Math.max(0.3, pct(end) - pct(i.start));
    const label = `${dur(end-i.start)}${running?' running':''}`;
    h += `<div class="tlrow${openNode===i.node?' sel':''}" data-id="${esc(i.node)}" title="${esc(i.label)}: ${esc(clock(i.start))} → ${running?'now':esc(clock(i.end))}"><div class="tllab">${esc(i.label)}</div><div class="tlbar"><div class="bar${running?' run':''}${i.status==='failed'||i.status==='stopped'?' fail':''}" style="left:${pct(i.start)}%;width:${w}%;--c:${color(i.role)}">${esc(label)}${i.status==='failed'?' FAILED':''}</div></div></div>`;
  }
  h += `</div>`;
  setHTML($('#timeline'), h, 'tl');
}

function renderGrade(){
  const d = data; let h = '';
  const g = d.grade, r = d.results;
  if (g || r || d.grade_note) {
    h += `<h2>Grade</h2><div style="display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start">`;
    if (g) {
      h += `<div style="flex:1;min-width:340px"><table class="checks">` + g.checks.map(c=>`<tr><td class="${c.ok?'ok':'bad'}" style="white-space:nowrap">${c.ok?'✓ pass':'✗ fail'}</td><td>${esc(c.check)}<div class="dim mono" style="font-size:12px">${esc(c.detail)}</div></td></tr>`).join('') + `</table></div>`;
    } else h += `<div class="msg" style="flex:1">${esc(d.grade_note||'grade not available')}</div>`;
    if (r) {
      const keys = ['code_grader_passed','failed_checks','last_review','reviews','review_findings','way_says_done','graph_verdict','workers','total_seconds','wall_seconds','model_calls','patch_lines','cost_usd','hit_time_limit','refusals','leak_hits'];
      h += `<div style="flex:1;min-width:300px"><div class="dim" style="font-size:12px;margin-bottom:4px">results.jsonl line</div><div class="pre mono">` + keys.filter(k=>k in r).map(k=>esc(k)+': '+esc(JSON.stringify(r[k]))).join('\n') + `</div></div>`;
    }
    h += `</div>`;
  }
  setHTML($('#gradewrap'), h, 'grade');
}

// ---------------- requests to the person (SG_HUMAN runs)
const drafts = {};
// the copy button of a question: the state ("Copied" / "Could not copy") is part of the markup, so a re-render on polling keeps it
const copyState = {};
const COPY_SVG = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5H3.5A1.5 1.5 0 0 0 2 3v5.5A1.5 1.5 0 0 0 3.5 10H4"/></svg>';
const CHECK_SVG = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8.5l3.2 3.2L13 4.8"/></svg>';
function copyBtn(stem, tail){
  const st = copyState[stem + '|' + (tail ? 1 : '')], cls = st ? (st.ok ? ' done' : ' fail') : '';
  return `<button type="button" class="copybtn${cls}" data-copy="${esc(stem)}"${tail ? ' data-tail="1"' : ''} aria-label="Copy the question" title="Copy the question">${st && st.ok ? CHECK_SVG : COPY_SVG}${st ? '<span>' + (st.ok ? 'Copied' : 'Could not copy') + '</span>' : ''}</button>`;
}
function copyText(r, tail){
  return `${data.name} / ${r.node} (request ${r.k})\n\n${r.request}` + (tail && r.answer_tail ? `\n\n--- the end of the node's message ---\n${r.answer_tail}` : '');
}
async function writeClip(text){
  try { if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(text); return true; } } catch (e) {}
  const prev = document.activeElement, ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch (e) {}
  ta.remove(); if (prev && prev.focus) prev.focus();
  return ok;
}
async function copyClick(btn){
  const stem = btn.dataset.copy, tail = !!btn.dataset.tail, key = stem + '|' + (tail ? 1 : '');
  const r = (data.human || []).find(x => x.name === stem); if (!r) return;
  const ok = await writeClip(copyText(r, tail));
  copyState[key] = {ok, t: setTimeout(() => { delete copyState[key]; renderHuman(); renderPanel(); }, ok ? 1500 : 4000)};
  renderHuman(); renderPanel();
  document.getElementById('copylive').textContent = ok ? 'Copied' : 'Could not copy';
  setTimeout(() => { const l = document.getElementById('copylive'); if (l.textContent) l.textContent = ''; }, 1600);
  if (!ok) {   // select the text, so that the person can press the copy keys
    const first = document.querySelector(`[data-q="${CSS.escape(stem)}"] .req`), last = document.querySelector(`[data-qtail="${CSS.escape(stem)}"]`) || first;
    if (first) { const g = document.createRange(); g.setStartBefore(first); g.setEndAfter(last); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(g); }
  }
}
// one open request as a box to answer in: for the person in the big banner, for the coordinator (a person may step in) in the node's panel
function askBox(r){
  const coord = r.for === 'coordinator';
  const head = coord ? `<h3>${esc(r.node)} is waiting for the coordinator <span class="badge b-waiting-for-coordinator">for the coordinator</span></h3>`
                     : `<h3>✋ ${esc(r.node)} is waiting for you</h3>`;
  const passed = r.escalated_at ? `<div class="when">passed on to the person ${esc(stamp(r.escalated_at))}${r.escalation_note ? ': ' + esc(r.escalation_note) : ''}</div>` : '';
  const cmds = !coord && data.person_commands ? `<div class="l">The person's commands, from the plan</div><div class="pre">${esc(data.person_commands)}</div>` : '';
  return `<div class="ask${coord ? ' coord' : ''}" data-stem="${esc(r.name)}">
    ${head}
    <div class="when">asked ${esc(stamp(r.asked_at))} · <span class="ago" data-t="${esc(r.asked_at)}"></span> · request ${esc(r.k)}${r.worker ? ' · from worker ' + esc(r.worker) + (r.worker_kind ? ' (' + esc(r.worker_kind) + ')' : '') : ' · a gate in the plan (no model runs)'}</div>${passed}
    <div class="l">What it asks</div><div class="qwrap" data-q="${esc(r.name)}"><div class="req">${esc(r.request)}</div>${copyBtn(r.name, !!r.answer_tail)}</div>
    ${r.answer_tail ? `<div class="l">The end of its answer</div><div class="pre" data-qtail="${esc(r.name)}">${esc(r.answer_tail)}</div>` : ''}
    ${cmds}
    <div class="l">${coord ? 'Your answer, if you step in for the coordinator' : 'Your answer'}</div><textarea data-stem="${esc(r.name)}" placeholder="${coord ? 'The coordinator normally answers this from the command line. Your words go to the node as they are.' : 'Your words go to the node as they are. The node continues with them.'}"></textarea>
    <div class="btns"><button class="send" data-stem="${esc(r.name)}">Send answer</button>${coord ? '' : `<button class="abort" data-stem="${esc(r.name)}">Abort run</button>`}<span class="msg2" data-msg></span></div>
  </div>`;
}
function restoreDrafts(){ document.querySelectorAll('.ask textarea').forEach(t => { t.value = drafts[t.dataset.stem] || ''; }); }
function refreshAgo(){ document.querySelectorAll('.ask .ago').forEach(el => { el.textContent = dur(data.now - Number(el.dataset.t)) + ' ago'; }); }
// ---------------- rulings: every answered request of the run, and the rulings added by hand; newest first
const openRulings = new Set();
document.addEventListener('toggle', e => { const d = e.target; if (d && d.classList && d.classList.contains('ruling')) { d.open ? openRulings.add(d.id) : openRulings.delete(d.id); } }, true);
function renderRulings(){
  const list = data.rulings || [];
  const html = list.length ? `<h2>Rulings <span class="dim">(${list.length}: what was decided while the run went on; every later prompt carries them)</span></h2>` + list.map(r => {
    const id = 'ruling-' + r.id, who = r.by === 'person' ? 'the person' : 'the coordinator';
    const from = r.node ? ` · asked by ${esc(r.node)}${r.worker ? ' (worker ' + esc(r.worker) + ')' : ''}` : ' · added without a question';
    return `<details class="ruling" id="${esc(id)}"${openRulings.has(id) ? ' open' : ''}><summary><b>${esc(r.id)}</b> <span class="dim">answered by ${who} ${esc(stamp(r.at))}${from}</span></summary>`
      + (r.question ? `<div class="l">Question</div><div class="pre">${esc(r.question)}</div>` : '') + `<div class="l">Answer</div><div class="pre">${esc(r.answer)}</div></details>`;
  }).join('') : '';
  setHTML($('#rulings'), html, 'rulings');
}
function showRuling(id){ const d = document.getElementById('ruling-' + id); if (!d) return; d.open = true; openRulings.add(d.id); d.scrollIntoView({block: 'center'}); }
function renderHuman(){
  const open = (data.human_open || []).filter(r => r.for === 'person');   // the banner is for requests for the person only
  document.title = (open.length ? '✋ waiting for you — ' : '') + data.name + ' — run view';
  if (setHTML($('#human'), open.map(askBox).join(''), 'human')) restoreDrafts();
  refreshAgo();
}
document.addEventListener('input', e => { if (e.target.matches && e.target.matches('.ask textarea')) drafts[e.target.dataset.stem] = e.target.value; });
async function sendAnswer(box, stem, text){
  const msg = box.querySelector('[data-msg]'), buttons = box.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  try {
    const r = await fetch(`/api/run/${encodeURIComponent(RUN)}/human/${encodeURIComponent(stem)}`,
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({answer: text})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    delete drafts[stem];
    msg.className = 'msg2 ok'; msg.textContent = 'sent; the script picks it up within a couple of seconds';
  } catch (e) { msg.className = 'msg2 bad'; msg.textContent = 'not sent: ' + e.message; buttons.forEach(b => b.disabled = false); }
}
function humanClick(t){
  const btn = t.closest('.ask button'); if (!btn) return false;
  const box = btn.closest('.ask'), stem = btn.dataset.stem, text = box.querySelector('textarea').value;
  if (btn.classList.contains('send')) {
    if (!text.trim()) { const m = box.querySelector('[data-msg]'); m.className = 'msg2 bad'; m.textContent = 'write an answer first'; return true; }
    sendAnswer(box, stem, text);
  } else if (confirm('Abort the whole run? The node, and everything that depends on it, will not be finished.')) {
    sendAnswer(box, stem, 'ABORT' + (text.trim() ? '\n' + text : ''));
  }
  return true;
}

// ---------------- panel
function nodeById(id){ return data && data.nodes && data.nodes.find(n=>n.id===id); }
function curWorker(n){ const ws = data.workers.filter(w=>w.node===n.id); return ws.find(w=>w.tag===openWorker[n.id]) || ws[ws.length-1]; }
function renderPanel(){
  const n = openNode && nodeById(openNode);
  document.body.classList.toggle('open', !!n);
  if (!n) return;
  const st = STATE[n.state] || ['?', n.state];
  setHTML($('#phead'), `<div><div class="nid" style="font-size:17px">${esc(n.id)} <span class="badge b-${slug(n.state)}">${st[0]} ${esc(n.state_text||st[1])}</span></div><div class="dim">${esc(n.label)}</div>
    <div class="nmeta" style="margin-top:4px"><span class="chip" style="--c:${color(n.color_role)}">${esc(n.model)}</span> <span class="dim">${esc(n.role)} · ${esc(n.category)} · needs ${n.deps.length?esc(n.deps.join(', ')):'nothing'}</span></div></div><button class="x" id="closep" aria-label="Close">×</button>`, 'phead');
  const ws = data.workers.filter(w=>w.node===n.id);
  const cur = curWorker(n);
  let h = `<div class="sect"><div class="l">${n.kind === 'script'?'Steps (from the plan)':'Task (from the plan)'}</div><div class="pre">${esc(n.task)}</div></div>`;
  if (n.commands.length) h += `<div class="sect"><div class="l">${n.kind === 'acceptance'?'Commands it runs':'Told to check with'}</div>` + n.commands.map(c=>`<div class="mono" style="margin:2px 0;word-break:break-all">${esc(c)}</div>`).join('') + `</div>`;
  if ((n.human||[]).length) {
    h += `<div class="sect"><div class="l">Requests</div>` + n.human.map(r => {
      if (r.state === 'open' && r.for === 'coordinator') return askBox(r);   // calm, and a person may step in
      const to = r.for === 'coordinator' ? '<span class="badge b-waiting-for-coordinator">for the coordinator</span>' : '<span class="dim">for the person</span>';
      const by = r.answered_by || (r.for === 'coordinator' ? 'coordinator' : 'person');
      const link = r.state === 'answered' && (data.rulings || []).some(x => x.id === r.name) ? ` <a href="#ruling-${esc(r.name)}" class="rulink" data-r="${esc(r.name)}">ruling</a>` : '';
      return `<div class="hreq"><div><b>request ${esc(r.k)}</b>${link} ${to}${r.worker ? ' <span class="dim">worker ' + esc(r.worker) + (r.worker_kind ? ' (' + esc(r.worker_kind) + ')' : '') + '</span>' : ''} <span class="${r.state==='answered'?'ok':'warn'}">${esc(r.state)}</span> <span class="dim">asked ${esc(clock(r.asked_at))}${r.answered_at?' · answered '+esc(clock(r.answered_at)):''}</span></div>`
        + (r.escalated_at ? `<div class="dim" style="font-size:12px">passed on to the person ${esc(clock(r.escalated_at))}${r.escalation_note ? ': ' + esc(r.escalation_note) : ''}</div>` : '')
        + `<div class="qwrap"><div class="pre">${esc(r.request)}</div>${copyBtn(r.name, false)}</div>` + (r.answer != null ? `<div class="dim" style="font-size:12px">answered by the ${esc(by)}</div><div class="pre">${esc(r.answer)}</div>` : '') + `</div>`;
    }).join('') + `</div>`;
  }
  if (n.kind === 'acceptance') {
    h += `<div class="sect"><div class="l">Rounds</div>` + ((n.rounds||[]).map(r=>`<div style="margin:6px 0"><b>round ${r.round}</b> <span class="${r.ok?'ok':'bad'}">${r.ok?'✓ all passed':'✗ failed'}</span> <span class="dim">${esc(clock(r.start))} → ${esc(clock(r.end))}</span>` +
      r.commands.map(c=>`<div class="cmd"><span class="${c.exit===0?'ok':'bad'}">${c.exit===0?'✓':'✗'}</span><code>${esc(c.command)}</code><span class="ex">exit ${esc(c.exit)} · ${esc(c.seconds)}s</span></div>`).join('') + `</div>`).join('') || '<span class="dim">no round has ended yet (the script writes a round only when all its commands have run)</span>') + `</div>`;
  }
  if (n.kind === 'review' && (n.verdicts||[]).length) h += `<div class="sect"><div class="l">Verdicts</div>` + n.verdicts.map(v=>`<div><span class="mono">${esc(v.tag)}</span> → <b>${esc(v.verdict)}</b>${v.findings!=null?' · '+v.findings+' findings':''}</div>`).join('') + `</div>`;
  if (ws.length) {
    h += `<div class="tabs">` + ws.map(w=>`<button class="tab${w.tag===cur.tag?' on':''}" data-w="${esc(w.tag)}">${esc(w.tag)} · ${esc(w.status)}</button>`).join('') + `</div>`;
    const tools = Object.entries(cur.tools).sort((a,b)=>b[1]-a[1]);
    h += `<div class="kv"><span>model <b>${esc(cur.model)}</b></span><span>events <b>${cur.events}</b></span><span>tool calls <b>${cur.tool_calls}</b></span><span>tool errors <b>${cur.tool_errors}</b></span><span>screenshots <b>${cur.shots}</b> <span class="dim">(${cur.shot_runs} shot.mjs runs)</span></span>` +
      `<span>${cur.status==='running'?'running '+dur(data.now-(cur.start||data.now)):'took '+dur((cur.end||0)-(cur.start||0))}</span>${cur.tokens?`<span>tokens in <b>${num(cur.tokens.input)}</b> cached <b>${num(cur.tokens.cache_read)}</b> out <b>${num(cur.tokens.output)}</b></span>`:''}${cur.cost_usd!=null?`<span>cost <b>$${esc(cur.cost_usd.toFixed(4))}</b></span>`:''}</div>`;
    h += `<div class="tools">${tools.map(([k,v])=>`<span>${esc(k)} ${v}</span>`).join('')||'<span class="dim">no tool calls yet</span>'}</div>`;
    h += `<div class="sect"><div class="l">What it is doing — last 40 events${cur.status==='running'?' (live)':''}</div><div id="tail"></div></div>`;
    h += `<div class="sect"><div class="l">answer.md</div><div id="answer"></div></div>`;
  } else {
    h += `<div class="msg" style="margin-top:12px">${n.kind === 'acceptance'||n.kind === 'script'?'Done by the script, no model.':n.kind === 'human'?'A gate for the person: no model runs here.':'No worker has started on this node yet.'}</div>`;
  }
  const body = $('#pbody'), st0 = body.scrollTop;
  if (setHTML(body, h, 'pbody')) { body.scrollTop = st0; delete $('#pbody').dataset.x; restoreDrafts(); }
  refreshAgo();
  renderWorker();
}
function renderWorker(){
  const n = openNode && nodeById(openNode); if (!n) return;
  const cur = curWorker(n);
  if (!cur || !wdata || wdata.tag !== cur.tag) return;
  const tail = $('#tail'); if (!tail) return;
  const kind = {says:'SAYS',call:'CALL',result:'RESULT',start:'START',end:'END'};
  const html = (wdata.tail||[]).map(e=>`<div class="ev ev-${esc(e.kind)}${e.error?' err':''}"><span class="t">${esc(e.ts?clock(e.ts/1000):'')}</span><span class="k">${e.error?'ERROR':esc(kind[e.kind]||e.kind)}</span><span class="v">${e.kind==='call'?`<b>${esc(e.name)}</b> <code>${esc(e.text)}</code>`:e.kind==='result'?`<span class="dim">${esc(e.name)}${e.ms!=null?' '+esc(e.ms)+'ms':''}</span> ${esc(e.text)}`:esc(e.text)}</span></div>`).join('') || '<div class="ev dim">no events yet</div>';
  if (tail.dataset.html !== html) {
    const atBottom = !tail.dataset.init || tail.scrollHeight - tail.scrollTop - tail.clientHeight < 30, prev = tail.scrollTop;
    tail.dataset.html = html; tail.innerHTML = html; tail.dataset.init = '1'; tail.scrollTop = atBottom ? tail.scrollHeight : prev;
  }
  const ans = $('#answer');
  if (ans) { const a = wdata.answer; const t = a==null ? '<div class="dim">not written yet: the worker has not finished</div>' : `<div class="pre big">${esc(a)}</div>`;
    if (ans.dataset.html !== t) { const s = ans.firstElementChild ? ans.firstElementChild.scrollTop : 0; ans.dataset.html = t; ans.innerHTML = t; if (ans.firstElementChild) ans.firstElementChild.scrollTop = s; } }
}
async function refreshWorker(){
  const n = openNode && nodeById(openNode); if (!n) { wdata = null; return; }
  const cur = curWorker(n); if (!cur) { wdata = null; return; }
  try { wdata = await getJSON(`/api/run/${encodeURIComponent(RUN)}/worker/${encodeURIComponent(cur.tag)}`); } catch (e) { wdata = null; }
  renderWorker();
}
function openIt(id){ openNode = id; wdata = null; delete lastHTML.pbody;
  try { history.replaceState(null, '', location.pathname + location.search + (id ? '#node=' + encodeURIComponent(id) : '')); } catch(e){}
  if (data && data.readable) { renderPanel(); renderGraph(); renderTimeline(); refreshWorker(); } }
document.addEventListener('click', e => {
  const t = e.target;
  const cb = t.closest && t.closest('button.copybtn'); if (cb) { e.preventDefault(); copyClick(cb); return; }
  const rl = t.closest && t.closest('a.rulink'); if (rl) { e.preventDefault(); showRuling(rl.dataset.r); return; }
  if (humanClick(t)) return;
  if (t.closest('#closep')) return openIt(null);
  const tab = t.closest('.tab'); if (tab) { openWorker[openNode] = tab.dataset.w; wdata = null; delete lastHTML.pbody; renderPanel(); refreshWorker(); return; }
  const el = t.closest('.node, .tlrow'); if (el && el.dataset.id) openIt(openNode === el.dataset.id && el.classList.contains('node') ? null : el.dataset.id);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && openNode) openIt(null);
  if ((e.key==='Enter'||e.key===' ') && e.target.classList && e.target.classList.contains('node')) { e.preventDefault(); openIt(e.target.dataset.id); } });

async function tick(){
  try {
    const [runs, d] = await Promise.all([getJSON('/api/runs'), RUN ? getJSON('/api/run/' + encodeURIComponent(RUN)) : Promise.resolve(null)]);
    runsList = runs.runs; data = d; $('#note').textContent = '';
    renderSelector();
    if (!d) { $('#sub').textContent = 'no run to show'; }
    else if (d.error) { $('#sub').textContent = d.error; }
    else if (renderHeader()) { renderHuman(); renderRulings(); renderGraph(); renderTimeline(); renderGrade(); renderPanel(); await refreshWorker();
      $('#note').textContent = 'View of the run folder, refreshed every 3 s; it writes only your answers to open requests of the nodes. Not recorded in the files: ' + (data.reports ? 'model calls (until graded), ' : '') + 'tokens of unfinished workers, the progress inside an acceptance round before it ends, and the final status of a node before result.json exists (inferred from the files).'; }
  } catch (e) { $('#note').textContent = 'cannot reach the server: ' + e.message; }
  setTimeout(tick, 3000);
}
const m = location.hash.match(/node=([^&]+)/); if (m) openNode = decodeURIComponent(m[1]);
window.addEventListener('resize', () => { if (data && data.nodes) drawEdges(); });
tick();
</script></body></html>
'''

if __name__ == '__main__':
    main()
