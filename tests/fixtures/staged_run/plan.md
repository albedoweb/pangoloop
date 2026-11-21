# Plan: task T2, cut p1

An invented plan with the shape of a real one: one folder of a run holds this file and a `human/` folder of requests.

## Nodes

### styles
- label: Styles
- category: default
- role: build
- depends_on: none
- write_scope: src/styles
- TASK: split the stylesheet.
- DELIVERABLE: two files.
- SCOPE: src/styles only.
- VERIFY: npm run build
- STOP WHEN: the build passes.

### fixture
- label: Fixture
- category: default
- role: build
- depends_on: styles
- write_scope: tests
- TASK: make the sample data a fixture.
- DELIVERABLE: one file.
- SCOPE: tests only.
- VERIFY: npm test
- STOP WHEN: the tests pass.

### play
- label: Play
- category: default
- role: build
- depends_on: fixture
- write_scope: src/play
- TASK: name the default level.
- DELIVERABLE: a change.
- SCOPE: src/play only.
- VERIFY: npm test
- STOP WHEN: the tests pass.

### exports
- label: Exports
- category: default
- role: build
- depends_on: play
- write_scope: src
- TASK: remove unused exports.
- DELIVERABLE: a change.
- SCOPE: src only.
- VERIFY: npm run lint
- STOP WHEN: lint passes.

### acceptance
- label: Acceptance
- category: default
- role: acceptance
- depends_on: exports
- write_scope: nothing
- TASK: run the commands.
- DELIVERABLE: exit codes.
- SCOPE: nothing.
- VERIFY: none
- STOP WHEN: all exit 0.

### review
- label: Review
- category: review
- role: review
- depends_on: acceptance
- write_scope: nothing
- TASK: read the diff.
- DELIVERABLE: a verdict.
- SCOPE: nothing.
- VERIFY: none
- STOP WHEN: a verdict is written.

### acceptance-final
- label: Final acceptance
- category: default
- role: acceptance
- depends_on: review
- write_scope: nothing
- TASK: run the commands again.
- DELIVERABLE: exit codes.
- SCOPE: nothing.
- VERIFY: none
- STOP WHEN: all exit 0.

## Task statement

An invented task: tidy a small web project in four parts.
