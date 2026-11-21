"""The steps of a node with `role: script`, and the two that touch files: `delete` and `move` (step 4).

A script node is done by the engine itself, with no model. Its `STEPS:` field is a
list of steps separated by ` ; ` (parsed in pangoloop/plan.py); the engine performs them in order in the node's copy of
the repository. This module holds what is safe to test on its own: finding the paths a `delete` step names, refusing
the ones that must never be touched, and deleting them.

A `delete` is refused, and nothing is deleted by that step, when a path:
  - is absolute, or leaves the copy with `..`;
  - is the copy's root itself;
  - is, or is inside, `.git`;
  - goes through a symbolic link, or is one (a link is never followed and never deleted by a step);
  - resolves outside the copy once links are resolved;
  - matches nothing (unless the step is `delete?`, which skips it and says so).
All the paths of one step are checked before the first is deleted. Wildcards: `*` and `?` and `[..]` inside one path
component, `**` for any number of folders (as the last component: everything below). As in a shell, a wildcard does not
match a name that starts with a dot unless the pattern's component does too, `**` does not enter folders that start with
a dot, and `.git` is never matched by a wildcard.
"""
import fnmatch
import os
import posixpath
import shutil
import stat


class StepRefused(Exception):
    """A step is refused or failed; the message says why, in words for the person who reads the node's handoff."""


def _magic(component: str) -> bool:
    return any(char in component for char in '*?[')


def normalize(raw: str) -> str:
    """The path as a clean relative path with `/`, or StepRefused for the static refusals."""
    if not raw or not raw.strip():
        raise StepRefused('an empty path')
    if os.path.isabs(raw):
        raise StepRefused(f'{raw}: an absolute path is not allowed (paths are relative to the node\'s copy)')
    path = posixpath.normpath(raw)
    if path == '..' or path.startswith('../'):
        raise StepRefused(f'{raw}: leaves the node\'s copy')
    if path == '.':
        raise StepRefused(f'{raw}: is the root of the node\'s copy itself')
    if any(part.lower() == '.git' for part in path.split('/')):
        raise StepRefused(f'{raw}: anything inside .git is refused')
    return path


class RealFS:
    """The node's copy on disk, as `expand` and the move checks see it. Paths are relative with `/`; '' is the root."""

    def __init__(self, copy: str):
        self.copy = copy

    def _full(self, rel: str) -> str:
        return os.path.join(self.copy, rel) if rel else self.copy

    def kind(self, rel: str) -> str | None:
        """'link', 'dir', 'file' or None (a link is not followed)."""
        try:
            mode = os.lstat(self._full(rel)).st_mode
        except OSError:
            return None
        return 'link' if stat.S_ISLNK(mode) else 'dir' if stat.S_ISDIR(mode) else 'file'

    def isdir(self, rel: str, follow: bool = True) -> bool:
        return os.path.isdir(self._full(rel)) if follow else self.kind(rel) == 'dir'

    def listdir(self, rel: str) -> list[str]:
        return sorted(os.listdir(self._full(rel)))


def expand(copy: str, pattern: str, fs=None) -> list[str]:
    """The paths (relative, `/`) inside `copy` that the normalized pattern names. A path with no wildcard is returned
    when it exists (a broken link counts as existing); nothing found is an empty list. `fs` is the file system to look at
    (default: the real copy; `steps check` passes an overlay that has the earlier steps applied)."""
    fs = fs or RealFS(copy)
    found: list[str] = []

    def walk(rel: str, parts: list[str]) -> None:
        if not parts:
            if fs.kind(rel) is not None:
                found.append(rel)
            return
        head, rest = parts[0], parts[1:]
        if head == '**':
            if fs.kind(rel) != 'dir':
                return
            names = fs.listdir(rel)
            if not rest:   # everything below, hidden names excepted; a folder goes whole
                found.extend(f'{rel}/{name}' if rel else name for name in names if not name.startswith('.'))
                return
            walk(rel, rest)   # zero folders
            for name in names:
                sub = f'{rel}/{name}' if rel else name
                if not name.startswith('.') and fs.kind(sub) == 'dir':
                    walk(sub, parts)
        elif _magic(head):
            if fs.kind(rel) != 'dir':
                return
            for name in fs.listdir(rel):
                if name.lower() == '.git' or (name.startswith('.') and not head.startswith('.')):
                    continue
                sub = f'{rel}/{name}' if rel else name
                if fnmatch.fnmatchcase(name, head) and (not rest or fs.isdir(sub)):
                    walk(sub, rest)
        else:
            walk(f'{rel}/{head}' if rel else head, rest)

    walk('', pattern.split('/'))
    return found


