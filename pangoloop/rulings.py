"""Rulings: what a run's coordinator (or person) decided while the run went on, kept in <run>/rulings.md.

Every answered request of a run is a ruling on the plan ("do (b), not (a) or (c)", "move this file there"). Without that a later review round
never sees one of them, reports the work that followed it as a fault, and the next
repair worker undid correct work. So the engine appends each ruling to <run>/rulings.md as it is made, and puts them into
every prompt it builds afterwards (pangoloop/graph.py, `Run.ask`).

An aborted request (an answer whose first line is ABORT), an answer that is empty, and a closing line whose question is
empty or `none` (pangoloop/plan.py, is_empty_question) are not rulings. A coordinator may also add a ruling without a
question (`python3 -m pangoloop requests rule`); its id is `ruling-<n>`.

The file: a title, then one block per ruling, in the order they were made. A block starts with a heading line

    ## ruling <id> | node: <node> | worker: <worker> | by: <coordinator|person> | at: <time> | question: <n> chars | answer: <m> chars

then a blank line, `Question:`, a blank line, the question, a blank line, `Answer:`, a blank line, the answer, a blank
line. The two lengths in the heading let a reader take the texts exactly, whatever they contain.
"""
import fcntl
import os
import re

from pangoloop.plan import is_empty_question

FILE_NAME = 'rulings.md'
TITLE = ('# Rulings made during this run\n\n'
         'Each ruling is an answer to a request of a node, or a ruling added by the coordinator. Where a ruling and the\n'
         'task statement differ, the ruling holds.\n\n')
HEADING = re.compile(r'## ruling (\S+) \| node: (.*?) \| worker: (.*?) \| by: (\S+) \| at: (\S*) \| '
                     r'question: (\d+) chars \| answer: (\d+) chars\n')
DEFAULT_LIMIT = 12000   # characters up to which every ruling goes into a prompt in full (limits.rulings_chars)
OLDER_CHARS = 600       # what an older ruling keeps when they are too long together
SECTION_TITLE = ('Rulings made during this run (they are part of the task; where a ruling and the statement differ, '
                 'the ruling holds)')
REVIEWER_LINE = 'Do not report as a fault something a ruling required.'


def path_of(folder: str) -> str:
    return os.path.join(folder, FILE_NAME)


def render(ruling: dict) -> str:
    """One block of the file for a ruling {id, node, worker, by, at, question, answer}."""
    question = (ruling.get('question') or '').strip()
    answer = (ruling.get('answer') or '').rstrip('\n')
    heading = (f"## ruling {ruling['id']} | node: {ruling.get('node') or '-'} | worker: {ruling.get('worker') or '-'} | "
               f"by: {ruling.get('by') or 'coordinator'} | at: {ruling.get('at') or ''} | "
               f"question: {len(question)} chars | answer: {len(answer)} chars\n")
    return f'{heading}\nQuestion:\n\n{question}\n\nAnswer:\n\n{answer}\n\n'


def append(folder: str, ruling: dict) -> None:
    """Appends one ruling to <run>/rulings.md (made, with its title, when it is not there). Safe against two writers at
    once (the engine's threads, `requests rule` in another process): the file is locked while it is written."""
    os.makedirs(folder, exist_ok=True)
    with open(path_of(folder), 'a+', encoding='utf-8', newline='') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(TITLE)
            handle.write(render(ruling))
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse(text: str) -> list[dict]:
    """The rulings in the text of a rulings.md, oldest first. A damaged block is skipped, not fatal."""
    found, position = [], 0
    while True:
        head = HEADING.search(text, position)
        if not head:
            return found
        ident, node, worker, by, at, q_len, a_len = head.groups()
        q_len, a_len = int(q_len), int(a_len)
        marker = '\nQuestion:\n\n'
        start = head.end()
        position = start
        if not text.startswith(marker, start):
            continue
        start += len(marker)
        middle = '\n\nAnswer:\n\n'
        if text[start + q_len:start + q_len + len(middle)] != middle:
            continue
        a_start = start + q_len + len(middle)
        found.append({'id': ident, 'node': None if node == '-' else node, 'worker': None if worker == '-' else worker,
                      'by': by, 'at': at or None, 'question': text[start:start + q_len],
                      'answer': text[a_start:a_start + a_len]})
        position = a_start + a_len


