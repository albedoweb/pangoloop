# Roadmap

What is planned, and why. Nothing here is promised: it is the order the author would work in, and
each item says what it answers. What exists today is in the README, `docs/SETUP.md` and `pangoloop --help`.

## Planned

**Rounds with a score, and attempts side by side.** A node that is an experiment (tuning a value, a search) is not "done when
the checks pass": it has a score. Two shapes of one mechanism: *rounds*, where a round changes one thing, scripts measure it,
a gate compares with the targets and with the last kept round, and the round is kept or dropped until the targets hold or a
limit is reached; and *attempts*, where N workers try the same node in separate copies and the best-scoring one is integrated.
Why: the checks of a normal node answer yes or no, and a plain loop around `pangoloop run` already works for one round at a time
but loses the record of what was kept and why. Both would share one record shape and the cost cap.

**A graph whose plan can be patched during a run.** Today the plan is frozen before a run, and a run can be resumed with a
changed plan only where no accepted node changes. Research changes the work: an open node (a design, an experiment) would end
with knowledge and a patch to the plan, a block of new nodes in a fixed form. The patch goes through `preflight` and a gate (the
coordinator for technique, a person for scope) before the engine adds the nodes. What stays rigid, because it is what made runs
with no questions possible: accepted work is never rewritten, a script schedules and a model does not, every file has one
owner, and a build node cannot re-plan. Why: a long project changes while it runs, and a frozen plan makes every change a
restart.

**Stages.** A plan as a chain of stages, each with its own acceptance and review, and a gate between stages that shows what to
look at (a command, a page). A stage's checks travel with it: reference images regenerated where the stage may change a screen,
hidden tests per stage, a grade per stage in the run's records. Why: the statement of a stage depends on the result of the one
before; today that is solved by hand, by starting a new run.

**Cost limits per node.** A run can stop starting workers when its known cost passes a dollar limit. A node has limits on time
and turns but no limit on money, and what a script step spends on its own model calls is counted only when the command
reports it (a line `PANGOLOOP-SPEND: <usd> [label]` in its output). Why: a single
worker that loops can spend more than the rest of the run, and a limit on turns does not bound the money.

**Isolation of workers.** Today a node's limits are the words of the plan plus what the model harness blocks, in a git
worktree on the same machine; a node can read files outside its worktree, such as the user's ssh configuration. Planned: a node in a container with
its own branch, a scratch volume, the network limited to the model's address and the package registry, only the credentials
that run needs, the result returned as a commit, and the acceptance commands run in a fresh container. The agent that runs a
node and the place it executes would be two separate choices behind two interfaces.

**Secrets.** Today a node can be asked for a credential in a plan's own words and nothing refuses. Planned: a request never asks
for a credential to be pasted; outputs are shown to the person before they go to a node, with a way to cut lines out; commands
the person must run are data the engine knows (an approval step, a deny list for `run`), not prose.

**A better human side for operations work.** A gate that carries exact commands with a copy button, the folder and the
expected result; three answers (done with output, refused with a reason, abort); a queue of open requests that shows what is
blocked behind each; a deadline and reminders per request; a notification that reaches a person away from the machine; a record
of who answered what.

**Smaller items.** A review or a rebase repair that asks keeps the integration lock while it waits. An acceptance that knows the
usual duration of each command and repeats a failing run that took twice as long before a repair worker is called. Fields of
several lines in the plan format. A check that a node's diff stays inside its `write_scope` (today it is required and checked
nowhere). Return only the failing unit of a review, with its own scope, rather than the whole change.