def check_inside(copy: str, rel: str) -> None:
    """The dynamic refusals for one path that exists: a link in it, or a resolution outside the copy."""
    current = copy
    for part in rel.split('/'):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise StepRefused(f'{rel}: goes through a symbolic link ({os.path.relpath(current, copy)}); links are never '
                              'followed or deleted by a step')
    real, root = os.path.realpath(os.path.join(copy, rel)), os.path.realpath(copy)
    if real == root:
        raise StepRefused(f'{rel}: is the root of the node\'s copy itself')
    if not real.startswith(root + os.sep):
        raise StepRefused(f'{rel}: resolves outside the node\'s copy ({real})')


def count_files(path: str) -> int:
    if not os.path.isdir(path) or os.path.islink(path):
        return 1
    total = 0
    for here, folders, files in os.walk(path):
        total += len(files) + sum(1 for name in folders if os.path.islink(os.path.join(here, name)))
    return total


def delete_step(copy: str, patterns: list[str], optional: bool = False) -> dict:
    """Performs one `delete` step in `copy`. Returns {'deleted': [(path, file count)], 'skipped': [pattern, ...]}; raises
    StepRefused (nothing deleted by this step) for a refusal or a pattern that matches nothing (unless `optional`)."""
    chosen: list[str] = []
    skipped: list[str] = []
    for raw in patterns:
        pattern = normalize(raw)
        matches = expand(copy, pattern)
        if not matches:
            if optional:
                skipped.append(raw)
                continue
            raise StepRefused(f'{raw}: matches nothing in the node\'s copy (write `delete?` to allow that)')
        for rel in matches:
            check_inside(copy, rel)
            chosen.append(rel)
    keep: list[str] = []
    for rel in sorted(set(chosen)):
        if not any(rel.startswith(parent + '/') for parent in keep):
            keep.append(rel)
    counts = [(rel, count_files(os.path.join(copy, rel))) for rel in keep]
    for rel, _ in counts:
        full = os.path.join(copy, rel)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.unlink(full)
        except OSError as error:
            raise StepRefused(f'{rel}: could not be deleted: {error}') from error
    return {'deleted': counts, 'skipped': skipped}


# ---- move (step 4) --------------------------------------------------------------------------------------------------

def _parents(rel: str) -> list[str]:
    """The proper ancestors of a relative path, from the top: 'a/b/c' -> ['a', 'a/b']."""
    parts = rel.split('/')
    return ['/'.join(parts[:index]) for index in range(1, len(parts))]


