---
id: G-136
title: Normalize External Observation Failures
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

Keep absent evidence separate from failed Git, GitHub and filesystem observation. Several callers currently turn command failure, transient SQLite error or malformed `gh` response into a missing document, absent PR head or definitive ownership refusal; others send operational failures into Agent correction. These paths can consume retries or leave work manually blocked without a valid safety judgment.

## Acceptance Criteria

- Catalog SHA backfill, revision document reads, execution ownership checks, direct rebase PR head lookup, GitHub PR observation, managed rebase publication, bounded maintenance Git context and Planner validation distinguish confirmed absence from command, database, parsing or I/O failure.
- Recoverable observation failure preserves the existing work, snapshot, session and relevant safety gate for the next authorized retry; confirmed absence and actual ownership violation retain their current decisions. Failure disposition does not depend on exception message wording.
- Existing callable interfaces, Git/PR authority, SQLite format, Agent correction limits and documented user-visible error categories are preserved except for correcting misclassified observation failures.

## Implementation

- Use the existing per-domain error/result types and command adapters to carry stable absence, transient and unsafe outcomes. Narrow catches at call sites rather than adding a process-global error wrapper.
- Keep decision ownership in catalog, execution, planning and rebase services; tests should inject failures at each external edge and verify no unsafe operation follows uncertainty.
- The responsibility boundary is external observation adapters versus the services that decide whether work can proceed. Preserve user behavior, public contract and storage format by retaining current decisions for confirmed evidence and expressing operational failure through existing recovery results.

## Validation

- Worker runs targeted unit tests in `tests/test_catalog_migration.py`, `tests/test_execution_publication_phase.py`, `tests/test_planner_validation_recovery.py`, `tests/test_managed_rebase_lifecycle.py` and relevant GitHub observation tests for absent versus failed reads, malformed JSON, lock/I/O errors and changed exception messages.
- Full verify confirms existing catalog, Planner correction, ownership, rebase and PR recovery behavior, including safe refusal when evidence is missing.

## Progress

- [x] Define failure disposition at existing external observation boundaries.
- [x] Add fault injection for absence, transient error and unsafe uncertainty.

## Decision Log

- Manifest findings 3, 6, 7, 11, 13, 15, 19, 23 and 24 share an evidence-classification responsibility. The plan leaves policy decisions in their current services and does not reinterpret a failed lookup as permission to proceed.
- Observed caller paths: `loop_manager_internal/catalog/catalog_service.py`, `loop_manager_internal/execution/plan_validation.py`, `loop_manager_internal/execution/execution_ownership_handler.py`, `loop_manager_internal/git/direct_rebase_service.py`, `loop_manager_internal/git/managed_rebase_publication.py`, `loop_manager_internal/git/github_observation.py`, `loop_manager_internal/maintenance/maintenance_git.py`, `loop_manager_internal/planning/planner_validation.py`.
- Catalog SHA backfill now stops on failed Git history before changing SQLite or recording a safety block. Revision documents and Planner base paths use successful tree evidence to confirm absence; file stat/read and READY revision I/O failures remain operational, while confirmed missing READY declarations retain the validation refusal.
- GitHub observation validates command success and the requested PR JSON shape before returning a PR or confirmed empty list. The services retain PR identity and publication decisions. Managed rebase publication, direct rebase proof, checkpoint ownership and both plan/project blocked recovery use typed retry results for failed Git/SQLite observations; valid remote mismatch, ancestry failure and rejected rebase state remain safety refusals. Recovery disposition uses result and exception types, not diagnostic wording.
- Bounded maintenance Git context marks command/stream failure separately from confirmed missing baseline or nonancestor evidence; preparation pauses without saving a Planner error for failed observation. Targeted fault injection covers these distinctions.
- Rebase publication marks failed remote, PR, current-state and ownership reads with a distinct retry type; a rejected push keeps its saved intent and existing blocked-release result. Published review and project-block ownership compare one exact remote ref/OID record, while empty output remains confirmed absence and malformed records remain retryable.
