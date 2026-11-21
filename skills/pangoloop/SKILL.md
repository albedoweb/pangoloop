---
name: pangoloop
description: Run a piece of coding work as a graph of coding-agent nodes with the `pangoloop` command: parts that run side by side in their own git worktrees, scripted checks, an independent review by another model, and a local web page of the run. Use it when the user asks for a piece of work to be done by a team or graph of agents, in parallel parts, with checks and a review, or says "pangoloop". Do not use it for a change of one file, a question, or an exploration; do those directly.
version: 0.1.0
license: MIT
---

# Pangoloop: work done by a graph of agents

`pangoloop` is a command-line tool. It takes a **plan** (a Markdown file of nodes with dependencies), starts each node as a
separate coding-agent session in its own git worktree, gathers the work, runs the acceptance commands by script, sends the whole
change to a reviewer, and serves a local page of the run. You are the one who plans and watches: you turn the user's request into
a plan, start the run, answer the questions the nodes ask, and report the result. The workers are other sessions, not you. The
tool is a script; the quality of the result depends mostly on the plan you write, so the plan gets the care.

Read the files in `references/` when a step points to one; each is one page and stands alone. Ask the user with your own tool for
questions (in Claude Code, AskUserQuestion; elsewhere a plain message), all the questions of a step at once, never one by one.

## When not to use a graph

A graph costs several model sessions and some minutes of setup. Do the work yourself, directly, when it is a change in one or two
files, a question about the code, an exploration, or something you can finish faster than you can write its plan. Say so to the
user in a sentence and go on. Use the graph when the work has parts that can proceed independently, checks that a script can run,
and enough size that an independent review earns its price.

## The flow

**1. Check the ground.** Run `pangoloop version`. If the command is not found, tell the user the two ways to get it and stop until
they have run one or told you to: `pipx install "git+https://github.com/albedoweb/pangoloop.git"`, or, in a virtual environment,
`python3 -m venv .venv && .venv/bin/pip install "git+https://github.com/albedoweb/pangoloop.git"` (then use `.venv/bin/pangoloop`).
Python 3.11 or newer and `git` are needed. Then check that the folder is a git repository with at least one commit (`git log -1`)
and look at `git status --short`. A run integrates and commits in the working folder, so **ask the user to commit or stash their own
changes to tracked files first**: they would be mixed into the run's change. Untracked files are carried through and left as they
were. Tell them this plainly; do not touch their changes yourself.

