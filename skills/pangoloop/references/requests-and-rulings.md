# Requests and rulings

A node that is blocked stops and asks instead of guessing. Its last line is `NEEDS COORDINATOR: <question>` or
`NEEDS HUMAN: <question>`. A node that is not blocked writes no such line, and a line whose question is empty or starts with
`none` is not a request. The engine then waits, and a new worker of the same node continues with the answer.

## The two kinds

- **`for: coordinator`**: a fact the node cannot find, a measurement it cannot take, a clarification of the plan, or an ordinary
  operation the plan requires that its session may not do (for example deleting folders when the tool refuses recursive deletes).
  **You are the coordinator**: you wrote the plan and know the statement. Answer these yourself.
- **`for: person`**: for the user and nobody else: a command the plan lists under `## The person's commands`, anything outside the
  repository or on a live system, anything that cannot be undone. Pass these to the user. If a request marked for the coordinator
  is really one of these, pass it on with `pangoloop requests escalate <run> <id> --note "<why>"`.

A request for the coordinator that nobody answers within 15 minutes escalates itself to the person.

## The loop

```
pangoloop requests wait <run> --json --for any        # prints one request as JSON and exits 0; exit 3: the run is over
pangoloop requests list <run> --json                  # every request, one JSON object per line
pangoloop requests answer <run> <id> --text "..."     # as the coordinator (the default)
pangoloop requests answer <run> <id> --file <file>
pangoloop requests answer <run> <id> --text "..." --by person    # only with the user's own words
pangoloop requests answer <run> <id> --abort          # ends the run as stopped by a person (exit 3)
pangoloop requests escalate <run> <id> --note "..."
pangoloop requests rulings <run>                      # the rulings so far
pangoloop requests rule <run> --text "..."            # a ruling that answers no question
```

A request object has `id` (such as `a-1`), `node`, `for`, `question`, `asked_at`, `state` (`open`, `answered`, `no answer`,
`superseded`), `worker`, `worker_kind`, `answered_at`, `answered_by`. `requests answer` refuses (exit 1) a request that is for the
person unless `--by person` is given: that is a guard against mistakes, not access control, so **use `--by person` only to pass on
what the user actually said**. The page also has a box to answer a request; the user may use it, and then the request is no longer
open.

Wait in the background if your agent can, so that you can talk to the user meanwhile; run `requests wait` again after each answer.

## Answering as the coordinator

1. Read the question and the node's task. Find the answer in the statement, the repository or the project's conventions. If the
   answer is a fact, check it with a command; do not answer from memory.
2. Answer the question that was asked, in a few plain sentences, and name the rule you ruled on ("R2 allows this: the file is in
   the node's write scope"). Do not widen the work: if the answer would add something the statement does not ask for, either it
   is not needed, or it is a change of the plan and the user decides.
3. If two rules collide, say which wins and why, so the next node meets the same answer.
4. If you cannot tell, escalate: guessing is worse than waiting.

## What a ruling is

Every answered request is a **ruling** on the plan. The engine appends each one to `<run>/rulings.md` and puts all of them into
the prompt of every later node, repair worker and review round, headed "Rulings made during this run (they are part of the task;
where a ruling and the statement differ, the ruling holds)". The reviewer is told not to report as a fault something a ruling
required. So an answer is not a private reply to one node: it changes what the rest of the run is judged by. That is why you
answer narrowly and cite the rule. Use `requests rule` for something you notice that every node should know and nobody asked.

## What goes to the user

For a request for the person, tell the user in their language: which node asks, the question in the node's own words **unchanged**
(quote it), what you would answer and why, and how to answer (they can tell you, or use the box on the page). Then wait. You never
run yourself a command that a node said only a person may run, and you never answer such a request in their name.
