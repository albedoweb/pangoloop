"""The children a run starts, recorded while they run, and stopped when the engine that started them is gone.

Added November 20, 2026 (TODO section 12, "Orphans after a kill"). `kill -9` of the engine leaves its workers, script steps,
acceptance commands and integration checks running. The engine therefore

  - starts every child in a process group of its own (a session of its own: the child is the leader, so its process group id
    is its pid), which `os.killpg` stops whole, grandchildren included;
  - records each child in the run's `state.json` under `children` while it runs: `{"<pid>": {pid, started, kind, what, at}}`,
    `started` being the start time the operating system reports for that process (`ps -o lstart=`); the entry is removed
    when the child ends;
  - on `--resume`, before anything else, and on `pangoloop cleanup <run>`, stops what is recorded, still alive and
    PROVABLY THE SAME PROCESS: the recorded start time must equal the one the system reports now. A process id alone is never
    enough (ids are reused); a live process with another start time is left alone and the report says so.

What is not covered: a child that started a session of its own (a daemon) is outside the group; a child started in the
instant between the process starting and its record being written (a few milliseconds) is not recorded.
"""
import os
import signal
import subprocess
import time


def _ps(pid: int, field: str) -> str:
    try:
        return subprocess.run(['ps', '-o', f'{field}=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ''


def start_time(pid: int) -> str:
    """The start time the system reports for the process, '' when there is none."""
    return _ps(pid, 'lstart')


def entry(pid: int, kind: str, what: str) -> dict:
    """The record of a child just started: its pid, its start time (read now), what kind it is, what it is."""
    return {'pid': pid, 'started': start_time(pid), 'kind': kind, 'what': what[:200],
            'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}


def is_same_process(item: dict) -> bool:
    """True when a live (not zombie) process with the recorded pid has the recorded start time."""
    pid, started = item.get('pid'), item.get('started')
    if not isinstance(pid, int) or pid <= 1 or not started:
        return False
    status = _ps(pid, 'stat')
    if not status or status.startswith('Z'):
        return False
    return start_time(pid) == started


def kill_group(pid: int, sign: int = signal.SIGKILL) -> None:
    """Signals the process group of which `pid` is the leader; when it is not a leader, that one process only."""
    try:
        if _ps(pid, 'pgid') == str(pid):
            os.killpg(pid, sign)
        else:
            os.kill(pid, sign)
    except (ProcessLookupError, PermissionError):
        pass


def stop_recorded(children: dict | None) -> list[str]:
    """Stops every recorded child that is alive and provably the same process (SIGTERM to its group, then SIGKILL to the
    group after up to two seconds). Returns one line per child that was stopped or that was left alone because the process
    of that id is not the recorded one now; a child that is simply gone says nothing."""
    lines: list[str] = []
    victims: list[dict] = []
    for item in sorted((children or {}).values(), key=lambda value: value.get('pid') or 0):
        label = f"{item.get('kind', 'child')} {item.get('what', '')!r} (pid {item.get('pid')}, started {item.get('started')})"
        if is_same_process(item):
            victims.append(item)
            lines.append(f'stopped {label}')
        elif isinstance(item.get('pid'), int) and item['pid'] > 1 and _ps(item['pid'], 'stat') \
                and not _ps(item['pid'], 'stat').startswith('Z'):
            lines.append(f'left alone: a process with pid {item["pid"]} runs now, but it did not start at the recorded '
                         f'time, so it is not the recorded {item.get("kind", "child")} {item.get("what", "")!r}')
    for item in victims:
        kill_group(item['pid'], signal.SIGTERM)
    deadline = time.time() + 2
    while time.time() < deadline and any(is_same_process(item) for item in victims):
        time.sleep(0.05)
    for item in victims:   # the leader may be gone while a grandchild ignores SIGTERM: the group id still names the group
        try:
            os.killpg(item['pid'], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return lines
