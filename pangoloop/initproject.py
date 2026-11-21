"""`pangoloop init`: makes a folder ready to be a project, asking nothing.

    pangoloop init [<folder>] [--preset claude-code-only|hermes-mixed|one-model] [--runner hermes|claude-code]
                   [--route <category>=<model@provider>]... [--reader <name>=<model@provider>]... [--force]

It writes `pangoloop.toml` from a named preset (the values are filled from the options, so that a program can pass what a person
answered), makes `.pangoloop/runs`, `.pangoloop/plans` and `.pangoloop/roles` with a short README in each, copies the example
roles into the roles folder (an existing role file is never replaced), adds `.pangoloop/runs/` to the project's `.gitignore`
(plans and roles are not ignored: a project may want them in git), and prints what it did and the next command. It refuses to
overwrite an existing `pangoloop.toml` without `--force`, writes nothing outside the folder, and runs no git command that
changes anything: in a folder that is not a git repository it says that a run needs one (`git init` is yours).
"""
import argparse
import json
import os
import re
import subprocess
import sys

from pangoloop import config, example_roles, project
from pangoloop.plan import PLACEHOLDER_MODELS

PRESET_NAMES = ('claude-code-only', 'hermes-mixed', 'one-model')

# What each preset says. The model names are the presets' own (the same as examples/presets/*.toml); nothing else in the
# package names a model. A placeholder (`default-model`, `review-model`, `ui-model`) stops a run that needs it, with exit 2.
PRESETS = {
    'claude-code-only': {
        'runner': 'claude-code',
        'about': 'Everything through Claude Code (`claude -p`). The names are Claude Code aliases: starting values, not advice.',
        'routes': {'default': 'sonnet', 'review': 'opus', 'ui': 'opus', 'simple-work': 'haiku'},
        'readers': {'quick': 'haiku', 'careful': 'opus'},
        'limits': {'max_turns': 100, 'node_limit_seconds': 3600, 'max_parallel': 3},
    },
    'hermes-mixed': {
        'runner': 'hermes',
        'about': 'The Hermes Agent runner with a mix of models. Every model@provider is a PLACEHOLDER: write ones you can call.',
        'routes': {'default': 'default-model@example-provider', 'review': 'review-model@example-provider',
                   'ui': 'ui-model@example-provider'},
        'readers': {'quick': 'reader-model-one@example-provider', 'careful': 'reader-model-two@example-provider'},
        'limits': {'max_parallel': 3, 'max_fix_rounds': 2, 'node_limit_seconds': 3600, 'max_turns': 200},
    },
    'one-model': {
        'runner': 'hermes',
        'about': 'The smallest setup: one model for every role. A reviewer that is the coder\'s own model agrees with it more '
                 'than it should; give the review route another model as soon as you can.',
        'routes': {'default': 'default-model@example-provider', 'review': 'default-model@example-provider',
                   'ui': 'default-model@example-provider'},
        'readers': {'quick': 'default-model@example-provider'},
        'limits': {'max_parallel': 2, 'max_turns': 100},
    },
}
PLACEHOLDER_NOTE = ('A route that still holds a placeholder (default-model, review-model, ui-model) stops a run that needs it, '
                    'with exit 2 and a sentence that names it.')

FOLDER_READMES = {
    'runs': ("# Runs\n\nOne folder per run, made by `pangoloop run`: the plan as run, the workers' prompts and answers, the state,\n"
             "`result.json`, the requests the nodes made. `pangoloop status` and `pangoloop view` read it.\n\n"
             "Git ignores this folder (`pangoloop init` added `.pangoloop/runs/` to `.gitignore`): it is large, it is a record\n"
             "of what happened, and the accepted work is in your repository, not here. Do not edit it by hand.\n"),
    'plans': ("# Plans\n\nPut the plans of this project here: one Markdown file per plan (`pangoloop run <name>` looks for\n"
              "`<name>`, `<name>.md` in this folder when the name is not a path). The format is explained at the top of the\n"
              "toy plan, `examples/toy/plan.md`; `pangoloop preflight <plan> --strict` checks one before a run.\n\n"
              "A node has a `kind` (what the engine does with it) and, for a node that has a model worker, a `role` (who the\n"
              "worker is: a file in `.pangoloop/roles/`).\n\n"
              "Git does NOT ignore this folder: plans are worth committing. If you would rather not, add the folder to\n"
              "your `.gitignore`.\n"),
    'roles': ("# Roles\n\nA role is who a worker is: a Markdown file `<name>.md` here. A small header between `---` lines says\n"
              "which kinds of node it may be used with, its category (which picks the model route) and a one-line\n"
              "description; the rest of the file is the prompt the engine puts before the node's task.\n\n"
              "    ---\n    kind: build\n    category: deep\n    description: one line for lists\n    ---\n    You are ...\n\n"
              "The files copied here by `pangoloop init` are examples; edit them or add your own, and use a role in a plan as\n"
              "`kind: build` and `role: <name>`. `pangoloop roles` lists them.\n\n"
              "Git does NOT ignore this folder: roles are worth committing. If you would rather not, add the folder to\n"
              "your `.gitignore`.\n"),
}


def quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def key(name: str) -> str:
    return name if re.fullmatch(r'[A-Za-z0-9_-]+', name) else quote(name)


def pair(value: str, flag: str) -> tuple[str, str]:
    name, equals, model = value.partition('=')
    if not equals or not name.strip() or not model.strip():
        raise SystemExit(f'pangoloop init: {flag} {value!r}: write it as <name>=<model@provider>')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name.strip()):
        raise SystemExit(f'pangoloop init: {flag} {value!r}: a name is letters, digits, dots, dashes and underscores')
    return name.strip(), model.strip()


def convert(model: str, runner: str) -> str:
    """A preset's own value for another runner: Claude Code takes the model alone, Hermes needs `model@provider`."""
    return model.rpartition('@')[0] if runner == 'claude-code' and '@' in model else model


def render(preset: str, runner: str | None, routes: list[str], readers: list[str]) -> str:
    base = PRESETS[preset]
    runner = runner or base['runner']
    table = {name: convert(model, runner) for name, model in base['routes'].items()}
    for item in routes:
        name, model = pair(item, '--route')
        table[name] = model
    people = {name: convert(model, runner) for name, model in base['readers'].items()}
    if readers:
        people = dict(pair(item, '--reader') for item in readers)
    lines = [f'# pangoloop.toml, made by `pangoloop init --preset {preset}`.', f'# {base["about"]}', f'# {PLACEHOLDER_NOTE}',
             '# `pangoloop config` prints what is in effect and where each value came from.', '',
             '[runner]', f'name = {quote(runner)}                 # PANGOLOOP_RUNNER wins over this', '',
             '[routes]                         # a node\'s category picks its route; `default` is every category not listed']
    lines += [f'{key(name)} = {quote(model)}' for name, model in table.items()]
    lines += ['', '[readers]                        # `pangoloop preflight --reader <name>`: a name you choose = model']
    lines += [f'{key(name)} = {quote(model)}' for name, model in people.items()]
    lines += ['', '[limits]', '# max_cost_usd = 5.0             # no new worker starts once the run\'s known cost is over this']
    lines += [f'{name} = {value}' for name, value in base['limits'].items()]
    lines += ['', '# Where things are kept, relative to this folder. These are the defaults; uncomment to move one.',
              '# [paths]', '# runs = ".pangoloop/runs"', '# plans = ".pangoloop/plans"', '# roles = ".pangoloop/roles"',
              '# [view]', '# port = 5420                    # else the first free port from 5420 upward', '']
    return '\n'.join(lines)


def git_facts(folder: str) -> tuple[str | None, bool]:
    """(the top folder of the repository the folder is in, or None; whether it has a commit)."""
    top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=folder, capture_output=True, text=True)
    if top.returncode:
        return None, False
    head = subprocess.run(['git', 'rev-parse', '--verify', '-q', 'HEAD'], cwd=folder, capture_output=True, text=True)
    return os.path.realpath(top.stdout.strip()), head.returncode == 0


IGNORED = ('.pangoloop/runs/', '.pangoloop/view.json', '.pangoloop/view.log')   # what the package writes there and git must not carry


def ignore_runs(folder: str) -> str:
    """Adds the runs folder and the page's two files (`.pangoloop/runs/`, `.pangoloop/view.json`, `.pangoloop/view.log`) to the
    folder's .gitignore (made when absent); a line that is there already is not added again, and the whole `.pangoloop` folder
    ignored covers all. Plans and roles are not ignored. What was done, as a sentence."""
    path = os.path.join(folder, '.gitignore')
    existing = open(path).read() if os.path.exists(path) else None
    have = {entry.strip().lstrip('/') for entry in (existing or '').splitlines()}
    if have & {'.pangoloop', '.pangoloop/'}:
        return '.gitignore already ignores .pangoloop/'
    missing = [line for line in IGNORED if line not in have and line.rstrip('/') not in have]
    if not missing:
        return '.gitignore already ignores .pangoloop/runs/ and the page\'s files'
    with open(path, 'a') as handle:
        if existing and not existing.endswith('\n'):
            handle.write('\n')
        handle.write(''.join(f'{line}\n' for line in missing))
    what = ', '.join(missing)
    return f'added {what} to .gitignore' if existing is not None else f'made .gitignore with {what} in it'


