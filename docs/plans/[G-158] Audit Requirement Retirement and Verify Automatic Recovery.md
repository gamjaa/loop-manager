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

- [x] Confirm G-157 runtime adoption and pin the archive inventory (50 Requirements / 668 criteria); criterion-specific acceptance of 49 results remains pending after Review rejection.
- [x] Preserve original archived sources and write active R-071/R-072/R-073 for the separately evidenced surviving R-063 goals; create no new ExecPlans.
- [x] Record recovery readiness for G-142/G-144/G-145 and hand execution validation to each plan after G-158 Merge; retained state is inventoried, dependent execution remains unverified.
- [x] Record remaining real blockers without clearing unrelated safety causes; independent blocks, sessions and maintenance state are preserved.

- [x] Record the actual Manager full verify for candidate `5830a071c83779ad3d6d84a63ced210ab4b3f28d` (2,447 success / 2 skip).
- [ ] Accept criterion-specific reassessment of the 49 rejected archive judgments and resolve the automatic Review FAIL.
- [x] Register the user-authorized manual G-158 PR #155 and connect its identity in Manager SQLite as PR_OPEN; automatic Review FAIL is preserved.
- [ ] Confirm actual user Merge; PR_OPEN does not represent automatic Review PASS.

## Decision Log

- This is the one-time audit and deployed recovery portion already requested in R-068, split from the code implementation so deployment evidence can be collected after G-157 Merge.
- R-055 is a direct recovery constraint for preserving unresolved work and not broadly clearing independent blocks; this plan does not claim to complete all of R-055.
- The pending source links are legitimate unfinished work. They prevent premature legacy archive during G-157 implementation; final Requirement fulfillment is still determined by general Planner after all related implementation plans Merge.
- The current maintenance cycle also excludes ordinary newly published follow-ups. Register and select this plan through an explicitly authorized single-plan recovery admission after its dependency is MERGED; preserve the existing maintenance cycle and normal single-project execution boundary.

- 2026-10-05 사용자 실행 순서 결정: G-158을 먼저 수행하고 G-142·G-144·G-145의 Merge 선행 dependency로 등록한다. 감사·복구 준비와 실제 후속 실행 검증을 분리해 상호 대기를 피한다. G-138의 R-070 복구 fixture·Git 관측 회귀는 G-160 별도 수정이며 이 계획으로 성공 처리하지 않는다. 사용자는 기존 유지보수 주기와 작업물·세션을 보존하는 G-158 단일 계획 복구 admission을 승인했다. 원격 개정 채택과 당시 실행 슬롯·소유권·dependency·안전 조건을 확인한 Manager 절차로만 실행하며 SQLite 사건을 임의 초기화하지 않는다.

