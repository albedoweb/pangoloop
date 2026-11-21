"""Roles: who a worker is. A role is a prompt in a file, `<roles folder>/<name>.md`; the list is open.

A node of a plan has a `kind` and, when its kind has a model worker, a `role`:

  kind   what the ENGINE does with the node: a closed list (`KINDS`), six words.
  role   who the WORKER is: a name the plan chooses, defined by a file in the roles folder.

The kinds with a worker are `build` (a session in its own git worktree, then an integration), `review` (a session that reads the
whole change, with repair rounds) and `baseline` (a session that looks and changes nothing). `script`, `acceptance` and `human`
have no model worker, so they have no role.

A role file is a small header and then the prompt:

    ---
    kind: build            # which kinds of node it may be used with: build, review, or both (build, review)
    category: ui           # optional: the route's category, which selects the model; a node's own `category` wins
    description: one line, shown in lists
    ---
    The text the engine puts into the worker's prompt, before the node's task.

Where the prompt goes (the same place for every role, `Run.prompt` in pangoloop/graph.py): after the line that names the node
and before the node's contract lines (TASK, DELIVERABLE, SCOPE, VERIFY, STOP WHEN), as

    Your role: <name> (<description>).
    <the prompt, with {{REPO}} and {{BRANCH}} filled as in the plan: the folder the node works in and its branch>

and, for a node of kind `review`, after it the engine's own paragraph (`REVIEW_CONTRACT`): the closing line `VERDICT: PASS` or
`VERDICT: CHANGES REQUESTED` and the `FINDING <n>:` lines, which is what the engine reads a verdict from. That paragraph is not in
any role file, so a role cannot break it. Nothing else is in a role: no tools, no limits; those stay the node's and the
configuration's.

Plans written before kinds existed have `role: build` (and so on) and no `kind`: a node whose `role` is one of the six words and
that has no `kind` is read as that kind with the built-in role of the same name, whose prompt is empty, so its worker gets what it
always got (`pangoloop/plan.py`).
"""
import os
import re
import sys

KINDS = ('baseline', 'build', 'script', 'acceptance', 'review', 'human')
NO_WORKER_KINDS = ('script', 'acceptance', 'human')
WORKER_KINDS = ('baseline', 'build', 'review')           # the kinds that have a model worker, and so may have a role
ROLE_KINDS = ('build', 'review')                         # what a role file may say in `kind:` (a baseline uses a build role)
HEADER_KEYS = ('kind', 'category', 'description')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
SKIPPED = ('readme.md',)                                 # the folder's own README is not a role

# What the engine adds for a node of kind `review`, whatever the role says (the engine reads the verdict from the answer with
# plan.VERDICT and counts the findings with plan.FINDING_LINE).
REVIEW_CONTRACT = ('How to close your review (the engine reads it, so keep to it): list every finding on a line of its own that '
                   'starts with `FINDING 1:`, `FINDING 2:` and so on, each naming where the problem is and what is wrong. Then '
                   'end your answer with one line, `VERDICT: PASS` when there is no finding that must be fixed, or '
                   '`VERDICT: CHANGES REQUESTED` when there is one. An answer without a VERDICT line is not read as a review.')


class RoleFault(Exception):
    """A role file that cannot be used; the message names the file."""


class Role:
    def __init__(self, name: str, kinds: tuple[str, ...], category: str | None, description: str, prompt: str, path: str) -> None:
        self.name, self.kinds, self.category, self.description, self.prompt, self.path = \
            name, kinds, category, description, prompt, path

    def allows(self, kind: str) -> bool:
        """A role of kind `build` also serves a `baseline` node (a session that looks and changes nothing)."""
        return ('build' if kind == 'baseline' else kind) in self.kinds


class RoleSet(dict):
    """name -> Role for the roles of a folder that parsed; `problems` maps the name of a file that did not to its message."""

    def __init__(self, folder: str) -> None:
        super().__init__()
        self.folder = folder
        self.problems: dict[str, str] = {}

    def unknown(self, name: str, node_id: str) -> str:
        found = ', '.join(sorted(self)) or 'none'
        text = (f'node {node_id}: unknown role {name!r}. The roles are the files <name>.md in {self.folder}; '
                f'it has: {found}.')
        if name in self.problems:
            text += f' ({name}.md exists but cannot be used: {self.problems[name]})'
        return text + ' Add a file there (`pangoloop roles` lists them), or use a role that exists.'


