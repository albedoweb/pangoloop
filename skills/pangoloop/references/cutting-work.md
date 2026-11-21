# Cutting work into nodes

The short form of the rules of the craft. The longer form, written as a prompt for a model that prepares a whole stage of work,
is `prompts/STAGE_AUTHOR.md` in the tool's repository; the bold headings below are the same ones, in the same words, so that you
can look a rule up there when you want its reasons at length. The workers are capable and literal: they do what the text says,
stop where two rules collide, and cannot ask you what you meant except through a slow channel. Every fault left in the plan
costs a stop, a wrong repair, or a correct solution failed by a wrong check. Read your own plan once as the worker of each node:
can it do its part knowing only this file?

## Facts before words

- **Measure, do not remember.** Which files import which, which command runs the tests, what a tool prints: get it from the
  repository with a command before you write it into a plan. A plan written from an impression names files that do not exist.
- **Do the mechanical part once yourself, crudely, in a throwaway copy.** For a rename, a move or a deletion, try it in a copy and
  run the tests. What is red afterwards is the real work, and its size tells you how to cut it.
- **Try the riskiest rule by hand before you write it down.** A rule that cannot be followed shows itself in an hour of trying; in
  a run it shows itself as a stop.
- **Count before and after.** Lines, files, test cases. The user will ask, and the numbers catch a deletion that went too far.

## Scripts before models

- **Anything a table can describe is a script node**: deleting listed paths, moving files, running a formatter or a generator. A
  model asked to delete many folders may stall, because its tools refuse recursive deletes. Use `kind: script` with `STEPS`.
- **A model node starts from a state you have measured.** Best is a state that builds and passes. If the scripted part leaves
  something red that a script cannot repair, the first model node's only job is to repair exactly the red list you measured, and
  its task names that list. No node starts from a red state nobody listed.
- **Tools are part of the base, and they are checks.** "Nothing unused is left" is a command that exits 0, run in the
  acceptance, not a sentence for a reviewer. If the project has a linter, type checker or build, they are acceptance commands.

## Cutting the work into nodes

- **One part, one node, one set of files.** Two nodes never change the same file unless one follows the other. Nodes with
  overlapping files run in sequence; only nodes with disjoint files run side by side. Every file the work may touch belongs to
  exactly one node's `write_scope`; list the files, do not describe them.
- **Order by dependency of meaning, not of size.** Separate "tests stop using X" from "X leaves the product": the first is wide
  and safe, the second is narrow and risky, and it is only possible after the first.
- **A node's part fits in one reading.** If you cannot say what the node does in three sentences, split it. A node a cheap model
  can finish in a few minutes is about right.
- **Picture work goes to the model that can see**, with looks allowed in its `VERIFY` line (the role `interface-builder`, the route
  `ui`); text and logic go to the cheap model. A reviewer does not judge pictures: say so, and say who does (the user).
- **Put the acceptance in the plan twice**: after the build nodes and after the review, because a repair after a review can break
  what passed. A small plan of two or three nodes may keep one, placed before the review.

## Writing the statement

The statement is the `## Task statement` at the end of the plan.

- **Start with the owner's words, quoted**, then one paragraph of what the work is and is not. The owner is the user who asked.
- **Say what does not change before what does.** A section "What stays": paths, behaviours, public names. Every exception is
  written in that section, by path.
- **Number every rule.** `R1`, `R2`: workers and the reviewer quote them, and you will rule on them during the run.
- **Enumerate, do not characterise.** Write "these three files: a, b, c", not "the affected files". A phrase that names a set is
  read as a different set by different readers.
- **Never write an absolute you will break.** "This task only removes" meets "add one test file" and a reader calls it a
  contradiction, rightly. Write what is true: "it adds no feature; the only new things are the tests the parts ask for".
- **When two rules can meet, say which wins, with the case.** For each pair of rules that touch the same files, ask what a worker
  does when both apply.
- **Each part ends with its files**: the paths it may change. The parts' files and the "what stays" section must agree.
- **End with how the work is accepted**: the exact commands, in order.

Plain words, short sentences, lines under 115 characters where you can. No sentence that needs the previous conversation.

## Writing the plan

- **Every model node has a `kind` and a `role`.** Run `pangoloop roles` and read a role (`pangoloop roles show <name>`) before you
  use it: its text goes into the worker's prompt before your `TASK`, so do not repeat it in the node; write only what is particular
  to this node. Use a role that exists; ask the user before you add a role, since roles are shared by every plan.
- **The background says how to work, the statement says what must be true.** In the background: the repository's conventions,
  what counts as reading and what as checking, how files are deleted and moved here, which commands never run (the package
  installer, anything that rewrites tracked files, anything on a port the user uses), and a paragraph "Rulings during the run: none
  yet" saying that the coordinator appends to it and that every node and the reviewer read it before judging anything.
- **VERIFY is the exact list of commands a node may use to check its work**: to decide whether the work is right, it runs those and
  no others. It restricts checking, not working. Say what is not a check either: reading and comparing files (`cat`, `grep`, `diff`,
  `git status`, `git diff`), and part of a listed command (the tests of one folder).
- **A TASK line tells the order of work and the first command to run**; where it names a command for checking, that command is in the
  node's `VERIFY` line or is a part of one that is.
- **STOP WHEN names the way out**: "or a rule cannot be met as written: then stop and ask the coordinator, saying which rule and
  why". Workers that have this line ask good questions; workers without it guess.
- **Asking.** The engine gives nodes two closing lines, `NEEDS COORDINATOR:` and `NEEDS HUMAN:`. If the work has commands only the
  person may run (deploy, anything outside the repository, anything that cannot be undone), list them in a section
  `## The person's commands` before `## Nodes`. Do not tell a node to "end with" such a line: a node told to may write
  "NEEDS COORDINATOR: none" and stop itself. A node that is not blocked writes no such line at all; say so in the background. The engine tells every node what the two lines
  mean, but a reader of the plan alone does not know who "the coordinator" is, so the background says in one sentence that the
  coordinator is whoever wrote the plan and answers a node that ends with `NEEDS COORDINATOR: <question>`.
- **The reviewer's node** (`kind: review`, a reviewer role): say how to read the change, then what is theirs to judge, most
  important first, as a numbered list naming the rules it enforces, then what is NOT theirs and not to be reported. A reviewer told
  only "review against the statement" judges by the letter of a rule and repair rounds make the product worse.
- **Rulings must travel.** However careful the statement, nodes will find gaps, and you will rule during the run
  (`references/requests-and-rulings.md`). The engine carries each ruling to every later node and review round.

## How big, how many

Two or three nodes for a small change, five to eight for a feature, with at most two or three side by side (`max_parallel` caps the
workers at once and the user's account has rate limits). More nodes do not finish sooner unless they really are independent. When
unsure, fewer and larger nodes, cut by files. A node that could not be told apart from the whole job is not a graph; do it directly.
