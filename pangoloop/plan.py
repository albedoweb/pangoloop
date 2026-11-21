"""The plan format, in one place: what the engine (graph.py) and the viewer (view.py) both read.

Moved here from the two files on November 18, 2026 (step 2), where each had its own copy: the routes, the node
fields, the patterns that find a verdict, a finding and an asking line, the default number of fix rounds, and the
parser. The engine's parser is strict (a plan fault stops the run); the viewer's is tolerant (`strict=False`: a missing
field is an empty string, a missing `## Nodes` is no nodes), because it must show whatever is in a folder.
"""
import re
import shlex

from pangoloop.roles import KINDS, NO_WORKER_KINDS, WORKER_KINDS

MAX_REPAIRS = 2   # oh-my-hermes: two repairs per node
DEFAULT_MAX_FIX_ROUNDS = MAX_REPAIRS   # SG_MAX_FIX_ROUNDS defaults to this; the viewer assumes it for a run with no result.json

TOOLSETS = 'file,terminal,omh-lsp'   # what a delegated child has: the lead's tool sets without delegation and omh
# category -> (model, provider) for the Hermes runner. PLACEHOLDERS: no model name is built in; a project names its own
# in `[routes]` of the configuration file (see examples/presets/). Without them a Hermes run asks Hermes
# for a model called `default-model`, which it will refuse.
ROUTES = {'review': ('review-model', 'example-provider'),
          'ui': ('ui-model', 'example-provider')}   # 'ui': interface nodes
DEFAULT_ROUTE = ('default-model', 'example-provider')
# category -> tool sets; every category not listed has TOOLSETS ('ui' gets vision, added 2026-11-17)
CATEGORY_TOOLSETS = {'ui': TOOLSETS + ',vision'}
# The models of the placeholder routes: a run whose nodes resolve to one of them stops before it starts (exit 2).
PLACEHOLDER_MODELS = frozenset({'default-model', 'review-model', 'ui-model'})


def is_placeholder(route: tuple[str, str]) -> bool:
    return route[0] in PLACEHOLDER_MODELS


def kind_of(node: dict) -> str:
    """What the engine does with a node: its `kind`; for a node that has only `role` (a plan written before kinds, or a dict
    made by hand) the role word, which was the kind then."""
    return node.get('kind') or node.get('role') or ''


def route_name(category: str, routes: dict) -> str:
    """The key of `[routes]` that decides the model of a category: the category itself, else `default`."""
    return category if category in routes else 'default'


def role_of(category: str, routes: dict) -> str:
    """The role a worker plays, from the category of its route (never from its model): `coder` (the route is the default
    one), `reviewer` (`review`), `interface` (`ui`), `script`, `reader`, and `other` for a category the configuration
    routes on its own. The page colours by this."""
    if category in ('script', 'reader'):
        return category
    if category == 'review':
        return 'reviewer'
    if category == 'ui':
        return 'interface'
    return 'other' if category in routes else 'coder'


def placeholder_routes(nodes: list[dict], routes: dict, default: tuple[str, str]) -> list[str]:
    """The `[routes]` keys that a plan needs and that are still placeholders, in the order the plan meets them. A node
    that has no model (a script node, a `human` node) needs none; a `review` node uses `review`; an `acceptance` node
    may need the default route for the repair of a failed acceptance."""
    needed: list[str] = []
    for node in nodes:
        role = kind_of(node)
        if role in ('script', 'human'):
            continue
        for category in ((node['category'],) if role != 'acceptance' else (node['category'], 'deep')):
            if role == 'review':
                category = node.get('route_category') or 'review'
            key = route_name(category, routes)
            if key not in needed and is_placeholder(routes.get(category, default)):
                needed.append(key)
    return needed

CONTRACT = ('TASK', 'DELIVERABLE', 'SCOPE', 'VERIFY', 'STOP WHEN')
REQUIRED = ('label', 'category', 'role', 'depends_on', 'write_scope') + CONTRACT   # of every node but a script node
# (`role` is satisfied by a `kind` instead; a node with a `kind` may leave out `category`: the role's, else a default, is used)
# A node with `role: script` is done by the engine: it has STEPS in place of TASK, and needs only these fields; the
# category and the four other contract lines are optional for it (category defaults to `script`, the rest to '').
SCRIPT_REQUIRED = ('label', 'role', 'depends_on', 'write_scope')   # and STEPS or steps_file (or both)
STEPS_FILE_FORMATS = ('moves',)   # `steps_file_format: moves`: every line is `<old><TAB><new>`, one `move` each
FIELDS = REQUIRED + ('kind', 'STEPS', 'steps_file', 'steps_file_format', 'may_be_empty')
NO_SCOPE = ('nothing', 'none')   # a write_scope that says the node writes nothing (case and a final full stop ignored)
STEP_WORDS = ('delete', 'delete?', 'move', 'run')