def validate_move(fs, source: str, target: str) -> tuple[str, str]:
    """The refusals of one `move`, looked up in `fs` (the real copy or an overlay). Returns the normalized (source,
    target) or raises StepRefused: both paths follow the rules of `delete` (relative, inside the copy, not the root, not
    `.git`); no symbolic link in the source's path or the source itself; the source exists; the target is not the source
    or inside it; the target does not exist (no overwrite, no merge into a folder); no existing ancestor of the target is
    a link or a file."""
    src, dst = normalize(source), normalize(target)
    for part in _parents(src) + [src]:
        if fs.kind(part) == 'link':
            raise StepRefused(f'{source}: goes through a symbolic link ({part}); links are never followed or moved by a step')
    if fs.kind(src) is None:
        raise StepRefused(f'{source}: the source does not exist' + fs.why_gone(src))
    if dst == src or dst.startswith(src + '/'):
        raise StepRefused(f'{target}: the target is inside its own source {source}' if dst != src
                          else f'{target}: the target is the source itself')
    if fs.kind(dst) is not None:
        raise StepRefused(f'{target}: the target already exists (a move never overwrites or merges)' + fs.why_there(dst))
    for part in _parents(dst):
        kind = fs.kind(part)
        if kind == 'link':
            raise StepRefused(f'{target}: goes through a symbolic link ({part}); links are never followed by a step')
        if kind == 'file':
            raise StepRefused(f'{target}: {part} is a file, so the target cannot be made inside it')
        if kind is None:
            break
    return src, dst


def move_step(copy: str, source: str, target: str) -> dict:
    """Performs one `move` in `copy` (a file-system move; git sees it when the node's work is committed). Raises
    StepRefused, having changed nothing, for any refusal of `validate_move`. Missing parent folders of the target are
    made; a source folder left empty is removed, upward, until one that is not empty (never the root).
    Returns {'moved': (source, target, file count), 'removed': [emptied folders]}."""
    fs = RealFS(copy)
    fs.why_gone = fs.why_there = lambda rel: ''
    src, dst = validate_move(fs, source, target)
    check_inside(copy, src)
    count = count_files(os.path.join(copy, src))
    try:
        os.makedirs(os.path.dirname(os.path.join(copy, dst)), exist_ok=True)
        os.rename(os.path.join(copy, src), os.path.join(copy, dst))
    except OSError as error:
        raise StepRefused(f'{source} -> {target}: could not be moved: {error}') from error
    removed: list[str] = []
    here = posixpath.dirname(src)
    while here and os.path.isdir(os.path.join(copy, here)) and not os.listdir(os.path.join(copy, here)):
        os.rmdir(os.path.join(copy, here))
        removed.append(here)
        here = posixpath.dirname(here)
    return {'moved': (src, dst, count), 'removed': removed}


def load_steps_file(copy: str, rel: str, form: str = '') -> list[dict]:
    """The steps of a node's `steps_file`, read from the node's copy: a file inside the repository, so a relative path
    with the refusals of `delete` (no `..`, no `.git`, no link on the way). StepRefused when it is missing or any line
    is wrong (all wrong lines are named)."""
    from pangoloop import plan
    path = normalize(rel)
    check_inside(copy, path)
    full = os.path.join(copy, path)
    if not os.path.isfile(full):
        raise StepRefused(f'steps file {rel}: not a file in the node\'s copy (it must be committed in the base of the run '
                          'or produced by a node this one depends on)')
    try:
        with open(full, encoding='utf-8') as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        raise StepRefused(f'steps file {rel}: cannot be read: {error}') from error
    found, problems = plan.parse_steps_file(text, form)
    if problems:
        raise StepRefused(f'steps file {rel}: ' + '; '.join(problems[:10]) + (f' ... and {len(problems) - 10} more' if len(problems) > 10 else ''))
    if not found:
        raise StepRefused(f'steps file {rel}: has no steps')
    return found


def describe_move(done: dict) -> str:
    src, dst, count = done['moved']
    text = f"moved {src} -> {dst} ({count} file{'' if count == 1 else 's'})"
    if done['removed']:
        text += '; removed emptied ' + ', '.join(done['removed'])
    return text


