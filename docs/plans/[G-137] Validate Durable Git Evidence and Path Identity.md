---
id: G-137
title: Validate Durable Git Evidence and Path Identity
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
depends_on: []
complexity: medium
risk: high
priority: normal
---

## Purpose

Centralize strict decoding of Git paths and stored mutation evidence where recovery decisions consume them. Line-based or whitespace-trimmed Git output can change special filenames, while malformed nested journal or rebase snapshots can escape the existing mutation-evidence refusal path.

## Acceptance Criteria

- Planning and worktree changed-path readers preserve leading whitespace and decode NUL-delimited Git paths, including tabs, newlines and backslashes. Current normal path results and path-based planning classifications remain the same.
- Journal and managed rebase baseline readers validate required nested fields before use and classify malformed durable evidence through the existing safe recovery error; they never infer successful Git mutation from a damaged record.
- On uncertainty, worktree, index, refs, checkpoint and stored format remain untouched. Valid historical evidence and public helper behavior remain readable.

## Implementation

- Reuse the repository's existing NUL and snapshot helpers at the Git workspace and planning rebase readers. Validate journal/rebase payloads at deserialization before mutation decision code uses them.
- Keep durable JSON shape and registered Git mutation boundary unchanged; separate decoding from policy decisions.
- The responsibility boundary is path/evidence decoding versus recovery policy. Preserve user behavior, public contract and storage format by retaining valid NUL path results and historical evidence shape while rejecting malformed inputs before a mutation decision.

## Validation

- Worker runs targeted unit tests in `tests/test_managed_rebase_evidence.py`, `tests/test_manager_git_journal.py`, `tests/test_planning_rebase.py` and workspace path tests for special names, explicit Git index mode, malformed nested evidence, no repeated apply and valid legacy replay.
- Full verify confirms ordinary planning path classification, rebase recovery and journal replay across existing suites.

## Progress

- [ ] Consolidate lossless path decoding and evidence validation.
- [ ] Add malformed, replay and special-name regression tests.

## Decision Log

- Manifest findings 12, 14, 16 and 22 identify the shared parsing boundary. Findings 62 and 66 identify direct test evidence needed to guard replay and Git index mode behavior.
- Observed parsing paths: `loop_manager_internal/git/manager_git_journal.py`, `loop_manager_internal/git/workspace_service.py`, `loop_manager_internal/git/managed_rebase_adoption.py`, `loop_manager_internal/planning/planning_rebase.py`.