# A finding is a line that starts with "FINDING <number>:", after optional indentation,
# one list marker and bold marks.
FINDING_LINE = r'(?m)^[ \t]*(?:[-*>][ \t]*)?(?:\*\*|__)?FINDING +\d+ *:'
VERDICT = r'VERDICT:\s*(PASS|CHANGES REQUESTED)'
# The last line of a node's answer when it stops to ask: `NEEDS HUMAN: <question>` (for the person, and only the
# person) or `NEEDS COORDINATOR: <question>` (for the coordinator, the one who wrote the plan; a person may step in).
ASK_LINE = r'(?m)^[ \t]*(?:[-*>][ \t]*)?(?:\*\*|__)?NEEDS (HUMAN|COORDINATOR):(?:\*\*|__)?[ \t]*'
FOR_PERSON, FOR_COORDINATOR = 'person', 'coordinator'
COMMANDS_HEADING = r"(?m)^##[ \t]+The person['\u2019]s commands[ \t]*\n(.*?)(?=^##[ \t]|\Z)"


# A closing line whose question is empty, or starts with one of these words, is not a request (a node ended its handoff
# with `NEEDS COORDINATOR: none - part C is complete` and the engine stopped it and waited; November 18, 2026).
NOT_A_QUESTION = re.compile(r"(?is)^(?:none|nothing|n/a|not needed|no questions?)(?!\w)")


def is_empty_question(text: str | None) -> bool:
    """True for a question that is empty after trimming punctuation, or starts with `none`, `nothing`, `n/a`,
    `not needed`, `no question` or `no questions` as a whole word, in any letter case."""
    trimmed = re.sub(r'^[\W_]+', '', text or '')
    return not trimmed.strip() or bool(NOT_A_QUESTION.match(trimmed))


def find_request(answer: str | None) -> tuple[str, str] | None:
    """The request of the last asking line of an answer, with whatever follows it: (for, text), `for` being 'person' or
    'coordinator'; None when there is no such line, or when its question is empty or `none` (see is_empty_question)."""
    found = list(re.finditer(ASK_LINE, answer or ''))
    if not found:
        return None
    last = found[-1]
    kind = FOR_COORDINATOR if last.group(1) == 'COORDINATOR' else FOR_PERSON
    text = answer[last.end():].strip()
    return None if is_empty_question(text) else (kind, text)


def ignored_request(answer: str | None) -> str | None:
    """The text of a closing asking line that find_request ignores (an empty or `none` question), else None."""
    found = list(re.finditer(ASK_LINE, answer or ''))
    if not found:
        return None
    text = answer[found[-1].end():].strip()
    return text if is_empty_question(text) else None


WORKER_KINDS = ('node', 'repair', 'rebase-repair', 'review', 'acceptance-repair')


def worker_kind(worker: str | None, nodes: list[dict]) -> str | None:
    """What a worker is, from its tag (`17-review-fix1`) and the plan's nodes: 'node' (the node's own worker), 'review'
    (the worker of a review node), 'repair' (`<node>-fix<k>`: a fix after a review), 'acceptance-repair' (the same of an
    acceptance node), 'rebase-repair' (`<node>-rebase<k>`). None when there is no worker (a gate for the person)."""
    if not worker:
        return None
    name = re.sub(r'^\d+-', '', worker)
    roles = {node['id']: kind_of(node) for node in nodes}
    if name in roles:
        return 'review' if roles[name] == 'review' else 'node'
    found = re.match(r'^(.*)-(rebase|fix)\d+$', name)
    if found and found.group(1) in roles:
        if found.group(2) == 'rebase':
            return 'rebase-repair'
        return 'acceptance-repair' if roles[found.group(1)] == 'acceptance' else 'repair'
    return 'node'


def person_commands(plan: str) -> str:
    """The text of the optional section `## The person's commands` of a plan ('' when there is none): free text, shown
    to every node with the asking instructions and by the viewer on a request for the person. Not enforced."""
    found = re.search(COMMANDS_HEADING, plan or '', re.S)
    return found.group(1).strip() if found else ''


