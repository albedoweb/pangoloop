"""Processes that outlive the command that started them: a detached engine (`run --detach`) and the page (`run --view`).

A detached engine is `python -m pangoloop run <plan> <repo> <name>` in a session of its own (it survives the caller's end and
its terminal). The command returns when the engine has made the run's folder, which it does after it has read and checked the
plan (a plan or configuration fault stops the engine first, and then the command prints the engine's last lines and exits with the
engine's status, 2). The engine's own output goes to `<run folder>/engine.log`: it is first written to `<runs>/.<name>.log`
(the folder does not exist yet, and the engine refuses to start in one that does) and moved into the run folder, still open, when
the folder appears.
"""
import json
import os
import signal
import subprocess
import sys
import time

from pangoloop import project, viewfile

START_SECONDS = 60       # how long to wait for the engine to make its folder
PAGE_SECONDS = 20        # and for the page to say where it listens
POLL = 0.05


def _tail(path: str, lines: int = 12) -> str:
    try:
        with open(path, errors='replace') as handle:
            return ''.join(handle.readlines()[-lines:]).rstrip()
    except OSError:
        return ''


def start_engine(args: list[str], runs: str, name: str, cwd: str, wait: float | None = None) -> dict:
    """Starts the engine detached with `args` (what follows `pangoloop run`, including the run's name) and returns
    {'pid', 'folder', 'log'} when its folder exists; `Failed` (the sentence to print and the exit status) when it does not."""
    os.makedirs(runs, exist_ok=True)
    folder = os.path.join(runs, name)
    if os.path.exists(folder):
        raise Failed(f'pangoloop: the run folder {folder} exists already; a run name is never reused', 2)
    early = os.path.join(runs, f'.{name}.log')
    log = open(early, 'w')
    process = subprocess.Popen([sys.executable, '-m', 'pangoloop', 'run', *args], cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    log.close()
    deadline = time.time() + (wait or START_SECONDS)
    while True:
        if os.path.isdir(folder):
            final = os.path.join(folder, 'engine.log')
            try:
                os.replace(early, final)   # the engine keeps writing to the same file
            except OSError:
                final = early
            return {'pid': process.pid, 'folder': folder, 'log': final}
        code = process.poll()
        if code is not None:
            message = _tail(early)
            try:
                os.remove(early)
            except OSError:
                pass
            raise Failed(message or f'pangoloop: the engine ended with status {code} before the run started', code or 2)
        if time.time() > deadline:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            raise Failed(f'pangoloop: the engine did not make {folder} in {int(wait or START_SECONDS)} seconds; it was stopped. '
                         f'Its output is in {early}', 1)
        time.sleep(POLL)


class Failed(Exception):
    """The engine or the page did not start: the sentence to print, and the exit status."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.message, self.code = message, code


def ensure_view(root: str, wait: float | None = None) -> str:
    """The address of a page that serves the project: the one already serving, or one started now in the background."""
    found = viewfile.address(root)
    if found:
        return found
    log = open(os.path.join(project.state_folder(root), 'view.log'), 'w') if os.path.isdir(project.state_folder(root)) else None
    process = subprocess.Popen([sys.executable, '-m', 'pangoloop', 'view'], cwd=root, stdin=subprocess.DEVNULL,
                               stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    if log:
        log.close()
    deadline = time.time() + (wait or PAGE_SECONDS)
    while time.time() < deadline:
        found = viewfile.address(root)
        if found:
            return found
        code = process.poll()
        if code is not None:   # a page for the project started meanwhile (another command) or this one failed
            found = viewfile.address(root)
            if found:
                return found
            raise Failed(f'pangoloop: the page did not start (status {code}): '
                         + _tail(os.path.join(project.state_folder(root), 'view.log')), 1)
        time.sleep(POLL)
    raise Failed(f'pangoloop: the page did not say where it listens in {int(wait or PAGE_SECONDS)} seconds '
                 f'(see {os.path.join(project.state_folder(root), "view.log")})', 1)


def describe(info: dict, name: str, page: str | None, as_json: bool) -> str:
    if as_json:
        return json.dumps({'run': name, 'folder': info['folder'], 'log': info['log'], 'pid': info['pid'], 'page': page,
                           'page_run': f'{page}?run={name}' if page else None})
    lines = [f'run: {name}', f"folder: {info['folder']}", f"engine log: {info['log']}", f"engine pid: {info['pid']}",
             f'page: {page}?run={name}' if page else 'page: not serving (`pangoloop run ... --view`, or `pangoloop view`)',
             f'next: pangoloop status {name}']
    return '\n'.join(lines)