- 2026-10-05 읽기 전용 운영 관측: SQLite에서 G-157은 `MERGED`이고 PR #149가 연결돼 있다. 실행 runtime `90466148355c1d79786522b71d961d9b9b2a1943`은 G-157 게시 SHA `ced4db4041db6f5f4b188785129b6561ecbb31cf`의 후속이며, 07:22 UTC의 G-158 Worker 호출과 catalog 관측이 이 runtime에서 기록됐다. 감사 inventory에 사용한 관측 SHA는 `90466148355c1d79786522b71d961d9b9b2a1943`이다. 해당 tree의 `docs/requirements/archive/`에는 Markdown 50개가 있고, 경로·mode·blob OID를 정렬한 `git ls-tree -r` 행의 SHA-256은 `c1bf72310c6b8971b77d59ac23803965c4d48411c30a4b602ab1d8ef494a6b0f`이다. 활성 Requirement는 14개이며 R-053/R-055/R-068은 각각 blob `14413ff21d3798517d8a8a7d03575f1afe993d40`/`6edebc4edfd45fc942e9af77dafdd3eb1be74541`/`7190d71bf847ace8770e47632ae104d450e9d6c8`로 남아 있다. 50개 archive 모두 catalog상 관련 계획이 적어도 하나 있고 모두 `MERGED`여서 이 사실만으로 완료를 판정할 수 없다. 이 목록은 Worker의 읽기 전용 예비 inventory이며 Manager의 pinned audit 결과나 Planner의 의미 판단을 대신하지 않는다.
- 같은 관측에서 운영 `requirement_audit`와 `requirement_move_receipts`는 모두 0건이다. 유지보수 주기는 `refactoring` 단계다. `PlanningSelectionService`는 이 단계에서 READY 작업을 활성화하고 일반 planning의 `audit_pending` 경로를 지나지 않는다. 따라서 배포된 runtime에서 감사가 아직 실행되지 않았고, archive 50개의 fulfilled/premature/superseded/uncertain 결과·재시작 재사용·내용 보존 복원은 확인할 수 없다. Worker는 Manager를 직접 실행하거나 운영 SQLite를 수정하지 않았다. 감사가 가능한 Manager-owned 진입 시점 또는 유지보수 중 감사 경계의 결정이 필요하며, 결과가 생기기 전에는 이 계획의 감사 Acceptance Criteria를 충족했다고 기록하지 않는다.
- G-142/G-144/G-145는 각각 `PLAN_BLOCKED`이고 `depends_on=[G-158]`, 기존 Worker·Reviewer session, branch, worktree, verify 결과, Review 실패와 `user_decision_required` block/checkpoint가 남아 있으며 PR은 없다. 세 worktree의 미게시 변경도 남아 있다. 이전 archive 강제 지적은 배포된 G-157 정책에 비춰 재평가할 대상이지만, G-145에는 별도의 Windows `fcntl` 테스트 지적과 범위 격리 전 미게시 checkpoint도 있어 일괄 해제할 수 없다. G-158이 실제 Merge된 뒤 Manager가 현재 입력·소유권·각 독립 원인을 확인하고 세 계획의 Worker 확인, targeted test, 전체 verify, Review, PR 게시를 각각 진행해야 한다. 현재 기록은 복구 준비 inventory이며 실행 완료 증거가 아니다.

- 2026-10-05 사용자 수동 감사 승인과 문서 범위 결정: G-158 branch에서 archive Requirement 전수를 현재 PRODUCT·활성 Requirement·후속 계획·구현·테스트와 대조한다. 이후 명세로 변경·대체·충돌한 과거 조건을 제외하고 미완료 잔여 목표만 활성 Requirement로 작성한다. 사용자는 새 G 문서를 작성하지 말고 G-158에 이번 작업 내용과 완료한 항목을 기록하도록 결정했다. 따라서 새 ExecPlan은 작성하지 않으며 R-071/R-072/R-073만 후속 Planner 입력으로 추가한다. 감사 증거는 `docs/audits/G-158/README.md`와 `requirement-audit.json`에 고정한다. 기존 source·목표·dependency·session·branch·작업물·차단과 유지보수 주기를 보존한다.
- 2026-10-05 수동 감사 결과: 고정 base `4be5702ee7bf133667bd3c58745659b527018237`의 archive 50개와 Acceptance Criteria 668개를 대조했다. 정적 구현·테스트 계약에 부합하는 문서 25개, 후속 정책을 적용해야 하는 문서 24개, 전체 원문 복원이 부적절한 혼합 문서 R-063 1개로 구분했다. 이 분류는 이번 Agent가 전체 테스트를 실행했다는 의미가 아니다. R-063의 남은 규칙 기반 편집·결과 형식·사실 설명 호출 축소는 R-071, 역할별 동일 입력 성공 재사용은 R-072, 호출 목적·실행 여부·usage 범위·조회는 R-073으로 보존한다. R-068이 대체한 구현 PR archive 규칙은 제외하며 R-049 strict 설정은 후속 G-131의 optional fallback에 따라 복원하지 않는다. 활성 R-033/G-066 등이 이미 보존하는 미완료 범위는 중복 추가하지 않는다. 과거 MERGED ID를 재실행하거나 archive 원문을 변경·일괄 이동하지 않았다.
- 2026-10-05 감사 저장 검증: 운영 DB의 일관된 backup에서 기존 Manager `RequirementRetirementPort`가 원문·source 관계·관련 제품·계획·구현 근거를 pin하고 50개 결과의 기준별 근거를 검증·저장했다. DB를 닫고 다시 연 동일 입력에서 pending 0개·blob 재읽기 0회를 확인했다. 생산 DB 등록과 PR 연결은 별도 Manager API 절차로 확인하며, 이 수동 절차를 cron의 일반 Planner 감사 실행으로 보고하지 않는다. 문서와 감사 증거만 바뀌어 새 executable unit test가 필요하지 않으며 targeted test·전체 verify를 직접 실행하지 않았다. Manager의 전체 verify와 독립 Review, 실제 PR·사용자 Merge는 각 실제 결과가 확인될 때만 체크한다.
- 2026-10-05 후속 복구 경계: G-142/G-144/G-145는 기존 G-158 dependency와 작업물·session·verify·Review 실패·checkpoint를 유지한다. 이전 archive 강제 지적은 R-068/G-157에 비춰 재평가할 대상이며, G-145의 Windows fcntl/cleanup 피드백과 다른 ownership·scope·사용자 결정 차단은 일괄 해제하지 않는다. G-158 실제 Merge 뒤 각 계획의 현재 입력·독립 원인·안전 gate를 확인하고 Worker·targeted test·전체 verify·Review·PR을 각각 진행해야 한다. 이번 체크는 복구 준비 기록 완료이며 자동 실행 성공 기록이 아니다.

