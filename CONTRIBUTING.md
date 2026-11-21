# Contributing

How to change this repository. It describes how the work is done.

## Branches and pull requests

- Work on a branch made from `main` (`fix/...`, `docs/...`, `feature/...`), and open a pull request at
  https://github.com/albedoweb/pangoloop. Do not force-push a branch someone else has pulled.
- One pull request is one piece of work that can be read in one sitting. A branch that has fallen behind takes `main` in with a
  merge (`git merge main`).
- Several people may work at once: give each branch its own worktree (`git worktree add ../pangoloop-wt/<name> -b <branch> main`,
  outside this folder) and do not edit a file another branch is changing.

## The suite must pass

```
python3 -m unittest discover -s tests          # all of it: about two minutes, no network, no model call
python3 -m unittest discover -s tests -p 'test_resume.py'   # one module
```

- Python 3.11 or newer, standard library only; the package has no dependencies and a pull request must not add one.
- **Stubs only in tests.** No test calls a real model, a real service or the person's own installation: a stub stands in for the
  runner (`tests/claude_stub.py`, `tests/engine_stub.py`, the recorded streams in `tests/fixtures/`). A new behaviour comes with a
  test that fails without it.
- The suite starts subprocesses and uses `git`, `ps`, `pgrep` and process signals, so it needs a Unix system with these on the
  path. It runs on macOS and, in the workflow `.github/workflows/tests.yml`, on Linux with Python 3.11, 3.12 and 3.13. One test
  (`test_the_pages_script_is_valid_javascript`) skips itself when `node` is not installed. One test is an expected failure
  by design (`test_ui_whole_script`): when it starts to pass, remove its marker.
- A test that cannot run on some system is marked to skip with the reason, never deleted.
- Linux (the workflow has not run on GitHub yet; the suite was read for it, not run there): no test uses a macOS-only tool. The
  desktop notification (`osascript`, `terminal-notifier`) is switched off or replaced in every test, `git` is always given an
  identity and `init -b work` on the command line, and the `ps -o lstart=`, `ps -axo pid=,ppid=` and `pgrep -f` forms used by the
  engine and the tests are the ones procps on Linux accepts. Where a first run on Linux fails, look first at the process
  start time that `ps` reports (`lstart`: the engine compares it to tell a reused process id from the same process, and on a
  virtual machine whose clock was corrected the value can move by a second) and at timing on a slow runner.

## One fault, one commit

- One fault or one change per commit, a message in the style of `git log` here: a first line that says what is now true, then a
  paragraph of what and why, with the numbers that were measured.
- **The documents travel with the change**: the help texts (`pangoloop <command> --help`) and `examples/toy/plan.md` for anything
  the code reads or writes (variables, files, exit codes, the plan format), the README for what a user sees, `docs/SETUP.md` and
  the comments of the presets for settings, `ROADMAP.md` for what is planned. A change whose behaviour is not in the documents is
  not finished.
- Do not mix a move or a rename with a change of behaviour in one commit.

## Secrets, other people's material and the repository's own state

- No keys, tokens, passwords, cookies or personal paths in any file or commit. Test values are invented. The recorded streams in
  `tests/fixtures/` were scrubbed before committing (`tests/fixtures/claude_code/README.md` says how).
- Do not add copies of other people's articles, images or API output.
- A run's folder (`.pangoloop/`, `runs/`) and build output are ignored; `git status` must be clean after a test run
  and after `pip install .`.

## Releases

A version is the number in `pangoloop/__init__.py` (one place). A release is a tag `vX.Y.Z` on a commit of `main` whose suite
passed; the maintainer makes it, not a pull request. No tag exists yet.
