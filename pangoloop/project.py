"""The project: the folder that holds a project's configuration, plans, roles and runs.

A project is a git repository with a `pangoloop.toml` at its root. Every command that works on a project finds it the same
way, in this order: `--project <path>` (a command-line option of every command), the variable PANGOLOOP_PROJECT, then the
first folder with a `pangoloop.toml` on the way up from the current folder (the search stops at the filesystem root and at
the user's home folder, which is never itself taken for a project). The result is one folder; everything the package keeps
for the project lies under it:

    <project>/pangoloop.toml          the configuration
    <project>/.pangoloop/runs/        one folder per run (Git-ignored by `pangoloop init`)
    <project>/.pangoloop/plans/       plans, kept in git if you want
    <project>/.pangoloop/roles/       roles (a prompt per file), kept in git if you want
    <project>/.pangoloop/view.json    the page that serves this project: pid, port, start (removed when it stops)
    <project>/.pangoloop/view.log     the page's own output when it was started in the background

Each folder can be moved with `[paths]` in the configuration file (`runs`, `plans`, `roles`; relative to the project, or
absolute); the variables PANGOLOOP_RUNS, PANGOLOOP_PLANS and PANGOLOOP_ROLES win over the file. `apply` puts the choice into
the environment, where the engine, the page and the other commands read it (pangoloop/paths.py), so a command that never heard of
projects sees only the variables it always read.
"""
import os
import tomllib

FILE_NAME = 'pangoloop.toml'
STATE_FOLDER = '.pangoloop'
PATH_KEYS = ('runs', 'plans', 'roles')
VARIABLES = {'runs': 'PANGOLOOP_RUNS', 'plans': 'PANGOLOOP_PLANS', 'roles': 'PANGOLOOP_ROLES'}


def find(start: str | None = None, home: str | None = None) -> str | None:
    """The first folder with a `pangoloop.toml` from `start` (default: the current folder) upward; None when there is none
    before the filesystem root or the home folder."""
    folder = os.path.realpath(start or os.getcwd())
    home = os.path.realpath(home or os.path.expanduser('~'))
    while True:
        if folder == home:
            return None
        if os.path.isfile(os.path.join(folder, FILE_NAME)):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def resolve(explicit: str | None = None, environ: dict | None = None, start: str | None = None) -> str | None:
    """The project's root: `explicit` (--project), else PANGOLOOP_PROJECT, else the search from `start`. A named project
    that has no `pangoloop.toml` is an error (SystemExit with the sentence); no project found is None."""
    environ = os.environ if environ is None else environ
    named = explicit or environ.get('PANGOLOOP_PROJECT', '').strip()
    if named:
        root = os.path.realpath(os.path.expanduser(named))
        if not os.path.isfile(os.path.join(root, FILE_NAME)):
            where = '--project' if explicit else 'PANGOLOOP_PROJECT'
            raise SystemExit(f'pangoloop: {where} names {named}, which has no {FILE_NAME}; make the project there with '
                             f'`pangoloop init {named}`')
        return root
    return find(start)


def read_table(root: str, name: str) -> dict:
    """One table of the project's configuration file ({} when the file or the table is missing or unreadable; the full
    checking is config.load's)."""
    try:
        with open(os.path.join(root, FILE_NAME), 'rb') as handle:
            table = tomllib.load(handle).get(name, {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return table if isinstance(table, dict) else {}


def folder(root: str, key: str, environ: dict | None = None) -> str:
    """The folder for `key` ('runs', 'plans' or 'roles') of the project: the variable, else `[paths]`, else the default."""
    environ = os.environ if environ is None else environ
    given = environ.get(VARIABLES[key], '').strip()
    if given:
        return os.path.abspath(given)
    value = read_table(root, 'paths').get(key)
    if isinstance(value, str) and value.strip():
        return os.path.abspath(os.path.join(root, os.path.expanduser(value.strip())))
    return os.path.join(root, STATE_FOLDER, key)


def state_folder(root: str) -> str:
    return os.path.join(root, STATE_FOLDER)


def view_file(root: str) -> str:
    return os.path.join(root, STATE_FOLDER, 'view.json')


def apply(root: str, environ: dict | None = None) -> None:
    """Makes the project the one the rest of the process sees: PANGOLOOP_PROJECT, and for each folder the variable, unless
    one was set by the caller (an explicit variable wins). The configuration file is read from the project's root (by
    pangoloop/config.py) unless --config or PANGOLOOP_CONFIG names another."""
    environ = os.environ if environ is None else environ
    environ['PANGOLOOP_PROJECT'] = root
    for key, variable in VARIABLES.items():
        if not environ.get(variable, '').strip():
            environ[variable] = folder(root, key, environ)
