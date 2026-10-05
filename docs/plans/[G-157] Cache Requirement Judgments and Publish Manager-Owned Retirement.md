---
id: G-157
title: Cache Requirement Judgments and Publish Manager-Owned Retirement
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-068] Planner Judgments and Manager-Owned Requirement Retirement.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

Remove the premature Requirement archive gate that repeatedly blocks valid implementation work such as G-142. After related implementation plans are MERGED, general Planner supplies an evidence-backed whole-Requirement judgment; Manager preserves, reuses and invalidates it and performs byte-preserving moves at ordinary planning publication. Defer whole-Requirement rechecks while its plans remain outstanding, and retain unresolved requirements without expanding G-142's notification refactor into their remaining implementation. Quick Planner is not part of this completion/retirement flow.

## Acceptance Criteria

- An implementation candidate can pass Worker, full verify, Review and PR publication while its partially implemented Requirement stays active. Worker is no longer required to match a graph-only archive move set, and cached invalid archive targets cannot reinstate that instruction after restart.
- Plan-authoring completion is distinct from implementation fulfillment. Manager makes no retirement-only general or quick Planner calls while related plans/drafts/execution/PR Merge waiting/blocks remain. After the final related plan is MERGED, general Planner checks all Requirement criteria once against the latest merged remote implementation. Newly planned remaining work defers the next full check until those plans finish.
- Durable judgments distinguish fulfilled, unfulfilled and uncertain results and bind criterion evidence to Requirement content, relevant specification and plan relationships, merged implementation and validation evidence. General Planner alone establishes a new fulfilled judgment; an unchanged successful final input is not assessed again on restart or publication retry.
- Specification changes use ordinary general replanning even during execution. Retirement itself never calls quick Planner: Manager reuses judgments from proven matching inputs, invalidates changed related implementation/test/plan evidence, and requests one general full check when related unfinished work is absent. Preserve ordinary plan-validity/change-impact checks as a separate existing flow. Intermediate Merges and unrelated changes do not cause full rechecks; the final related Merge triggers the first check for a new eligible input. Uncertain validity does not imply completion.
- Manager archives only after a valid post-Merge full judgment with unambiguous sources and no unresolved related plan/draft. It publishes moves through existing planning publication, including mixed new-plan/move batches and move-only batches, without requiring a separate PR or approval for a verified content-preserving move.
- Original Requirement bytes, paths in ExecPlan sources, IDs, execution history and unresolved work survive moves and restoration. Race, collision, interruption, retry, remote confirmation and self-runtime handoff preserve the existing safe publication boundary.
- Implement a resumable one-time audit boundary for unfinished source relationships and historical archive completion evidence. Only proven premature archives are restored; ambiguous or superseded goals are not revived. Audit inputs and outcomes are pinned and unchanged files are not reread every execution. G-158 performs the deployed audit and verifies recovery; this implementation plan does not claim that production audit or G-142 recovery has already finished.
- Additive state and recovery retain old Worker/Review feedback, owned workspaces and maintenance state. Actual unsafe ownership, pending decisions and validation failures remain enforced; this change does not implement the remaining R-053/R-055 features.

## Implementation

