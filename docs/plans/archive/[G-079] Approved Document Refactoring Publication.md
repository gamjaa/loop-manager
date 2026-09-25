---
id: G-079
title: Approved Document Refactoring Publication
sources:
  - docs/requirements/[R-027] Shared Quality Guidance and Full Project Refactoring Assessment.md
depends_on:
  - G-078
complexity: high
risk: high
priority: normal
---

## Purpose

An exact draft approved under G-078 still needs its own meaning-preserving ExecPlan, verification, read-only Review and user-merged PR. Connect that approved draft to the existing guarded publication flow without giving an AI Agent permission to edit user-owned specification originals.

## Acceptance Criteria

- At a safe selection boundary, a newly actionable approval triggers planning even when source and specification fingerprints are unchanged. For each such draft, create a separate document-only ExecPlan that identifies the approved draft, exact target, original revision and hashes, intended diff and R-027 source. Publish and execute it through the existing catalog, dependency, managed worktree and one-PR-per-plan rules. Rejected, pending or stale drafts create no implementation plan or PR; repeated runs do not duplicate a plan for the same approval.
- Immediately before applying the draft and again at final publication against the latest remote base, revalidate the original revision/content and relevant nonterminal plan source relationships. If the original changed or goal/source preservation cannot be shown, stop that document plan for a new user decision without altering the remote original. Keep the approval and failed check for audit.
- Program code applies only the approved bytes to the intended specification in a managed worktree. Worker and other AI Agents do not directly edit the original before or after approval. The resulting specification diff must equal the approved diff exactly; unrelated PRODUCT, Requirement, ARCHITECTURE or AGENTS.md changes are rejected.
- The document plan records relevant targeted unit test results or the concrete reason none apply, then passes the Loop Manager's full verify. A read-only Reviewer checks document roles, previous behavior and decisions, Acceptance Criteria preservation, cross-document consistency and exact equality with the approved diff. Uncertain meaning or a mismatched diff prevents publication and requests a new user decision. A separate PR is created only after these gates, and the base branch reflects the document change only after the user merges that PR.
- Interruption during plan creation, draft application, verification, PR publication or Merge observation resumes without duplicate plans, application or PRs. Other registered projects, periodic maintenance, active work ownership and existing user-document restrictions remain unchanged.

## Implementation

- Carry the G-078 approval identity into general or maintenance planning as a self-only scheduling trigger so the Planner emits one R-027 document ExecPlan per approved document, with a durable mapping from approval to plan ID. Advance that trigger only after the plan is published and registered. Use the existing catalog CAS, planning publication and dependency checks rather than modifying any existing plan outside an allowed snapshot.
- Add a document-only execution path in the maintenance/execution coordinator: prove original and catalog identity, apply exact approved bytes in the plan's managed worktree, reject any Agent-authored specification edits, and reuse existing verify, Reviewer, PR and recovery state transitions. Persist enough phase evidence to resume safely after each external side effect.
- Give Reviewer the original and approved content/diff identities and require an explicit meaning-preservation and exact-diff verdict before publication. Keep the document PR separate from code refactoring PRs and do not alter AGENTS.md or extend this approval mechanism to other projects.

## Validation

- Worker runs focused approval-to-plan, exact-application, Reviewer and publication-resume cases in `tests/test_maintenance_planning.py`, `tests/test_execution_handlers.py` and the directly changed document approval tests.
- Cover pending/rejected/stale approvals, unchanged source with a new approval, one plan per accepted draft, changed original or source graph before application or publication, exact bytes and forbidden Agent edits, semantic uncertainty, successful validation and Review, restart at each side-effect boundary, duplicate PR prevention and user Merge before base change.
- Full verify must retain catalog ownership, managed-worktree safety, targeted-test responsibility, full verify and read-only Review gates, normal code PR publication and periodic maintenance behavior.

## Progress

- [ ] Approved draft to unique document ExecPlan mapping
- [ ] Program-owned exact application and revalidation gate
- [ ] Review, PR and interrupted-publication regression cases

## Decision Log

- The user approves a particular draft, then separately decides whether to Merge its PR. Neither decision grants an AI Agent direct write access to user-owned originals.