class Overlay:
    """The copy as it would be after the steps applied so far, without touching the disk: the real tree under a list of
    operations (a deletion, a move). Used to check a whole list of steps before the first runs, and by `steps check`."""

    def __init__(self, copy: str):
        self.real = RealFS(copy)
        self.ops: list[tuple] = []   # ('delete', path, step number) or ('move', source, target, step number)

    def _trace(self, rel: str, upto: int | None = None):
        """None when the path is gone, 'made' for a folder a move created on the way, else ('real', original path)."""
        for index in range(len(self.ops) - 1 if upto is None else upto - 1, -1, -1):
            op = self.ops[index]
            if op[0] == 'delete':
                if rel == op[1] or rel.startswith(op[1] + '/'):
                    return None
            else:
                _, src, dst, _ = op
                if rel == dst or rel.startswith(dst + '/'):
                    return self._trace(src + rel[len(dst):], index)
                if rel == src or rel.startswith(src + '/'):
                    return None
                if dst.startswith(rel + '/'):
                    return 'made'
        return ('real', rel)

    def kind(self, rel: str) -> str | None:
        found = self._trace(rel)
        if found is None:
            return None
        return 'dir' if found == 'made' else self.real.kind(found[1])

    def isdir(self, rel: str, follow: bool = True) -> bool:
        return self.kind(rel) == 'dir'

    def listdir(self, rel: str) -> list[str]:
        names: set[str] = set()
        found = self._trace(rel)
        for origin in {rel, found[1] if isinstance(found, tuple) else rel}:
            if self.real.kind(origin) == 'dir':
                names.update(self.real.listdir(origin))
        prefix = rel + '/' if rel else ''
        for op in self.ops:
            if op[0] == 'move' and op[2].startswith(prefix):
                names.add(op[2][len(prefix):].split('/')[0])
        return sorted(name for name in names if self.kind(prefix + name) is not None)

    def why_gone(self, rel: str) -> str:
        for op in reversed(self.ops):
            if op[0] == 'delete' and (rel == op[1] or rel.startswith(op[1] + '/')):
                return f' (step {op[2]} deleted {op[1]})'
            if op[0] == 'move' and (rel == op[1] or rel.startswith(op[1] + '/')):
                return f' (step {op[3]} moved {op[1]} to {op[2]})'
        return ''

    def why_there(self, rel: str) -> str:
        for op in reversed(self.ops):
            if op[0] == 'move' and (rel == op[2] or rel.startswith(op[2] + '/')):
                return f' (step {op[3]} moves {op[1]} there: two steps to one target)'
        return ''

    def apply_delete(self, number: int, patterns: list[str], optional: bool) -> str:
        chosen: list[str] = []
        skipped: list[str] = []
        for raw in patterns:
            matches = expand('', normalize(raw), self)
            if not matches:
                if optional:
                    skipped.append(raw)
                    continue
                raise StepRefused(f'{raw}: matches nothing' + (self.why_gone(normalize(raw)) or " in the node's copy"))
            for rel in matches:
                for part in _parents(rel) + [rel]:
                    if self.kind(part) == 'link':
                        raise StepRefused(f'{rel}: goes through a symbolic link ({part}); links are never followed '
                                          'or deleted by a step')
                chosen.append(rel)
        for rel in sorted(set(chosen)):
            self.ops.append(('delete', rel, number))
        return 'would delete ' + (', '.join(sorted(set(chosen))) or 'nothing') + ''.join(f'; {raw}: nothing matched' for raw in skipped)

    def apply_move(self, number: int, source: str, target: str) -> str:
        src, dst = validate_move(self, source, target)
        self.ops.append(('move', src, dst, number))
        removed = []
        here = posixpath.dirname(src)
        while here and self.kind(here) == 'dir' and not self.listdir(here):
            self.ops.append(('delete', here, number))
            removed.append(here)
            here = posixpath.dirname(here)
        return f'would move {src} -> {dst}' + ('; would remove emptied ' + ', '.join(removed) if removed else '')