- Replace the mandatory implementation-archive contract in archive preparation, Worker prompting, result validation and pre-publication checks with the new deferred retirement responsibility. Reconcile existing archive target records by proven generation and candidate identity rather than deleting failure or execution history.
- Keep whole-Requirement assessment output, its validation and persistence separate from catalog source/state eligibility and Git publication. Use an additive repository for durable evidence, invalidation reason and pending move/audit progress. Do not accumulate assessment policy in schema initialization or the root composition module.
- Add a per-Requirement final-assessment eligibility boundary based on confirmed Merge and related unresolved work, independently of unrelated project work. Persist deferred/completed inputs and distinguish plan-authoring results from whole-Requirement judgments. Do not treat deleted/canceled plans as MERGED or silently discard necessary goals; reuse existing relationship recovery for uncertainty.
- Extend existing general Planner input and result contracts with eligible Requirement final assessments. Manager owns deterministic input comparison, reuse, invalidation and the deferred general recheck request. Keep existing quick Planner role selection and handoff out of this retirement mechanism. Track relevant source/implementation inputs rather than treating every remote commit as a semantic invalidation. Assess actual merged remote code with existing validation receipts; do not build a pre-Merge fulfillment cache or add candidate/Merge equivalence machinery solely for Requirement retirement.
- Have Manager perform regular-file, original-byte, destination, graph/state and remote checks before staging retirement/restoration. Integrate these Manager-owned changes and their receipts with planning scope validation and publication intent so a valid deterministic move is not attributed to the Agent as an unauthorized document edit.
- Support publication with plans plus moves and with moves alone. Adopt catalog/input/state effects only after remote confirmation, and resume successful-push/unfinished-confirmation cases without duplicate commits or repeated Planner calls.
- Add explicit one-time audit inputs and completion checkpoints. Historical archive documents are evidence for the requested audit, not ordinary active planning inputs. Return uncertain cases as existing user-decision work; restore proven incomplete documents first and plan their remaining work from the restored active input.
- Reuse existing runtime handoff and blocked-input reconciliation boundaries. After deployment and audit, allow the preserved G-142 candidate to resume with current valid input and repeat the normal validation/Review gates without forced retirement.
- Update the lasting PRODUCT rule with the implementation. Prepare aligned architecture and user-document wording within the granted document permissions; do not modify AGENTS.md or use this plan to silently expand Agent authority.

## Validation

- Worker runs targeted archive calculation and execution-contract unit tests for a partial last-linked plan, stale saved archive target, preserved user decision, pending source-linked plans, absent/ambiguous proof and unrelated Requirement candidates. Assert that unrelated unsafe-state and source ambiguity checks remain enforced.
- Target judgment repository and Planner selection unit tests cover positive/negative reuse, relevant hashes, Requirement/specification changes during outstanding work, changed related implementation/plan evidence, new fulfillment, unrelated changes and interrupted/invalid result handling. Assert zero quick Planner calls throughout retirement and zero retirement-only general calls for related READY/Worker/verify/Review/PR waiting/blocks and intermediate Merges, one general full check after the final related Merge or eligible invalidation, deferral for new follow-ups, independent Requirement eligibility, unchanged-result reuse and refused cancellation/deletion ambiguity. Preserve ordinary quick impact-check routing in existing regression coverage. Use fake lifecycle ports and explicit model-call counts.
- Target publication unit tests cover mixed plan/move and move-only batches, judgment invalidation during publication, original-byte preservation, collision, remote advancement, push-success/confirmation interruption, restart and Manager scope receipts. Assert that an unmerged PR candidate cannot provide a final retirement judgment.
- Target audit unit tests cover active and historical inputs, unfinished source relationships, partial-but-all-MERGED coverage, proven restore, replaced/withdrawn goals, uncertain evidence and resume without rereading identical inputs. Test execution/PR/maintenance history preservation using fixtures, not production database mutations.
- Full verify checks planning publication, source/archive fallback, role authority, execution/Review correction, maintenance scheduling, blocked recovery, notifications and self-runtime handoff. Worker does not execute the full suite; Manager runs it and delivers any failure output for correction.
- G-158 handles operational acceptance after this implementation PR is user-Merged: observe cron use the new runtime, execute/reuse the pinned audit, retain R-053/R-055 as warranted, and resume G-142 through Worker, full verify, Review and PR publication without archive mismatch. Do not report an implementation plan as MERGED before its actual user Merge.

## Progress

- [ ] Implement durable judgments, Manager reuse/invalidation and deferred general final checks.
- [ ] Remove forced archive from implementation validation and integrate Manager publication.
- [ ] Add resumable historical audit and targeted regression coverage.
- [ ] Hand the deployed audit and G-142 recovery verification to G-158.

