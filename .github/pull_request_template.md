## What and why

<!-- What is now true, and why. One fault or one change per pull request. -->

## Checks

- [ ] The branch is not `main`, and it contains the current `main` (`git merge main`)
- [ ] `python3 -m unittest discover -s tests` passes (state the result: tests run, skipped, expected failures)
- [ ] A new behaviour has a test that fails without it; stubs only, no real model, service or network
- [ ] No dependency added (standard library only)
- [ ] One fault or one change per commit; messages in the style of `git log`
- [ ] The documents are changed with the code (`README.md`, `ROADMAP.md`, `docs/SETUP.md` and the comments of the presets, where they apply)
- [ ] No secret, token, personal path or copy of another person's text or image; `git status` is clean after the tests