def load(folder: str) -> list[dict]:
    """The rulings of a run folder, oldest first; [] when there is no file."""
    try:
        with open(path_of(folder), encoding='utf-8', errors='replace', newline='') as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)   # not while a writer is in the middle of a block
            return parse(handle.read())
    except OSError:
        return []


def next_manual_id(folder: str) -> str:
    """`ruling-<n>` for the next ruling a coordinator adds without a question."""
    numbers = [int(match.group(1)) for item in load(folder) if (match := re.fullmatch(r'ruling-(\d+)', item['id']))]
    return f'ruling-{max(numbers, default=0) + 1}'


def is_ruling(info: dict, answer: str | None) -> bool:
    """True for an answered request that is a ruling: it has a non-blank answer that is not an abort, and its question is
    not an empty or `none` closing line."""
    text = answer or ''
    if not text.strip() or text.split('\n', 1)[0].rstrip('\r').strip() == 'ABORT':
        return False
    return not is_empty_question(str(info.get('request') or ''))


def from_requests(folder: str) -> list[dict]:
    """The rulings that the request files of a run make, oldest answer first: the function that builds rulings.md from
    human/*.json and human/*.answer.txt. The engine appends each ruling as it is made; this gives the same list again from
    the files (for a run recorded before rulings existed, and as a check)."""
    from pangoloop import requests
    found = []
    for item in requests.load_requests(folder):
        if item['state'] != 'answered' or not is_ruling(item, item['answer']):
            continue
        found.append({'id': item['id'], 'node': item['node'], 'worker': item['worker'],
                      'by': item['answered_by'] or item['for'], 'at': item['answered_at'] or item['asked_at'],
                      'question': item['request'], 'answer': (item['answer'] or '').rstrip('\n')})
    found.sort(key=lambda ruling: ruling['at'] or '')
    return found


def rebuild(folder: str) -> list[dict]:
    """Writes <run>/rulings.md afresh from the request files, keeping the rulings a coordinator added by hand
    (`ruling-<n>`), in the order of their times. Returns the rulings written."""
    manual = [item for item in load(folder) if re.fullmatch(r'ruling-\d+', item['id'])]
    every = sorted(from_requests(folder) + manual, key=lambda ruling: ruling['at'] or '')
    text = TITLE + ''.join(render(ruling) for ruling in every) if every else ''
    if text:
        with open(path_of(folder), 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
    return every


def describe(ruling: dict) -> str:
    """The line that heads a ruling in a prompt."""
    who = 'the coordinator' if ruling.get('by') == 'coordinator' else f"the {ruling.get('by')}"
    asked = ''
    if ruling.get('node'):
        asked = f" Asked by node `{ruling['node']}`" + (f", worker {ruling['worker']}" if ruling.get('worker') else '') + '.'
    return f"[ruling {ruling['id']}, answered by {who} at {ruling.get('at') or '?'}.{asked}]"


def full_text(ruling: dict) -> str:
    question = (ruling.get('question') or '').strip()
    body = (f"Question:\n{question}\n\n" if question else '') + f"Answer:\n{ruling['answer'].rstrip()}"
    return f'{describe(ruling)}\n{body}'


def short_text(ruling: dict) -> str:
    answer = ruling['answer'].strip()
    cut = len(answer) > OLDER_CHARS
    return f"{describe(ruling)}\nAnswer (first {OLDER_CHARS} characters):\n{answer[:OLDER_CHARS]}" + (' ...' if cut else '')


def section(folder: str, limit: int = DEFAULT_LIMIT, reviewer: bool = False) -> str:
    """The section every prompt carries once rulings exist ('' while there are none): all rulings in full when they are at
    most `limit` characters together, else the newest in full and the older ones as their first 600 characters, with the
    path of rulings.md to read. For a reviewer it also says not to report what a ruling required."""
    rulings = load(folder)
    if not rulings:
        return ''
    blocks = [full_text(item) for item in rulings]
    note = ''
    if sum(len(block) for block in blocks) > limit:
        blocks = [short_text(item) for item in rulings[:-1]] + [blocks[-1]]
        note = (f'\n\nThe older rulings are cut here; read every ruling in full in {path_of(folder)} before you act on '
                'a point they may decide.')
    lines = [SECTION_TITLE + ':', '', '\n\n'.join(blocks) + note]
    if reviewer:
        lines += ['', REVIEWER_LINE]
    return '\n'.join(lines) + '\n'
