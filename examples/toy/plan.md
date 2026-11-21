# Plan: toy task calc, a function and its test

Written on November 18, 2026 as the smallest plan that uses every kind of node but the review: a baseline, one build
node, a script node and an acceptance node. The toy repository (made by `examples/toy/run.sh`, or copy `examples/toy/files/`) is `calc.py` with an
`add` function and `test_calc.py` with its test. Repository: `{{REPO}}`, branch `{{BRANCH}}`.

<!--
How a plan is written (this comment is part of the background every node reads; delete it in a plan of your own).

A plan is one Markdown file: a background for every node, then `## Nodes`, then `## Task statement` with what must be true at
the end. The engine uses only what is between the two headings. The text of the file may use `{{REPO}}` (the repository folder)
and `{{BRANCH}}` (the branch it was on); the engine fills them in.

A node is `### <id>` (one word) followed by lines `- name: value`; a value is ONE line. The fields:
- label: a short title.
- category: picks the model route in `[routes]` of the configuration: `review` and `ui` have routes of their own, every other
  word (`deep`, `simple-work`, a word of your own) takes the default route. A node with a `kind` may leave it out: its role's
  category is used, else `default`.
- kind: what the ENGINE does with the node; the list is closed. `build`: a model session that changes files, in its own git
  worktree when `SG_NODE_WORKTREES=1` (the short `pangoloop run <plan>` turns that on). `baseline`: a model session that looks and
  changes nothing. `script`: no model, see STEPS below. `acceptance`: no model, the script runs the backticked commands of VERIFY
  and calls a repair worker if one fails. `review`: a model reads the whole change and ends with `VERDICT: PASS` or
  `VERDICT: CHANGES REQUESTED`. `human`: a question for a person (with `SG_HUMAN=1`).
- role: WHO the worker is, for a node of kind `build`, `review` or `baseline`: the name of a file `<name>.md` in the project's roles
  folder (`.pangoloop/roles/`; `pangoloop roles` lists them). Its text goes into the worker's prompt before the node's TASK. A
  node of kind `script`, `acceptance` or `human` has no role. A new plan writes both lines, `kind: build` and `role: coder`. The
  nodes below are written the old way, with only `role: build` (and the like): that word is read as the kind, with a built-in
  role that adds nothing, so a plan like this runs as it always did.
- depends_on: `none`, or node ids separated by commas. A node starts when all of them are accepted; ready nodes run side by side.
- write_scope: the files the node may change, or `nothing`. It is text for the agent; the engine does not check it.
- TASK, DELIVERABLE, SCOPE, VERIFY, STOP WHEN: the contract of a model node, passed to it word for word. VERIFY is the exact
  commands, in backticks, it checks itself with; STOP WHEN names when to stop, including when to ask.
- may_be_empty: `yes` lets a build node finish with no change (by default a build node that changed nothing fails).
- STEPS (kind `script` only, in place of TASK): steps separated by ` ; ` (space, semicolon, space; inside backticks a semicolon
  does not split): `delete <paths>`, `delete? <paths>` (as delete, but a path that matches nothing is skipped), `move <from> <to>`,
  `run <command>`. Example: `STEPS: delete legacy/ "old assets/" ; move src/a.py src/b.py ; run `npm test``. A node of this kind
  needs only label, kind (or role), depends_on, write_scope and STEPS.

A node that must stop and ask ends its answer with `NEEDS COORDINATOR: <question>` (a fact or a decision) or
`NEEDS HUMAN: <question>` (what only a person may do). Check a plan before a run with `pangoloop preflight <plan> --strict`.
-->

All nodes work in the same folder, the repository folder.

Rules for every node: change only the files your node names; run only the commands your node names; do not commit.
Answer in a few lines.

## Nodes

### baseline
- label: Run the tests as they are
- category: simple-work
- role: baseline
- depends_on: none
- write_scope: nothing
- TASK: Run `python3 -m unittest -q` in the repository and report whether it passes. Change nothing.
- DELIVERABLE: one line: the command's result.
- SCOPE: read only; no file is changed.
- VERIFY: `python3 -m unittest -q`
- STOP WHEN: you have reported the result.

### add-double
- label: Add double(n) and its test
- category: simple-work
- role: build
- depends_on: baseline
- write_scope: calc.py, test_calc.py
- TASK: Add a function `double(n)` to `calc.py` that returns n times two, and a test for it in `test_calc.py` (a new method of the existing test class, with at least two cases).
- DELIVERABLE: the changed `calc.py` and `test_calc.py`; in the handoff, one line saying what you added.
- SCOPE: only `calc.py` and `test_calc.py`.
- VERIFY: `python3 -m unittest -q`
- STOP WHEN: the command passes.

### run-tests
- label: The tests, by the script
- role: script
- depends_on: add-double
- write_scope: nothing
- STEPS: run `python3 -m unittest -q`

### accept
- label: Acceptance
- category: simple-work
- role: acceptance
- depends_on: run-tests
- write_scope: nothing
- TASK: the script runs the command in VERIFY; no model is asked.
- DELIVERABLE: exit 0.
- SCOPE: the whole repository.
- VERIFY: `python3 -m unittest -q`
- STOP WHEN: the command exits 0.

## Task statement

Add `double(n)` to the toy calculator and test it. Done means: `python3 -m unittest -q` exits 0 and `calc.py` has a
function `double`.