## Decision Log

- The user approved general Planner judgment, Manager movement at normal ExecPlan publication, no mandatory separate archive PR, cached judgments and general/quick rechecks based on change type.
- Subsequent user refinement: do not repeat whole-Requirement fulfillment checks while its generated plans remain outstanding. Check once after all related implementation plans actually MERGE; if more work is planned, defer the next check again. Specification-change and necessary code-impact checks remain available during execution. This replaces the earlier pre-Merge candidate-judgment approach and its candidate/reflection equivalence complexity.
- Final role refinement: quick Planner is excluded from Requirement completion, judgment maintenance and retirement. Manager compares recorded inputs and either reuses the judgment or schedules a general final check when eligible. The existing quick Planner for ordinary ExecPlan change-impact review remains separate; the earlier general/quick retirement recheck proposal is superseded.
- G-142 is a notification responsibility refactor. Source-linked plan Merge alone did not establish complete R-053/R-055 coverage; a 2,356-test passing candidate still failed Review when Manager forced premature archive.
- This plan is critical and does not depend on G-142: making the remedy depend on the blocked consumer would create an operational deadlock. Existing goal scopes and dependency completion requirements remain unchanged.
- Execution rollout prerequisite: the current project is in a refactoring maintenance cycle whose runnable set excludes ordinary newly published plans. Priority alone cannot select G-157 before G-142. After publication/catalog adoption, use an explicitly authorized, audited single-plan recovery dispatch with the normal ownership/dependency/safety checks and an empty execution slot; preserve the maintenance cycle, blocked candidates, sessions and unrelated approval blocks. Do not clear the cycle, fabricate Merge states, broadly relax the scheduler or assume publication has already happened. The concrete registration/dispatch must be validated against the then-current state before execution.
- Bootstrap publication prerequisite: until G-157 is user-Merged and automatic runtime handoff completes, the running Manager still enforces the old archive contract, including against this new implementation candidate. Worker code changes cannot change that running gate. Before activation, establish a supported, explicitly authorized one-time recovery path for this candidate's validation/publication that preserves active incomplete Requirements and all ownership, targeted-test, Manager verify, Review and PR checks. Do not claim that priority or READY activation alone resolves this second constraint, archive unfinished documents merely to pass the gate, patch the runtime without its ownership contract, or bypass a rejected publication. If no supported path is available, record that specific rollout blocker and obtain the necessary concrete approval before implementation execution.
- The previous G-155/G-156 local drafts concern remaining feature implementation and have not been published. This policy plan does not rely on their registration or publish them indirectly.
- Publication refinement: the already-approved one-time audit and operational recovery are a real follow-up G-158, dependent on G-157 and grounded in R-068 and R-055. Its pending source relationships keep R-068/R-055 out of the legacy archive candidate set while G-157 is implemented. R-053 is already protected by the unfinished G-142 source relationship. Recompute the complete legacy archive set after actual catalog adoption before activation; these relationships are not evidence that either older Requirement is fulfilled.

- Review corrections: final judgments can bind PRODUCT/ARCHITECTURE evidence to exact Markdown sections (including descendants); new judgments track only explicitly listed specification evidence; the versioned token invalidates older dependency assumptions once. Uncertain judgment waits expose criterion evidence, the original Requirement decision target and the document-edit re-evaluation path through existing work-exhaustion reporting. Planning retry revalidates current judgment/audit inputs and original bytes before rebinding staged move/restoration receipts to an advanced base. Focused retirement unit coverage includes unrelated/relevant section changes, missing/ambiguous sections, decision resumption and both move directions across remote advancement. No Requirement files or archive locations are changed by this Worker correction.
- Guidance document format: write ordinary Decision Log updates before the Agent Guidance Proposal/User Decision pair. Keep that pair at the end of this section; never append progress, review notes or prose after the User Decision patch fence. Preserve the approved proposal, decision, revision and deletion patch exactly.

