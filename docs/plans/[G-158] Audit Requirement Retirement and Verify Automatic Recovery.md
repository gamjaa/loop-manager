---
id: G-158
title: Audit Requirement Retirement and Verify Automatic Recovery
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-068] Planner Judgments and Manager-Owned Requirement Retirement.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-157
complexity: medium
risk: high
priority: high
---

## Purpose

Use the deployed Planner-judgment and Manager-retirement policy to perform the requested one-time Requirement audit and verify automatic recovery of valid implementation work, starting with G-142. Keep the existing Requirement goals and execution history while distinguishing real missing implementation from premature archive, stale feedback or source-state ambiguity. R-055 supplies the retained-work and scoped-recovery constraints; this plan does not implement its remaining planning-error or plan-deletion features.

## Acceptance Criteria

- G-157 is actually MERGED and cron is using its confirmed new runtime before accepting deployed audit results. Planner makes semantic judgments; Manager owns deterministic audit state, moves and publication. Worker/Reviewer do not replace those roles with ad hoc completion declarations.
- The audit records its pinned target set, Requirement bytes and source relationships, evidence, outcomes and resumable progress. Unfinished sources and historical archives are covered; repeated external runs reuse unchanged completed audit inputs.
- Proven incomplete Requirements stay active or are restored byte-identically through Manager publication. Uncertain, superseded or withdrawn historical goals are not revived without the existing decision process. Missing behavior is connected to legitimate follow-up planning from active input.
- Preserved G-142 work resumes automatically through targeted Worker verification, Manager full verify, Review and PR publication without forced R-053/R-055 archive. Its PR waiting and actual user Merge are distinguished. Unrelated true scope violations and approval blocks are not cleared.
- Audit and recovery retain existing plan IDs/goals/dependencies, sessions, workspaces, PRs, failure feedback and maintenance state. Evidence identifies any remaining real blocker instead of repeatedly resetting retries.

## Implementation

- Inspect Manager-owned audit and retirement receipts from the G-157 runtime. Observe the existing cron execution; do not directly start Loop Manager, run the full verification script, or perform semantic Requirement retirement in the Worker.
- Verify the target inventory and classify retained, restored, fulfilled, uncertain and explicitly superseded/withdrawn outcomes using the recorded Planner evidence. Record concise audit/recovery evidence in this plan's Decision Log; leave Requirement content and source paths unchanged.
- Confirm that current active R-053/R-055 and source-state facts survive the new lifecycle and that stale legacy archive targets cannot force their relocation. Let normal general planning identify real remaining work after its related-plan completion gate, rather than declaring a partial refactor as full Requirement implementation.
- Verify G-142 recovery with current candidate/input validation and the normal execution checks. Any operational single-plan dispatch is Manager-owned, explicitly authorized and audited; Worker does not manually edit production SQLite incidents or fabricate Merge states.
- If audit/recovery exposes a defect in the new lifecycle, fix only the relevant audit, persistence, publication or resume boundary and add a targeted regression. Preserve existing source/dependency, ownership, verification and user-decision protections.

## Validation

- Read Manager audit, runtime handoff and execution evidence for pinned inputs, byte preservation, restart reuse and actual G-142 progress. Document missing evidence and real unresolved causes explicitly.
- For a code correction, Worker runs only the affected targeted audit/retirement/recovery unit tests. For evidence/document-only work, state why no new executable unit test is needed. Manager executes full verify through its normal lifecycle.
- Reviewer compares the recorded audit/recovery evidence, current plan scope and any correction diff. A candidate is not treated as MERGED until the user actually Merges its PR.

## Progress

- [ ] Confirm G-157 runtime adoption and pinned audit results.
- [ ] Verify Requirement retention/restoration and follow-up handling.
- [ ] Verify automatic G-142 validation, Review and PR publication.
- [ ] Record remaining real blockers without clearing unrelated safety causes.

## Decision Log

- This is the one-time audit and deployed recovery portion already requested in R-068, split from the code implementation so deployment evidence can be collected after G-157 Merge.
- R-055 is a direct recovery constraint for preserving unresolved work and not broadly clearing independent blocks; this plan does not claim to complete all of R-055.
- The pending source links are legitimate unfinished work. They prevent premature legacy archive during G-157 implementation; final Requirement fulfillment is still determined by general Planner after all related implementation plans Merge.
- The current maintenance cycle also excludes ordinary newly published follow-ups. Register and select this plan through an explicitly authorized single-plan recovery admission after its dependency is MERGED; preserve the existing maintenance cycle and normal single-project execution boundary.
