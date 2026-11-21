# The plan format

A plan is one Markdown file, kept in `.pangoloop/plans/<name>.md` (run it by its bare name: `pangoloop run <name>`). It has three
parts, in this order:

1. **The background**: free text before `## Nodes`. Every node's worker is told to read it. It says how to work, not what must be
   true: the repository's conventions, the exact commands that check work and how long each takes, what never to run, how files
   are deleted here, what to do when two rules seem to collide (stop and ask, naming the rules).
2. **`## Nodes`**: the nodes, each a `### <id>` heading followed by field lines.
3. **`## Task statement`**: the statement of the work, written at the end of the plan: what must be true afterwards (numbered
   rules `R1`, `R2`...), what must not change, and how the work is accepted. The engine ignores it; the workers and the reviewer
   read it. `references/cutting-work.md` says how to write it.

The text may use `{{REPO}}` (the folder a node works in) and `{{BRANCH}}` (the branch the repository was on); the engine fills them.
Keep the line `All nodes work in the same folder, the repository folder.` in the background: with worktrees the engine replaces it
by a paragraph about each node's own copy. Do not use any other `{{...}}` or `@@...@@` placeholder: an unfilled one is a warning
of `preflight` and an error with `--strict`.

## A node

`### <id>` where the id is one word (letters, digits, dashes). Then lines `- <field>: <value>`. **A value is one line**, and no
line may be longer than 1,200 characters (a reader's file tool cuts longer ones); longer guidance goes into the background as
paragraphs. The fields:

| field | meaning |
|---|---|
| `label` | a short title |
| `kind` | what the engine does with the node: `build`, `review`, `baseline`, `script`, `acceptance`, `human` (a closed list) |
| `role` | who the worker is: a name of a role file (`pangoloop roles`); only for `build`, `review`, `baseline`. A role on `script`, `acceptance` or `human` is an error |
| `category` | optional: picks the model route (`review`, `ui`, or a category of the project's own in `[routes]`); default: the role's category, else the coder's route |
| `depends_on` | `none`, or node ids separated by commas; the node starts when all of them are accepted; ready nodes run side by side |
| `write_scope` | the files the node may change, or `nothing`. Text for the worker; **the engine does not check it** |
| `TASK` | what to do, in the order to do it, naming the first command to run |
| `DELIVERABLE` | what the node leaves behind and what its handoff says |
| `SCOPE` | what is in and out of the node's reach |
| `VERIFY` | the exact commands, in backticks, the node may check itself with |
| `STOP WHEN` | when to stop, including the way out: ask when a rule cannot be met as written |
| `may_be_empty` | `yes` lets a build node finish with no change; default `no` (a build node that changed nothing fails, reason `no change`) |
| `STEPS` | for `kind: script` only, in place of `TASK`: the steps (below) |

Every node except a script node needs `label`, `kind` (or `role`), `depends_on`, `write_scope` and the five contract lines
(`TASK`, `DELIVERABLE`, `SCOPE`, `VERIFY`, `STOP WHEN`). A script node needs `label`, `kind`, `depends_on`, `write_scope`, `STEPS`.

## One example of each kind

```
### add-parser
- label: Write the parser
- kind: build
- role: coder
- depends_on: none
- write_scope: src/parser.py
- TASK: Add `parse(text)` to `src/parser.py` as rule R1 describes it. Run `python3 -m pytest tests/test_parser.py -q` first to see the starting state.
- DELIVERABLE: the changed `src/parser.py`; in the handoff, one line saying what you added.
- SCOPE: only `src/parser.py`.
- VERIFY: `python3 -m pytest tests/test_parser.py -q`
- STOP WHEN: the command passes, or a rule cannot be met as written: then stop and ask the coordinator, saying which rule and why.

### survey
- label: Run the whole suite as it is
- kind: baseline
- role: coder
- depends_on: none
- write_scope: nothing
- TASK: Run `python3 -m pytest -q` and report the result in one line. Change nothing.
- DELIVERABLE: one line: the command's result.
- SCOPE: read only.
- VERIFY: `python3 -m pytest -q`
- STOP WHEN: you have reported the result.

### clean-up
- label: Remove the old module
- kind: script
- depends_on: add-parser
- write_scope: src/old_parser.py
- STEPS: delete src/old_parser.py ; run `python3 -m pytest -q`

### accept
- label: Acceptance
- kind: acceptance
- depends_on: clean-up
- write_scope: nothing
- TASK: the script runs the commands in VERIFY; no model is asked.
- DELIVERABLE: exit 0 for every command.
- SCOPE: the whole repository.
- VERIFY: `python3 -m pytest -q`, `python3 -m pyflakes src`
- STOP WHEN: every command exits 0.

### review
- label: Review of the whole change
- kind: review
- role: reviewer
- depends_on: accept
- write_scope: nothing
- TASK: Read the whole change against the Task statement: the rules R1 to R3, and what must not change. List each finding with the rule it breaks.
- DELIVERABLE: the findings, then the verdict.
- SCOPE: read only; the work of the script node is not yours to read line by line.
- VERIFY: `python3 -m pytest -q`
- STOP WHEN: you have given the verdict.

### sign-off
- label: Release approval
- kind: human
- depends_on: review
- write_scope: nothing
- TASK: Is the change ready to ship? Answer yes, or say what is missing.
- DELIVERABLE: the person's answer.
- SCOPE: nothing is changed.
- VERIFY: `git status`
- STOP WHEN: the person has answered.
```

An **acceptance** node runs every backticked command of `VERIFY` itself, in the repository folder, and calls a repair worker when
one fails (up to the configured rounds); it needs at least one command in backticks. A **review** node's role file never carries the
format of the verdict: the engine adds it (`FINDING 1:` lines, then `VERDICT: PASS` or `VERDICT: CHANGES REQUESTED`), and on a
request for changes it starts a repair worker and reviews again, up to `max_fix_rounds` (default 2). A **human** node is a question
for the person: its `TASK` is the question, any answer but `ABORT` accepts it. A **baseline** node looks and changes nothing; it
uses a `build` role.

## The steps of a script node

`STEPS` is one line of steps separated by ` ; ` (space, semicolon, space). Inside backticks a semicolon does not split. The words:

- `delete <paths>`: delete files or folders (a folder ends with `/` or is a folder). Paths are relative to the node's copy; quote
  one that has a space; `*`, `?`, `[...]` and `**` work as in a shell. Refused, and the node fails: an absolute path, `..`, the root,
  anything inside `.git`, a symbolic link, and a path that matches nothing.
- `delete? <paths>`: the same, but a path that matches nothing is skipped.
- `move <from> <to>`: move one file or folder; the target must not exist; no wildcards.
- `run <command>`: a shell command in the node's copy; a non-zero exit fails the node. Write it in backticks if it contains ` ; `.

A node's steps can also come from a file (`steps_file: <path>`, one step per line); `pangoloop steps check <repo> <file>` shows
what each step would do and changes nothing. A `run` step is an ordinary shell command with no deny list and no sandbox.

## What stops a run before it starts (exit status 2, nothing is run)

A missing node field; `## Nodes` absent; a `depends_on` naming an unknown node; an unknown `kind` or
`may_be_empty` value; a role on a kind that has none, a role file that does not exist or does not allow the kind; a fault in a
`STEPS` line (an empty step, an unknown word, an unbalanced backtick, a semicolon outside backticks in a step that has backticks);
a route the plan needs that is still a placeholder in `pangoloop.toml` (`default-model`, `review-model`, `ui-model`); a run name
already used. Two nodes with one id, and a dependency cycle, are **not** detected (such nodes would misbehave or never start): keep ids unique and the order a plain forward list. The
message is on standard error and names the node. `pangoloop preflight <plan> --strict` also turns into errors: a line over 1,200
characters and a placeholder left unfilled.
