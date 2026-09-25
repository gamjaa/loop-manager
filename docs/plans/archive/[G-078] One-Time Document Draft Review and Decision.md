---
id: G-078
title: One-Time Document Draft Review and Decision
sources:
  - docs/requirements/[R-027] Shared Quality Guidance and Full Project Refactoring Assessment.md
depends_on:
  - G-059
complexity: high
risk: high
priority: normal
---

## Purpose

G-059 records document improvement findings without changing user-owned specifications. Turn only meaning-preserving findings from this repository's one-time assessment into durable, reviewable drafts, and bind each user decision to the exact draft and source revision. This plan ends at the decision boundary; applying an approved draft belongs to a separate ExecPlan.

## Acceptance Criteria

- For this Loop Manager repository only, create one separate candidate per affected PRODUCT, active Requirement or ARCHITECTURE document. Keep the original file and base branch unchanged. Exclude generated files, dependencies, archive documents and findings that require a policy or behavior decision.
- Persist each draft outside the source checkout with its target path, complete proposed content and diff, original revision and content hash, proposed content hash, assessment finding identity and explicit pending/approved/rejected state. Make all of these available to the user before accepting a decision; an abbreviated notification or summary is not sufficient review material.
- Accept approval or rejection only for a specific persisted draft identity and content hash. Record the decision durably; a restart, repeated request or unchanged assessment cannot silently create another decision or replace the reviewed bytes. Rejection leaves the source and that finding unchanged except for the decision record.
- Recheck the original revision, content hash and nonterminal ExecPlan source relationships before an approval becomes actionable. If any changed, or meaning preservation is uncertain, retain the old draft and decision for audit but require a new draft and user decision. Do not interpret silence, an unrelated file edit or a generic approval as consent.
- Findings requiring new product behavior, policy choice or altered Acceptance Criteria are not passed through this meaning-preserving path. Existing Worker, PR_OPEN, PLAN_BLOCKED, PROJECT_BLOCKED and maintenance ownership gates remain in force. No AI Agent edits the original specification or common/project AGENTS.md during draft creation or decision recording.

## Implementation

- Extend the G-059 self-only assessment record in `maintenance_state.py` with a narrow draft-and-decision store keyed by finding and original document identity. Preserve the original bytes, generated candidate bytes, complete diff and user decision through interruption without using the working tree as the draft store.
- Give the user a review artifact and explicit decision entry point in the existing managed state/CLI boundary. Display or point to the complete content, diff and hashes before a decision, and validate the exact identity on submission. Keep this flow separate from the AGENTS.md guidance approval protocol.
- At decision time compare the latest `origin/<base-branch>` document and nonterminal catalog with the saved source identity. Record stale or ambiguous candidates as requiring fresh review rather than automatically applying them. Expose only a validated approved draft to the later document publication flow.

## Validation

- Worker runs focused draft-state and decision cases in `tests/test_maintenance_state.py` and the directly changed CLI/assessment boundary tests.
- Cover one-time self-only scope, complete review artifact, per-document approval/rejection, duplicate decisions, interruption and restart, changed original bytes/revision, changed source relationships, uncertain meaning, and policy-change findings kept out of the draft path.
- Full verify must retain periodic maintenance behavior, existing approval flows, source checkout immutability and active-work ownership gates.

## Progress

- [ ] Durable per-document draft and review artifact
- [ ] Exact user decision and stale-source gate
- [ ] Focused self-only, restart and consent tests

## Decision Log

- This is the draft and decision half of R-027. An approval records consent for exact bytes; it does not modify a source document, create a PR or bypass later review.