def simulate(copy: str, step_list: list[dict], stop_at_first: bool = True) -> list[dict]:
    """Applies the steps, in order, to an overlay of `copy` and reports each: [{'number', 'text', 'status', 'note'}],
    status 'ok', 'refused' (note is the refusal) or 'unchecked' (after a `run` step the files are no longer known: only
    the form of later steps is checked). A refused step is skipped and the steps after it are still looked at, unless
    `stop_at_first`. Nothing on disk changes."""
    overlay = Overlay(copy)
    report: list[dict] = []
    opaque = False
    for number, step in enumerate(step_list, 1):
        entry = {'number': number, 'text': step['text'], 'status': 'ok', 'note': ''}
        report.append(entry)
        try:
            if step['word'] == 'run':
                opaque = True
                entry['note'] = 'would run the command; the files are not simulated past it'
            elif opaque:
                for raw in step.get('paths', []) + [step.get('src'), step.get('dst')]:
                    if raw is not None:
                        normalize(raw)
                entry['status'], entry['note'] = 'unchecked', 'form checked only (after a run step)'
            elif step['word'] == 'move':
                entry['note'] = overlay.apply_move(number, step['src'], step['dst'])
            else:
                entry['note'] = overlay.apply_delete(number, step['paths'], step['word'] == 'delete?')
        except StepRefused as refusal:
            entry['status'], entry['note'] = 'refused', str(refusal)
            if stop_at_first:
                break
    return report


CHECK_HELP = '''usage: python3 -m pangoloop steps check <repository> <steps file> [--format moves]

Looks at a steps file (what a script node's `steps_file` names) against a folder, and changes nothing. Prints, for each
step, what would happen or why it would be refused, and at the end the refusals (including collisions between steps:
two moves to one target, a move out of a folder an earlier step deleted or moved away, a move whose target lies inside
its own source). Steps after a `run` step are checked for form only: what a command does to the files is not known.
--format moves: every line is `<old><TAB><new>`. Exit status 0 when nothing would be refused, 1 when something would,
2 when the file or the arguments are wrong.
'''


def check_main(argv: list[str]) -> int:
    """`pangoloop steps check`: see CHECK_HELP."""
    import sys
    from pangoloop import plan
    args = list(argv)
    if not args or args[0] in ('-h', '--help'):
        print(CHECK_HELP)
        return 0 if args else 2
    form = ''
    if '--format' in args:
        at = args.index('--format')
        if at + 1 >= len(args):
            print('pangoloop steps check: --format needs a value (moves)', file=sys.stderr)
            return 2
        form = args[at + 1]
        del args[at:at + 2]
        if form not in plan.STEPS_FILE_FORMATS:
            print(f"pangoloop steps check: unknown format {form!r} (moves)", file=sys.stderr)
            return 2
    if len(args) != 2:
        print(CHECK_HELP, file=sys.stderr)
        return 2
    repository, path = args
    if not os.path.isdir(repository):
        print(f'pangoloop steps check: {repository} is not a folder', file=sys.stderr)
        return 2
    try:
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        print(f'pangoloop steps check: cannot read {path}: {error}', file=sys.stderr)
        return 2
    found, problems = plan.parse_steps_file(text, form)
    if problems or not found:
        for problem in problems or ['the file has no steps']:
            print(f'{path}: {problem}', file=sys.stderr)
        return 2
    report = simulate(repository, found, stop_at_first=False)
    print(f'{path} against {repository}: {len(found)} steps')
    for item in report:
        mark = {'ok': '   ', 'refused': 'NO ', 'unchecked': '?  '}[item['status']]
        print(f"{mark}{item['number']:>4}. {item['text']}\n         {item['note']}")
    refused = [item for item in report if item['status'] == 'refused']
    if refused:
        print(f'\n{len(refused)} of {len(found)} steps would be refused; a script node with this file would not start:')
        for item in refused:
            print(f"  step {item['number']} ({item['text']}): {item['note']}")
        return 1
    print(f'\nnothing would be refused; nothing was changed')
    return 0