def init(folder: str, preset: str, runner: str | None, routes: list[str], readers: list[str], force: bool) -> dict:
    """Does the work; returns {'folder', 'actions': [...], 'notes': [...], 'next': [...]} or raises SystemExit(sentence)."""
    folder = os.path.realpath(folder)
    target = os.path.join(folder, project.FILE_NAME)
    if os.path.exists(target) and not force:
        raise SystemExit(f'pangoloop init: {target} exists already; nothing was changed. `--force` writes a new '
                         f'{project.FILE_NAME} over it (the roles and plans folders are left as they are)')
    text = render(preset, runner, routes, readers)
    os.makedirs(folder, exist_ok=True)
    previous = open(target).read() if os.path.exists(target) else None
    with open(target, 'w') as handle:
        handle.write(text)
    try:
        loaded = config.load(environ={'PANGOLOOP_CONFIG': target}, cwd=folder)   # a bad --route or --runner is found now
    except SystemExit as fault:
        if previous is None:
            os.remove(target)
        else:
            with open(target, 'w') as handle:
                handle.write(previous)
        raise SystemExit(f'pangoloop init: {fault.code}; nothing was changed') from None
    actions = [f'wrote {project.FILE_NAME} (preset {preset}' + (f', runner {runner}' if runner else '') + ')'
               + (' over the old one' if previous is not None else '')]
    for name in project.PATH_KEYS:
        path = project.folder(folder, name, {})
        os.makedirs(path, exist_ok=True)
        readme = os.path.join(path, 'README.md')
        if not os.path.exists(readme):
            with open(readme, 'w') as handle:
                handle.write(FOLDER_READMES[name])
        actions.append(f'made {os.path.relpath(path, folder)}/ with a README')
    made, kept = [], []
    for role_name, body in example_roles.ROLES.items():
        path = os.path.join(project.folder(folder, 'roles', {}), f'{role_name}.md')
        if os.path.exists(path):
            kept.append(role_name)
            continue
        with open(path, 'w') as handle:
            handle.write(body)
        made.append(role_name)
    if made:
        actions.append('copied the example roles: ' + ', '.join(made))
    if kept:
        actions.append('left your roles as they are: ' + ', '.join(kept))
    actions.append(ignore_runs(folder))
    notes = []
    top, has_commit = git_facts(folder)
    if top is None:
        notes.append('this folder is not a git repository. A run needs one: `git init` and a first commit are yours to make')
    elif top != folder:
        notes.append(f'this folder is inside the git repository {top}; a run uses the project folder as its repository, '
                     'so make the project at the repository root')
    elif not has_commit:
        notes.append('the repository has no commit yet; a run starts from one: commit your files first')
    notes.append('commit pangoloop.toml, .gitignore and, if you want them in git, the plans and roles, before a run '
                 '(untracked files are carried through a run as work in progress, then left as they were)')
    routes_now = {**loaded.routes, 'default': loaded.default_route}
    holders = sorted(name for name, (model, _) in routes_now.items() if model in PLACEHOLDER_MODELS)
    if holders:
        notes.append(f"the routes {', '.join(holders)} are still placeholders: pass --route <category>=<model@provider> or edit "
                     'pangoloop.toml before a run (a run that needs one stops with exit 2 and names it)')
    return {'folder': folder, 'actions': actions, 'notes': notes,
            'next': ['write or copy a plan into .pangoloop/plans/',
                     'pangoloop run <plan> --detach --view     # then: pangoloop status']}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='pangoloop init', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Makes a folder ready to be a project: pangoloop.toml, .pangoloop/{runs,plans,roles}, the example roles,\n'
                    'and .pangoloop/runs/ in .gitignore. Asks nothing; refuses to overwrite a pangoloop.toml without --force.',
        epilog='example:\n  pangoloop init --preset claude-code-only --route default=sonnet --route review=opus\n'
               '  pangoloop init ~/code/app --runner hermes --route default=a-model@a-provider --reader quick=a-model@a-provider')
    parser.add_argument('folder', nargs='?', default='.', help='the project folder (default: the current folder)')
    parser.add_argument('--preset', choices=PRESET_NAMES, default='one-model', help='default: one-model')
    parser.add_argument('--runner', choices=config.RUNNERS, help='the runner (default: the preset\'s)')
    parser.add_argument('--route', action='append', default=[], metavar='CATEGORY=MODEL@PROVIDER',
                        help='set a route (repeatable); `default` is every category not listed')
    parser.add_argument('--reader', action='append', default=[], metavar='NAME=MODEL@PROVIDER',
                        help='set the readers of `preflight` (repeatable; replaces the preset\'s readers)')
    parser.add_argument('--force', action='store_true', help='write pangoloop.toml over an existing one')
    parser.add_argument('--json', action='store_true', help='print the result as one JSON object: folder, actions, notes, next')
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    result = init(args.folder, args.preset, args.runner, args.route, args.reader, args.force)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    print(f"pangoloop init: project {result['folder']}")
    for line in result['actions']:
        print(f'  {line}')
    for line in result['notes']:
        print(f'  note: {line}')
    print('next:')
    for line in result['next']:
        print(f'  {line}')
    return 0
