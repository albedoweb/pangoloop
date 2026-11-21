# Kinds and roles

A node says two different things, and the plan keeps them apart.

- **`kind`** is what the engine does with the node. The list is closed:
  - `build`: a model session in its own git worktree, then an integration by rebase. It must change something unless the node
    says `may_be_empty: yes`.
  - `review`: a model session that reads the whole change and ends with a verdict. On `CHANGES REQUESTED` a repair worker gets the
    findings and the review runs again, up to `max_fix_rounds`.
  - `baseline`: a model session that looks and changes nothing (a first run of the tests, a survey).
  - `script`: no model; the steps of its `STEPS` line.
  - `acceptance`: no model; the script runs the backticked commands of `VERIFY` and calls a repair worker when one fails.
  - `human`: a stop for a person; its `TASK` is the question.
- **`role`** is who the worker is: a name you choose, defined by a file `.pangoloop/roles/<name>.md` whose text goes into the
  worker's prompt before the node's task. Only `build`, `review` and `baseline` nodes have a model worker and so a role; a role on
  `script`, `acceptance` or `human` is a plan error. The role does not choose the model: the model comes from the node's category
  (the role's own category, unless the node sets one) through `[routes]` in `pangoloop.toml`.

## The roles `pangoloop init` copies

`pangoloop roles` lists the project's roles (name, kinds, category, description); `pangoloop roles show <name>` prints one. Read the
role before you use it. The seven examples are short and plain, in one author's voice, and not measured; the project may have
changed them or added others.

| role | kind | what it is |
|---|---|---|
| `coder` | build | writes the code for one node, small and in the repository's style |
| `test-writer` | build | writes tests for behaviour that exists or is specified, changes no production code |
| `interface-builder` | build | builds or changes a screen and looks at it before saying it is done (category `ui`) |
| `researcher` | build | finds something out and writes it as a document at a path the task names, changes no code |
| `reviewer` | review | reads the whole change for correctness against the task statement (category `review`) |
| `security-reviewer` | review | reads the change as someone who wants to misuse it (category `review`) |
| `simplicity-reviewer` | review | reads the change for what can be left out (category `review`) |

Choose by the node's work: `coder` for most build nodes; `test-writer` for a node that only adds tests (it keeps tests and code in
different hands, which is the point); `interface-builder` only where a screen must be looked at; `reviewer` for the review; add
`security-reviewer` or `simplicity-reviewer` as a second review node only when the work warrants it. A `baseline` node takes a
build role (`coder`).

## Adding a role

Ask the user first: roles are shared by every plan of the project. Then write `.pangoloop/roles/<name>.md`:

```
---
kind: build
category: ui
description: one line, shown in lists
---
You are ... what you must do, what you must not do, what your handoff holds, and when to stop and ask.
```

The header lines are `kind:` (`build`, `review`, or `build, review`), `category:` (optional: the route that selects the model) and
`description:`. Nothing else goes in the header (a tool list or a limit is a fault). The prompt may use `{{REPO}}` and `{{BRANCH}}`.
A role never carries the verdict format of a review: the engine adds it. A role file that is broken stops only a plan that names
it. A role named in a plan that has no file stops the run before it starts (exit 2) with a sentence that lists the roles it found.
Commit the file with the plan.

A role you add changes how the worker behaves, not what it may do: the tools, the deny list and the limits belong to the
configuration. Plans written before kinds existed use `role: build` (and the like) with no `kind`; write `kind` and `role` in every
new plan.