def must_change(node: dict) -> bool:
    """True for a node whose work must not be an empty change: role `build`, a write_scope that is not `nothing` (or
    `none`), and no `may_be_empty: yes`."""
    scope = (node.get('write_scope') or '').strip().rstrip('.').strip().lower()
    return (kind_of(node) == 'build' and scope not in NO_SCOPE
            and (node.get('may_be_empty') or '').strip().lower() != 'yes')


def split_steps(text: str) -> list[str]:
    """The steps of a STEPS field: separated by ` ; `, except inside backticks (a `run` command may contain ` ; `)."""
    steps, current, inside = [], '', False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '`':
            inside = not inside
        if not inside and text.startswith(' ; ', index):
            steps.append(current.strip())
            current = ''
            index += 3
            continue
        current += char
        index += 1
    steps.append(current.strip())
    return steps


def step_problems(raw: str) -> list[str]:
    """Every fault in the text of ONE step, one message each (an empty list when it is sound): the step is empty; its
    word is unknown; it has no argument (a `run` with no command, also an empty pair of backticks); a backtick is left
    open (an odd number of them); a semicolon stands outside the backticks of a step that has backticks (`run `a`; run `b``
    is ONE step for the engine, because steps are separated by space, semicolon, space; the shell then fails at run
    time). A `run` command without backticks, and a semicolon inside backticks, are legal."""
    raw = raw.strip()
    if not raw:
        return ['the step is empty (steps are separated by " ; ")']
    word, rest = (re.split(r'[ \t]+', raw, maxsplit=1) + [''])[:2]
    rest = rest.strip()
    found = []
    if word not in STEP_WORDS:
        found.append(f"unknown step word {word!r} (the words are {', '.join(STEP_WORDS)})")
    elif not rest or (rest.count('`') % 2 == 0 and '`' in rest and not rest.replace('`', '').strip()):
        found.append('the step (run) has no command' if word == 'run' else f'the step ({word}) has no argument')
    if raw.count('`') % 2:
        found.append('unbalanced backtick: a backtick is opened and never closed')
    elif '`' in raw:
        outside, inside = '', False
        for char in raw:
            if char == '`':
                inside = not inside
            elif not inside:
                outside += char
        if ';' in outside:
            found.append('a semicolon outside the backticks: steps are separated by space, semicolon, space (" ; "), '
                         'so this is ONE step and the shell would fail on it; put a space on both sides of the semicolon '
                         'that separates steps')
    return found


def parse_step(raw: str) -> dict:
    """One step from its words: {'word', 'text', and 'paths' (delete), 'src' and 'dst' (move) or 'command' (run)}.
    ValueError(reason) when the step is empty, has an unknown word, lacks or has too many arguments, or is badly quoted."""
    raw = raw.strip()
    found = step_problems(raw)
    if found:
        raise ValueError('; '.join(found))
    word, rest = (re.split(r'[ \t]+', raw, maxsplit=1) + [''])[:2]   # a space or a tab after the word
    rest = rest.strip()
    if word == 'run':
        command = rest[1:-1].strip() if len(rest) > 2 and rest[0] == rest[-1] == '`' else rest
        return {'word': word, 'text': raw, 'command': command}
    try:
        paths = shlex.split(rest)
    except ValueError as error:
        raise ValueError(f'the step ({word}): {error}') from None
    if word == 'move':
        if len(paths) != 2:
            raise ValueError(f'the step (move) takes exactly two paths, <from> <to>, and has {len(paths)}')
        return {'word': word, 'text': raw, 'src': paths[0], 'dst': paths[1]}
    return {'word': word, 'text': raw, 'paths': paths}


def parse_steps(node_id: str, text: str) -> list[dict]:
    """Each step of a STEPS field (see parse_step). SystemExit(message) on a step that does not parse: a plan fault, found
    before anything runs; the message has one line per finding, each naming the node and the step."""
    steps, lines = [], []
    for number, raw in enumerate(split_steps(text), 1):
        problems = step_problems(raw)
        label = f'node {node_id}: step {number} of STEPS' + (f' ({raw.strip()})' if raw.strip() else '')
        lines += [f'{label}: {problem}' for problem in problems]
        if not problems:
            try:
                steps.append(parse_step(raw))
            except ValueError as error:   # what only the argument parser sees: a badly quoted path, a wrong count
                lines.append(f'{label}: {error}')
    if lines:
        raise SystemExit('\n'.join(lines))
    return steps