**2. The models, once.** If the project has no `pangoloop.toml`, ask the user in one message:
- which runner they use (Claude Code or Hermes);
- which model plays which role: the **coder** (most nodes: cheap, steady at following a literal instruction), the **reviewer** (a
  different model from the coder's, ideally from another vendor, because a model tends to approve what it would have written), the
  **interface model** (only if the work has a screen: it must accept images), and optionally one or two **readers** who check the
  plan before the run;
- whether to cap what one run may spend, in dollars (optional).

Offer a preset as the default: `claude-code-only`, `hermes-mixed` or `one-model`. Then run `pangoloop init` with their answers as
flags: `--preset`, `--runner`, `--route default=<model> --route review=<model> --route ui=<model>`, and one `--reader <name>=<model>`
for each reader they want (for Hermes write `model@provider`). Give every model as a flag. The preset fills what you leave out with
its own models, and `--reader` replaces the preset's readers (without it they stay, and one is a larger model). Editing
`pangoloop.toml` by hand to fix a model means a flag was missed. Read `pangoloop config` afterwards: no route or reader may hold a
model they did not choose (a preset may route a category of its own, such as `simple-work`; set it with `--route` too). A cap goes in
`pangoloop.toml` under `[limits]` as `max_cost_usd = <n>`; it stops new workers once the known cost passes it and counts only models
that have a price (`references/commands.md`). Commit `pangoloop.toml`, `.gitignore`, `.pangoloop/plans` and `.pangoloop/roles`.
If `pangoloop.toml` already exists, show the routes in two lines (from `pangoloop config`) and do not ask again unless they want a change.

**3. Understand the work.** Read the user's request and the repository: how tests, build and lint run, the layout, the conventions.
Ask only the questions whose answers would change the plan, at most three, all at once. Run the project's own check commands once
before planning: a plan built on a red starting point fails for reasons that are not its own, so if it is red, say so and ask
whether the plan should include fixing it.

**4. Write the plan** into `.pangoloop/plans/<short-name>.md` by the rules in `references/cutting-work.md` and the format in
`references/plan-format.md` (**every field of a node is exactly one line**, however long: a wrapped line loses its tail and the
worker never sees it). It holds:
- the statement of the work: what must be true afterwards as numbered rules, and what must not change;
- nodes with a `kind`, a `role`, `depends_on`, `write_scope`, a task, the exact check commands and when to stop;
- nodes that run side by side share no file, and every file the work may touch belongs to exactly one node;
- mechanical work is a script node, not a model;
- an acceptance by script after the build nodes, then a review by another model, and a second acceptance after the review when the
  plan has more than a few nodes (a repair after a review can break what passed).

Small work is a small plan: two or three nodes are normal. `references/roles.md` says which roles exist (`pangoloop roles`).

**5. Check the plan.** Commit the setup files and the plan first, so that readers who look at the repository do not take them for
part of the work. Run `pangoloop preflight <plan> --strict --reader <name> --out <a folder outside the repository>`, one `--reader`
per reader the user chose (with none chosen, skip this: the engine still refuses a malformed plan before anything starts). Readers
are model sessions that read the plan as a node would and report contradictions. Take every finding seriously and fix the text, not
the reader, then run it again. Stop after three rounds (the long form for a whole stage allows six): a reader can go on finding new
things in a plan that is already sound, and each round costs a model session. Findings still open after the third go into the
summary for the user, each with your view of it. A finding that only says the reader was not told who "the coordinator" is, or that
no node is assigned to it, you may set aside.

**6. Show the user a summary and wait for their yes.** In their language, as a short list: the nodes in order, who does each (role and
model), what will be checked, what they will have at the end (the change, uncommitted in their folder), and what it cannot promise (the
checks are only as good as the commands in the plan; a review by a model is an opinion). If they said at the start to go without
asking, go.

**7. Start it.** `pangoloop run <plan> --detach --view --json` prints one JSON object. Give the user `page_run` (the address of the
page, open on this run) first, then the run's name (`run`). The page is local, listens on 127.0.0.1 and has no login: do not
suggest forwarding it.

**8. Stay with it.** Wait for requests with `pangoloop requests wait <run> --json` (in the background if your agent can; it exits 3 with
`{"run_over": true, ...}` when the run is over: that is the end of the run, not a failure). Details are in `references/requests-and-rulings.md`. In short: a request
`for: coordinator` you answer yourself from the plan's statement and the repository with `pangoloop requests answer`: answer the
question that was asked, say which rule you ruled on, never widen the work. The answer becomes a ruling that every later node reads.
A request `for: person` goes to the user in the node's own words, unchanged, with what you would answer; you never do yourself what
a node said only a person may do. If a node cannot be helped, say so and offer to stop. Do not edit the repository while a run is
going: the engine commits the folder as it stands.

**9. Report at the end** from `pangoloop status <run> --json`: the verdict; what each node did, one line each; where the result is;
what the review said; what was spent if the cost is known (`cost.usd` is null when unknown: say "unknown", not zero; it counts the run's workers, not the
readers' sessions of step 5); what is left.
**Where the result is, exactly:** the accepted work is in the project's working folder as one uncommitted change on top of the
commit the run started from (`git status`, `git diff`). The run commits nothing to the user's branch (the setup commits of steps 2 and 5 are yours: say so). The nodes' branches `sg/<node>`
and the chain `refs/pangoloop/<run>/tip` remain in the repository. If the verdict is not `verified`, say why in plain words (the
node that failed and its reason, a review that did not pass, a limit) and offer the next step: fix the plan and
`pangoloop resume <run>`, or stop.

## What you never do, and what to tell the user

- You never push, merge or delete branches for the user, and you never commit the result for them unless they ask. The branches
  `sg/*` are theirs to delete (`git branch -D 'sg/*'`).
- The page has no login: anyone who can reach the address can read the runs and answer requests. Keep it on the machine.
- Workers run shell commands as the user. The deny list of commands (`git push`, `sudo`, `rm -rf`, `ssh` and some others) is a
  seat belt, not a sandbox: other spellings of the same command get through, workers can read files outside their worktree, and
  they can reach the network. Do not point a run at a repository that holds secrets. Say this once, before the first run in a project.
- A run costs model sessions. Say roughly how many (about one per model node, plus repair and review rounds) before you start.
- `write_scope` in a plan is text for the worker; the engine does not enforce it. Keep the nodes' files disjoint in the plan itself.
