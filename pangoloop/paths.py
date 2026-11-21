"""Where the runs, the plans and the roles are kept: one default, read by the engine, the viewer and the commands alike.

With a project (pangoloop/project.py) the folders are `<project>/.pangoloop/runs`, `.../plans` and `.../roles`, or what the
project's configuration file moved them to; the command line puts the choice into PANGOLOOP_RUNS, PANGOLOOP_PLANS and
PANGOLOOP_ROLES before anything else reads it. Without a project (the old way of calling `run <plan> <repo> <name>` from any
folder) the default of the runs is `<current working directory>/.pangoloop/runs`; the folder is made on demand (the
engine makes it when a run starts, the readers treat a missing one as empty). The variables override, and the viewer's own
RUN_VIEW_RUNS wins over both (even when set to an empty string).
"""
import os


def default_runs() -> str:
    root = os.environ.get('PANGOLOOP_PROJECT', '').strip()
    return os.path.join(root or os.getcwd(), '.pangoloop', 'runs')


def runs_folder(viewer: bool = False) -> str:
    """The folder that holds the runs, from the environment now."""
    if viewer and 'RUN_VIEW_RUNS' in os.environ:
        return os.environ['RUN_VIEW_RUNS']
    explicit = os.environ.get('PANGOLOOP_RUNS', '').strip()
    return os.path.abspath(explicit) if explicit else default_runs()


def _state_child(variable: str, name: str) -> str:
    explicit = os.environ.get(variable, '').strip()
    if explicit:
        return os.path.abspath(explicit)
    root = os.environ.get('PANGOLOOP_PROJECT', '').strip()
    return os.path.join(root or os.getcwd(), '.pangoloop', name)


def plans_folder() -> str:
    """The folder a plan given by bare name is looked for in."""
    return _state_child('PANGOLOOP_PLANS', 'plans')


def roles_folder() -> str:
    """The folder of the roles: one `<name>.md` per role."""
    return _state_child('PANGOLOOP_ROLES', 'roles')
