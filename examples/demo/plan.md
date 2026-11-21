# Plan: top_words for a small text-statistics module

An invented example for the page's picture: a plan with two build nodes side by side, a script node, an acceptance and a review.
The repository (made by `examples/demo/run.sh`) is `textstats.py` with `count_words` and its test. Repository: `{{REPO}}`,
branch `{{BRANCH}}`. With the stand-in for a model the build nodes write no real code; the plan is here for its shape.

All nodes work in the same folder, the repository folder.

Rules for every node: change only the files your node names; run only the commands your node names; do not commit.
Answer in a few lines. A node that is not blocked writes no `NEEDS ...:` line at all.

## Nodes

### parser
- label: Write top_words
- kind: build
- role: coder
- depends_on: none
- write_scope: textstats.py
- TASK: Add `top_words(text, n)` to `textstats.py`: the `n` most frequent words of `text`, lower-cased, most frequent first, ties in order of first appearance. Do not change `count_words`.
- DELIVERABLE: the changed `textstats.py`; in the handoff, one line saying what you added.
- SCOPE: only `textstats.py`.
- VERIFY: `python3 -m unittest -q`
- STOP WHEN: the command passes.

### tests
- label: Write the tests of top_words
- kind: build
- role: test-writer
- depends_on: none
- write_scope: test_textstats.py
- TASK: Add tests for `top_words(text, n)` to `test_textstats.py` (a new method of the existing test class): ordering, ties, `n` larger than the number of words, empty text.
- DELIVERABLE: the changed `test_textstats.py`; in the handoff, one line saying what you covered.
- SCOPE: only `test_textstats.py`.
- VERIFY: `python3 -m unittest -q`
- STOP WHEN: the tests are written; they may fail until the other node's work is accepted.

### check
- label: Run the tests, by the script
- kind: script
- depends_on: parser, tests
- write_scope: nothing
- STEPS: run `python3 -m unittest -q`

### accept
- label: Acceptance
- kind: acceptance
- depends_on: check
- write_scope: nothing
- TASK: the script runs the command in VERIFY; no model is asked.
- DELIVERABLE: exit 0.
- SCOPE: the whole repository.
- VERIFY: `python3 -m unittest -q`
- STOP WHEN: the command exits 0.

### review
- label: Review of the whole change
- kind: review
- role: reviewer
- depends_on: accept
- write_scope: nothing
- TASK: Read the whole change against the statement below and give a verdict.
- DELIVERABLE: the findings, if any, and the verdict.
- SCOPE: read only.
- VERIFY: `python3 -m unittest -q`
- STOP WHEN: you have given the verdict.

## Task statement

R1. `textstats.py` has `top_words(text, n)` as the task of the node `parser` describes it, and `count_words` is unchanged.
R2. `test_textstats.py` tests `top_words` and keeps the existing test.
R3. `python3 -m unittest -q` exits 0.