- Follow-up review corrections: a PRODUCT-only evidence set no longer depends on unrelated ARCHITECTURE content. An invalidated pending retirement is withdrawn only after receipt, remote-path and original-byte checks; only its index paths are reset, unrelated work is preserved, and the existing Planner validation-retry path requests fresh evidence or defers while new related plans remain unfinished. Modified local content stays blocked instead of being overwritten. Targeted tests cover restart, re-assessment, subsequent movement and retry routing.

### Agent Guidance Proposal: retirement-after-merged-judgment
Target: `AGENTS.md`
Scope: `common`
Reason: G-157은 관련 ExecPlan의 실제 Merge 후 일반 Planner가 Requirement 전체 충족을 판단하고 Manager가 일반 planning 게시에서 이동하도록 변경한다. 현재 공통 규칙은 마지막 구현 PR 안에서 이동을 요구한다.
Impact: 변경하지 않으면 Worker와 Reviewer가 계속 구현 PR의 archive 이동을 필수로 해석하여 부분 구현 계획의 정상 검증과 게시를 막는다.
Patch:
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -15,7 +15,7 @@
 - Requirement 구현 결과가 앞으로도 지켜야 할 제품 규칙이면 예외적으로 코드와 같은 PR에서 `PRODUCT.md`를 갱신할 수 있다.
 - Requirement는 frontmatter나 상태가 없는 순수 문서다.
 - `docs/requirements/` 바로 아래의 모든 Requirement는 Planner 입력으로 사용한다.
-- 완료된 Requirement는 마지막 관련 ExecPlan의 구현 PR에서 Loop Manager가 eligibility를 다시 확인한 뒤 내용 변경 없이 `docs/requirements/archive/`로 자동 이동한다. archive PR이 Merge되기 전에는 활성 Planner 입력으로 남으며, 관련 비종료 ExecPlan, archive 충돌 또는 source 관계 불확실성이 있으면 이동하지 않는다.
+- 완료된 Requirement는 관련 ExecPlan이 모두 실제 MERGED이고 일반 Planner가 최신 Merge된 구현·검증 근거로 전체 Acceptance Criteria 충족을 확인한 뒤에만 Loop Manager가 일반 planning 게시에서 원문 그대로 `docs/requirements/archive/`로 이동한다. 원격 게시가 확인되기 전에는 활성 Planner 입력으로 남으며, 관련 비종료 ExecPlan·draft, 이동 충돌 또는 source 관계·충족 판단 불확실성이 있으면 이동하지 않는다.
 - `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 작업 입력으로 읽지 않는다.
 
 시스템 설계:
```

### Agent Guidance User Decision: retirement-after-merged-judgment
Decision: `replace`
Revision: `user-2026-10-05-remove-retirement-policy-from-guidance`
Patch:
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -15,7 +15,6 @@
 - Requirement 구현 결과가 앞으로도 지켜야 할 제품 규칙이면 예외적으로 코드와 같은 PR에서 `PRODUCT.md`를 갱신할 수 있다.
 - Requirement는 frontmatter나 상태가 없는 순수 문서다.
 - `docs/requirements/` 바로 아래의 모든 Requirement는 Planner 입력으로 사용한다.
-- 완료된 Requirement는 마지막 관련 ExecPlan의 구현 PR에서 Loop Manager가 eligibility를 다시 확인한 뒤 내용 변경 없이 `docs/requirements/archive/`로 자동 이동한다. archive PR이 Merge되기 전에는 활성 Planner 입력으로 남으며, 관련 비종료 ExecPlan, archive 충돌 또는 source 관계 불확실성이 있으면 이동하지 않는다.
 - `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 작업 입력으로 읽지 않는다.
 
 시스템 설계:
```