def parse_steps_file(text: str, form: str = '') -> tuple[list[dict], list[str]]:
    """The steps of a steps file: (steps, problems). One step per line in the words of a STEPS field; blank lines and lines
    starting with `#` are skipped; tabs or spaces separate arguments, a path with spaces is quoted. With `form` 'moves'
    each line is `<old><TAB><new>` (a move; the paths may contain spaces; no quoting). Every line is looked at, so
    `problems` lists them all as 'line N: why'."""
    steps: list[dict] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        try:
            if form == 'moves':
                fields = line.rstrip('\r\n').split('\t')
                if len(fields) != 2 or not fields[0].strip() or not fields[1].strip():
                    raise ValueError('a moves line is <old><TAB><new>: exactly two non-empty fields separated by a tab')
                old, new = fields[0].strip(), fields[1].strip()
                steps.append({'word': 'move', 'text': f'move {shlex.quote(old)} {shlex.quote(new)}', 'src': old, 'dst': new})
            else:
                steps.append(parse_step(stripped))
        except ValueError as error:
            problems.append(f'line {number}: {error}')
    return steps, problems


def resolve_kind(node: dict, roles, strict: bool) -> dict:
    """Separates what the engine does (`kind`) from who the worker is (`role`) for one parsed node (fields as written), and
    returns the facts: kind, role, legacy (True for a node with only `role: <one of the six kinds>`: the kind is that word and
    the role is the built-in one, which adds nothing to the prompt), the Role object (when `roles` is given and the role is
    named), and a fault sentence (strict only; the caller raises it)."""
    given_kind, given_role = node.get('kind'), node.get('role')
    out = {'kind': '', 'role': given_role or '', 'legacy': False, 'role_object': None, 'fault': None}
    if given_kind is not None:
        if given_kind not in KINDS:
            out['fault'] = f"node {node['id']}: kind {given_kind!r} is not one of {', '.join(KINDS)}"
        out['kind'] = given_kind
    elif given_role in KINDS:
        out.update(kind=given_role, legacy=True)
    elif given_role:
        found = roles.get(given_role) if roles is not None else None
        if found is not None and len(found.kinds) == 1:
            out['kind'] = found.kinds[0]   # a role file for one kind says it, so a plan may leave `kind` out
        elif found is not None:
            out['kind'] = 'baseline'
            out['fault'] = (f"node {node['id']}: the role {given_role!r} serves kinds {', '.join(found.kinds)}; "
                            'say which with a `kind:` line')
        elif roles is not None and given_role in getattr(roles, 'problems', {}):
            out['kind'] = 'baseline'
            out['fault'] = roles.unknown(given_role, node['id'])
        else:
            # Before kinds any other word was a plain model session: still so (it is the kind `baseline`, the built-in role),
            # so that plans written then run as they did; a plan written now says `kind:` and gets a checked role.
            out.update(kind='baseline', legacy=True, other_word=True)
    if out['fault'] or not given_role or out['legacy']:
        return out
    if out['kind'] in NO_WORKER_KINDS:
        out['fault'] = (f"node {node['id']}: a node of kind {out['kind']} has no model worker, so it has no role "
                        f"(it says role {given_role!r}); remove the role line")
    elif roles is not None:
        found = roles.get(given_role)
        if found is None:
            out['fault'] = roles.unknown(given_role, node['id']) if hasattr(roles, 'unknown') \
                else f"node {node['id']}: unknown role {given_role!r}"
        elif not found.allows(out['kind']):
            out['fault'] = (f"node {node['id']}: the role {given_role!r} is for kind {', '.join(found.kinds)} ({found.path}), "
                            f"not for {out['kind']}")
        else:
            out['role_object'] = found
    return out