def parse_role(name: str, text: str, path: str) -> Role:
    """The role in a file's text, or RoleFault."""
    lines = text.replace('\r\n', '\n').split('\n')
    if not lines or lines[0].strip() != '---':
        raise RoleFault('the file must begin with a header: a line `---`, lines `key: value` (kind, category, description) and a '
                        'closing `---`')
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == '---')
    except StopIteration:
        raise RoleFault('the header is not closed: there is no second `---` line') from None
    header: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], 2):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, colon, value = line.partition(':')
        key = key.strip()
        if not colon or key not in HEADER_KEYS:
            raise RoleFault(f'header line {number} ({line.strip()!r}): write `key: value` with a key from {", ".join(HEADER_KEYS)}')
        if key in header:
            raise RoleFault(f'header line {number}: {key} is given twice')
        header[key] = value.strip()
    if 'kind' not in header:
        raise RoleFault('the header has no `kind:` (build, review, or both: `kind: build, review`)')
    kinds = tuple(dict.fromkeys(part for part in re.split(r'[\s,]+', header['kind']) if part))
    wrong = [kind for kind in kinds if kind not in ROLE_KINDS]
    if not kinds or wrong:
        raise RoleFault(f'kind {header["kind"]!r}: a role is used with the kinds {", ".join(ROLE_KINDS)} (one or both)')
    category = header.get('category') or None
    if category is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', category):
        raise RoleFault(f'category {category!r}: letters, digits, dots, dashes and underscores')
    prompt = '\n'.join(lines[end + 1:]).strip()
    if not prompt:
        raise RoleFault('the file has a header but no prompt after it')
    return Role(name, kinds, category, header.get('description', ''), prompt, path)


def load(folder: str) -> RoleSet:
    """Every role of the folder (a missing folder has none). A file that cannot be used is in `problems`, and stops only a
    plan that names it."""
    found = RoleSet(folder)
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return found
    for entry in entries:
        path = os.path.join(folder, entry)
        if not entry.endswith('.md') or entry.lower() in SKIPPED or not os.path.isfile(path):
            continue
        name = entry[:-3]
        try:
            if not NAME.fullmatch(name):
                raise RoleFault('the name of a role is letters, digits, dots, dashes and underscores')
            with open(path, encoding='utf-8') as handle:
                found[name] = parse_role(name, handle.read(), path)
        except RoleFault as fault:
            found.problems[name] = str(fault)
        except (OSError, UnicodeDecodeError) as error:
            found.problems[name] = f'cannot be read: {error}'
    return found


def fill(prompt: str, repo: str, branch: str) -> str:
    """The placeholders a role may use, as the engine fills them in a plan: {{REPO}} and {{BRANCH}}."""
    return prompt.replace('{{REPO}}', repo).replace('{{BRANCH}}', branch)


def section(node: dict, repo: str, branch: str) -> str:
    """The text a node's prompt gets at the role's place ('' for a node that gets none: a plan written before kinds, with the
    built-in role of its kind)."""
    parts = []
    if node.get('role_prompt'):
        head = f"Your role: {node['role']}" + (f" ({node['role_description']})" if node.get('role_description') else '') + '.'
        parts.append(head + '\n' + fill(node['role_prompt'], repo, branch))
    if node.get('kind') == 'review' and not node.get('legacy'):
        parts.append(REVIEW_CONTRACT)
    return '\n\n'.join(parts)


def main(argv: list[str]) -> int:
    """`pangoloop roles` lists the project's roles; `pangoloop roles show <name>` prints one."""
    import argparse
    from pangoloop import paths
    parser = argparse.ArgumentParser(prog='pangoloop roles', formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description='The roles of the project: who a worker is, one file <name>.md each in the roles '
                                                 'folder (.pangoloop/roles by default). `kind` says which kinds of node use it.',
                                     epilog='example:\n  pangoloop roles\n  pangoloop roles show reviewer\n  pangoloop roles --json')
    parser.add_argument('words', nargs='*', help='`show <name>` prints one role')
    parser.add_argument('--json', action='store_true', help='print the list as one JSON object: folder, roles, problems')
    args = parser.parse_args(argv)
    folder = paths.roles_folder()
    found = load(folder)
    if args.words[:1] == ['show']:
        if len(args.words) != 2:
            parser.error('usage: pangoloop roles show <name>')
        role = found.get(args.words[1])
        if role is None:
            print(found.unknown(args.words[1], '-').replace('node -: ', 'pangoloop: '), file=sys.stderr)
            return 2
        print(f'{role.path}\nname: {role.name}\nkind: {", ".join(role.kinds)}\ncategory: {role.category or "-"}\n'
              f'description: {role.description or "-"}\n\n{role.prompt}')
        return 0
    if args.words:
        parser.error('usage: pangoloop roles [show <name>] [--json]')
    if args.json:
        import json
        print(json.dumps({'folder': folder, 'roles': [{'name': r.name, 'kinds': list(r.kinds), 'category': r.category,
                                                       'description': r.description, 'file': r.path} for r in found.values()],
                          'problems': found.problems}, indent=1))
        return 0
    print(f'roles folder: {folder}')
    if not found and not found.problems:
        print('(no roles: `pangoloop init` copies the examples here, or write <name>.md files)')
    for role in found.values():
        print(f'  {role.name:<22} {", ".join(role.kinds):<13} {role.category or "-":<9} {role.description}  [{os.path.relpath(role.path, folder)}]')
    for name, problem in found.problems.items():
        print(f'  {name:<22} NOT USABLE: {problem}')
    print('built-in (plans with `role: baseline|build|review` and no `kind`): the same name, no extra prompt')
    return 0
