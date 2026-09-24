---
id: G-071
title: Minimal Reviewer Validation Context
sources:
  - docs/requirements/[R-035] Minimal Reviewer Validation Context.md
depends_on:
  - G-070
complexity: medium
risk: medium
priority: low
---

## Purpose

`reviewer_prompt` repeats the Worker test report and up to 6000 characters of successful verify output even though Review is entered only after validation. Keep the prompt focused on the diff and exceptional test-coverage facts.

## Acceptance Criteria

- Reviewer prompt excludes successful targeted-test and full-verify status, command, raw output, tail and per-test detail. Include only a concise reason when relevant targeted tests could not be run.
- Before any Reviewer invocation, program state proves a valid Worker result and required targeted-test disposition, successful full verify, and equality between the verified candidate revision/base and the revision presented to Reviewer. Missing, failed or stale evidence prevents invocation and returns to the appropriate correction/reverification path.
- Reviewer still reads code, tests, verify-script diff and source documents to judge coverage and weakening without running commands. Verification output and results remain stored for audit and Worker failure feedback, including after restart and Reviewer protocol retry.
- The G-070 rebase-first and publication-time revalidation gates remain in effect. A changed HEAD or base after verify must be validated before Review.

## Implementation

- Parse the `TARGETED_TESTS` portion of the validated Worker result and pass only an exceptional no-test reason to `reviewer_prompt` in `loop_manager.py`; do not infer success from arbitrary Worker prose.
- Add a review-entry evidence check in `execution_handlers.py` using the durable verified HEAD/base identity from G-070, current Git state and the saved verify result. Keep Reviewer session recovery and protocol-correction context distinct from success logs.
- Preserve `validation_feedback_state.py` raw result records and existing read-only Reviewer scope checks. Do not add a project-specific success-output parser.

## Validation

- Worker runs focused prompt and review-entry cases in `tests/test_loop_manager.py`, `tests/test_execution_handlers.py` and `tests/test_execution_revision.py`.
- Cover successful tests with long verify output, no runnable targeted test with reason, missing or failed evidence, HEAD/base drift, rebase before first verify, Reviewer retry/restart and unchanged Worker feedback audit.
- Full verify must retain Reviewer read-only, no-test-command and verify-script-weakening review coverage.

## Progress

- [ ] Minimal Reviewer prompt and exceptional targeted-test reason
- [ ] Durable current-revision Review entry gate
- [ ] Targeted prompt and stale-evidence tests

## Decision Log

- The program validates success; the Reviewer receives only facts needed to assess the changed files.
