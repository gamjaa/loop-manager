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
priority: critical
---

## Purpose

Use the deployed Planner-judgment and Manager-retirement policy to perform the requested one-time Requirement audit and verify automatic recovery of valid implementation work, starting with G-142. Keep the existing Requirement goals and execution history while distinguishing real missing implementation from premature archive, stale feedback or source-state ambiguity. R-055 supplies the retained-work and scoped-recovery constraints; this plan does not implement its remaining planning-error or plan-deletion features.

## Acceptance Criteria

- G-157 is actually MERGED and cron is using its confirmed new runtime before accepting deployed audit results. Planner makes semantic judgments; Manager owns deterministic audit state, moves and publication. Worker/Reviewer do not replace those roles with ad hoc completion declarations.
- The audit records its pinned target set, Requirement bytes and source relationships, evidence, outcomes and resumable progress. Unfinished sources and historical archives are covered; repeated external runs reuse unchanged completed audit inputs.
- Proven incomplete Requirements stay active or are restored byte-identically through Manager publication. Uncertain, superseded or withdrawn historical goals are not revived without the existing decision process. Missing behavior is connected to legitimate follow-up planning from active input.
- Audit and recovery readiness evidence covers preserved G-142, G-144 and G-145 work without forced premature Requirement archive. These plans depend on actual G-158 Merge; their Worker verification, Manager full verify, Review and PR publication are checked in each resumed plan afterward and are not prerequisites for completing G-158. Unrelated true scope violations and approval blocks are not cleared.
- Audit and recovery retain existing plan IDs/goals/dependencies, sessions, workspaces, PRs, failure feedback and maintenance state. Evidence identifies any remaining real blocker instead of repeatedly resetting retries.

## Implementation

- Inspect Manager-owned audit and retirement receipts from the G-157 runtime. Observe the existing cron execution; do not directly start Loop Manager, run the full verification script, or perform semantic Requirement retirement in the Worker.
- Verify the target inventory and classify retained, restored, fulfilled, uncertain and explicitly superseded/withdrawn outcomes using the recorded Planner evidence. Record concise audit/recovery evidence in this plan's Decision Log; leave Requirement content and source paths unchanged.
- Confirm that current active R-053/R-055 and source-state facts survive the new lifecycle and that stale legacy archive targets cannot force their relocation. Let normal general planning identify real remaining work after its related-plan completion gate, rather than declaring a partial refactor as full Requirement implementation.
- Verify recovery readiness for G-142, G-144 and G-145 with current candidate/input evidence and the normal dependency and safety gates. Do not start their Workers before G-158 is actually MERGED; verify execution and PR publication in each dependent plan after that Merge. Any operational single-plan dispatch is Manager-owned, explicitly authorized and audited; Worker does not manually edit production SQLite incidents or fabricate Merge states.
- If audit/recovery exposes a defect in the new lifecycle, fix only the relevant audit, persistence, publication or resume boundary and add a targeted regression. Preserve existing source/dependency, ownership, verification and user-decision protections.

## Validation

- Read Manager audit, runtime handoff and execution evidence for pinned inputs, byte preservation, restart reuse and recovery readiness and preserved dependent-plan state. Document missing evidence and real unresolved causes explicitly.
- For a code correction, Worker runs only the affected targeted audit/retirement/recovery unit tests. For evidence/document-only work, state why no new executable unit test is needed. Manager executes full verify through its normal lifecycle.
- Reviewer compares the recorded audit/recovery evidence, current plan scope and any correction diff. A candidate is not treated as MERGED until the user actually Merges its PR.

## Progress

- [ ] Confirm G-157 runtime adoption and pinned audit results.
- [ ] Verify Requirement retention/restoration and follow-up handling.
- [ ] Record recovery readiness for G-142/G-144/G-145 and hand execution validation to each plan after G-158 Merge.
- [ ] Record remaining real blockers without clearing unrelated safety causes.

## Decision Log

- This is the one-time audit and deployed recovery portion already requested in R-068, split from the code implementation so deployment evidence can be collected after G-157 Merge.
- R-055 is a direct recovery constraint for preserving unresolved work and not broadly clearing independent blocks; this plan does not claim to complete all of R-055.
- The pending source links are legitimate unfinished work. They prevent premature legacy archive during G-157 implementation; final Requirement fulfillment is still determined by general Planner after all related implementation plans Merge.
- The current maintenance cycle also excludes ordinary newly published follow-ups. Register and select this plan through an explicitly authorized single-plan recovery admission after its dependency is MERGED; preserve the existing maintenance cycle and normal single-project execution boundary.

- 2026-10-05 사용자 실행 순서 결정: G-158을 먼저 수행하고 G-142·G-144·G-145의 Merge 선행 dependency로 등록한다. 감사·복구 준비와 실제 후속 실행 검증을 분리해 상호 대기를 피한다. G-138의 R-070 복구 fixture·Git 관측 회귀는 G-160 별도 수정이며 이 계획으로 성공 처리하지 않는다. 사용자는 기존 유지보수 주기와 작업물·세션을 보존하는 G-158 단일 계획 복구 admission을 승인했다. 원격 개정 채택과 당시 실행 슬롯·소유권·dependency·안전 조건을 확인한 Manager 절차로만 실행하며 SQLite 사건을 임의 초기화하지 않는다.
