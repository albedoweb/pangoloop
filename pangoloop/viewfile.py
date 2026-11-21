"""`<project>/.pangoloop/view.json`: which process serves a project's page, and where.

    {"pid": 4242, "port": 5420, "started": "2026-11-20T10:00:00Z", "process_started": "Fri Nov 20 10:00:00 2026",
     "url": "http://127.0.0.1:5420/", "project": "/path/to/project"}

`pid` and `port` are what `run --detach --view`, `status --json` and a second `pangoloop view` need; `process_started` is what
the system reports as the process's start time, so that a file left by a process that is gone (and whose pid another process
may now have) is recognised as stale and replaced, never trusted. The page removes the file when it stops.
"""
import datetime
import json
import os

from pangoloop import project, resume


def url_for(port: int) -> str:
    return f'http://127.0.0.1:{port}/'


def read(root: str) -> dict | None:
    """The record of the page serving the project, or None when there is none, or the process is gone (a stale file)."""
    data = resume.read_json(project.view_file(root))
    if not isinstance(data, dict) or not isinstance(data.get('port'), int):
        return None
    if not resume.process_alive(data.get('pid'), data.get('process_started')):
        return None
    return data


def write(root: str, port: int) -> dict:
    """Records that this process serves the project on `port` (replacing a stale file)."""
    data = {'pid': os.getpid(), 'port': port,
            'started': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'process_started': resume._ps(os.getpid(), 'lstart'), 'url': url_for(port), 'project': root}
    os.makedirs(project.state_folder(root), exist_ok=True)
    resume.write_json(project.view_file(root), data)
    return data


def clear(root: str) -> None:
    """Removes the record when it is this process's own."""
    data = resume.read_json(project.view_file(root))
    if isinstance(data, dict) and data.get('pid') == os.getpid():
        try:
            os.remove(project.view_file(root))
        except OSError:
            pass


def address(root: str | None) -> str | None:
    """The address of the page serving the project, or None."""
    data = read(root) if root else None
    return data['url'] if data else None