def parse_nodes(plan: str, strict: bool = True, roles=None) -> list[dict]:
    """The nodes of a plan: the blocks `### <id>` between `## Nodes` and `## Task statement`, with their fields.

    strict: a missing field or an unknown dependency stops with SystemExit(message), and so does a plan without
    `## Nodes`. Not strict: a missing field is '', nothing stops.

    `roles` (pangoloop/roles.py: name -> Role, the roles folder's) lets the parser check a node's role and take its category and
    prompt; with None a role named by a node with a `kind` is accepted unchecked. Each node gets `kind` (what the engine does),
    `role` (who the worker is; '' for none), `legacy`, `role_prompt`, `role_description`, `route_category` (the category that
    selects the model: `review` for a review node of a plan written before kinds, else the node's category) and `category`."""
    if '\n## Nodes\n' not in plan:
        if strict:
            raise SystemExit("the plan has no '## Nodes' section")
        return []
    body = plan.split('\n## Nodes\n', 1)[1].split('\n## Task statement', 1)[0]
    body = re.split(r"\n##[ \t]+The person['\u2019]s commands[ \t]*\n", body, maxsplit=1)[0]   # a section left among the nodes
    nodes = []
    step_faults = []
    for block in re.split(r'\n(?=### )', '\n' + body):
        head = re.match(r'\s*### (\S+)\s*\n', block)
        if not head:
            continue
        node = {'id': head.group(1)}
        for line in block.splitlines()[1:]:
            found = re.match(r'- ([A-Za-z_ ]+?): (.*)$', line)
            if found and found.group(1) in FIELDS:
                node[found.group(1)] = found.group(2).strip()
        facts = resolve_kind(node, roles, strict)
        if strict and facts['fault']:
            raise SystemExit(facts['fault'])
        kind, role_object = facts['kind'], facts['role_object']
        script = kind == 'script'
        has_steps = 'STEPS' in node or 'steps_file' not in node
        if strict:
            named = 'role' in node or 'kind' in node
            optional = {'role'} if 'kind' in node else set()
            if not facts['legacy']:
                optional.add('category')   # the role's category, else the default for the kind
            missing = [field for field in (SCRIPT_REQUIRED if script else REQUIRED)
                       if field not in node and field not in optional]
            if not named and 'role' not in missing:
                missing.append('role')
            if script and 'STEPS' not in node and 'steps_file' not in node:
                missing.append('STEPS or steps_file')
            if missing:
                raise SystemExit(f"node {node['id']} lacks {missing}")
            for field in ('STEPS', 'steps_file', 'steps_file_format'):
                if not script and field in node:
                    raise SystemExit(f"node {node['id']} has {field} but its role is {node['role']!r}, not 'script'"
                                     if facts['legacy'] else f"node {node['id']} has {field} but its kind is {kind!r}, not 'script'")
            if node.get('steps_file_format') and 'steps_file' not in node:
                raise SystemExit(f"node {node['id']} has steps_file_format but no steps_file")
            if node.get('steps_file_format', 'moves') not in STEPS_FILE_FORMATS:
                raise SystemExit(f"node {node['id']}: steps_file_format {node['steps_file_format']!r} is unknown "
                                 f"(the format is {', '.join(STEPS_FILE_FORMATS)}; leave it out for one step per line)")
            if node.get('may_be_empty', 'yes').lower() not in ('yes', 'no'):
                raise SystemExit(f"node {node['id']}: may_be_empty is {node['may_be_empty']!r}; write yes or no")
        given_category = node.get('category')
        if script:
            node.setdefault('category', 'script')
        elif not given_category and role_object is not None and role_object.category:
            node['category'] = role_object.category
        elif not given_category and not facts['legacy']:
            node['category'] = 'review' if kind == 'review' else 'default'
        for field in FIELDS:
            node.setdefault(field, '')
        node.update(kind=kind, role=facts['role'], legacy=facts['legacy'], other_word=bool(facts.get('other_word')),
                    role_prompt=role_object.prompt if role_object else '',
                    role_description=role_object.description if role_object else '',
                    role_file=role_object.path if role_object else '',
                    route_category='review' if (facts['legacy'] and kind == 'review') else node['category'])
        if script:
            try:
                node['steps'] = parse_steps(node['id'], node['STEPS']) if has_steps else []
            except SystemExit as fault:
                if strict:
                    step_faults.append(str(fault.code))
                node['steps'] = []
        dep = node['depends_on']
        node['deps'] = [] if dep in ('', 'none') else [x.strip() for x in dep.split(',') if x.strip()]
        nodes.append(node)
    if step_faults:
        raise SystemExit('\n'.join(step_faults))
    if strict:
        ids = {node['id'] for node in nodes}
        for node in nodes:
            unknown = [dep for dep in node['deps'] if dep not in ids]
            if unknown:
                raise SystemExit(f"node {node['id']} depends on unknown nodes {unknown}")
    return nodes