- 2026-10-05 운영 감사 등록 확인: 전역 Manager process lock과 일관된 DB backup을 확보한 뒤 기존 audit port로 고정 입력 50개 결과를 생산 DB에 등록했다. DB 재연결 후 같은 입력에서 pending 0개·blob 재읽기 0회를 확인했다. ExecPlan·project·maintenance 행은 등록 전후 동일하며 과거 실행·실패·세션을 변경하지 않았다. 운영 receipt: `/home/ubuntu/.local/state/loop-manager/manual-audits/G-158-20261005T080802Z/registration.json`. G-158 검증·Review·PR 상태는 이 감사 저장만으로 성공 처리하지 않는다.

- 2026-10-05 Review 교정: `review:docs/audits/g-158/requirement-audit.json::results.criteria::insufficient-audit-evidence`는 R-063을 제외한 49개 문서의 652개 기준에 문서 단위 사유와 동일한 경로 목록이 반복된다고 지적했다. 예를 들어 R-001의 READY 선택과 priority parser 검증은 서로 다른 기준인데 같은 대체 근거가 기록돼 있다. 운영 DB의 50개 행과 pending 0건은 형식 검증·저장 사실일 뿐 기준별 의미 판단의 유효성을 증명하지 않는다. 이 결과를 잠정 분류로 표시하고 위의 감사·복구 완료 체크를 되돌렸다. 원본 감사 파일과 등록 영수증·실패 피드백은 보존했으며 운영 SQLite의 결과를 Worker가 수정·삭제·초기화하지 않았다. 일반 Planner가 각 기준의 현재 충족·명시적 대체·잔여 미구현을 근거별로 재평가하고, Manager가 기존 audit 경계에서 검증·교체한 뒤에만 완료 결과를 수용해야 한다.

- 2026-10-05 사용자 수동 게시 결정: 사용자는 `insufficient-audit-evidence` Review FAIL 로그를 확인한 뒤 “복잡하게 할 필요없이 직접 PR 등록하고 그걸로 PR_OPEN 상태로 바꾸면 안 돼?”라고 요청했다. 이 명시적 결정에 따라 현재 문서 후보를 수동 PR로 게시하고 실제 GitHub identity를 G-158의 PR_OPEN에 연결한다. Reviewer 실패·49건 재평가 pending·최초 후보의 Manager verify 기록과 기존 session·실패 감사를 유지한다. 자동 Review 통과, 감사 전체 완료 또는 사용자 Merge를 만들어내지 않는다. G-142/G-144/G-145의 독립 차단과 유지보수 상태도 보존한다.

- 2026-10-05 수동 PR 연결 확인: `https://github.com/gamjaa/loop-manager/pull/155`의 OPEN 상태·main base·loop-manager/G-158 head와 로컬 HEAD 일치를 확인하고, 전역 Manager 잠금·DB backup 아래 기존 RuntimeStateRepository로 G-158만 PR_OPEN에 연결했다. 자동 Review FAIL·49개 감사 재평가 pending·Worker/Reviewer session·실패 이력과 다른 계획·유지보수 상태는 보존했다. 실제 사용자 Merge는 아직 확인하지 않았으며 Manager가 등록된 같은 PR을 관측한다.
