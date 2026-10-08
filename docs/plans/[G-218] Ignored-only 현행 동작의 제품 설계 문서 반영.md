---
id: G-218
title: Ignored-only 현행 동작의 제품 설계 문서 반영
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-022] Ignored-Only Managed Worktree Tolerance.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

이미 Merge된 ignored-only 허용·저장 차단 재개·종료 worktree 정리의 장기 규칙을 PRODUCT에 반영하고, ARCHITECTURE의 checkpoint 설명을 실제 검사와 일치시킨다. ignored 파일 자체를 거부하는 설명 때문에 구현된 정상 동작을 미지원으로 해석하지 않게 한다.

## Acceptance Criteria

- PRODUCT에서 시작 ignore 규칙으로 증명된 index-free ignored 파일의 생성·수정·삭제는 Planner·Worker·Reviewer·Manager verify의 범위 위반이나 planning 오류의 단독 원인이 아님을 확인할 수 있다. 역할별 게시 가능 경로와 tracked·staged·non-ignored 변경, index flag·사용 Git 자원의 보호는 유지한다.
- 같은 ExecPlan worktree의 Worker targeted test → Manager verify → Reviewer 순서, 확장자 allowlist·복원 의무·별도 검증 worktree 부재를 현재 동작으로 설명한다. Planner 전용 bytecode 강제 설정을 요구하지 않는다.
- 활성 ignored 파일은 보존하고 tracked 쓰기와 같거나 부모·자식으로 겹치면 해당 Git 쓰기를 시작하지 않는 경계를 설명한다. ARCHITECTURE의 `### 차단 작업의 원격 반영과 보존`에서 ignored 파일의 존재를 일괄 거부하는 표현을 실제 충돌·소유권 검사로 정합하게 바꾼다.
- 저장된 ignored-only 오탐은 시작 규칙·역사 결과·현재 권위 상태와 소유권을 확인한 경우에만 같은 session·결과·단계에서 재개함을 설명한다. 실제 위반·증거 부재·다른 차단과 새로 숨긴 경로·강제 stage는 자동 수용하지 않는다.
- 정상 종료한 정확한 Manager 소유 planning/ExecPlan worktree만 ignored-only 상태에서도 전체 수명을 종료한다. 기본 checkout·불명확한 소유권·미게시 commit·권위 변경은 보존하며 ignored-only 정리가 active artifact 삭제 권한으로 확대되지 않는다.
- PRODUCT·ARCHITECTURE·활성 R-022·코드 사이에 위 경계의 반대 설명을 남기지 않는다. R-023·G-199의 격리 증거 없는 legacy 기본 checkout 판단은 변경하지 않는다.

## Implementation

- `agents/scope_observation.py`·`scope_baseline.py`·`scope_recovery.py`, `planning/planning_service.py`, `git/cleanup_service.py`와 관련 회귀를 재사용해 현행 동작의 조건·실패·재개를 확인한다. 새로운 범위 검사나 복구 기능을 구현하지 않는다.
- PRODUCT의 범위 검사·실패/재개·Merge 후 정리 설명을 하나의 관련 규칙으로 통합하고 ARCHITECTURE의 기존 ignored 비교 view와 checkpoint 절을 연결한다. 기술적인 snapshot·token 절차는 ARCHITECTURE에 유지한다.
- R-022 원문·Acceptance Criteria, 사용자 결정·실행 상태와 원본 snapshot은 수정하지 않는다. 아직 미완료인 R-059 runtime 캐시 목표나 G-189 전체 차단 감사까지 완료로 표현하지 않는다.

## Validation

- 문서만 변경하므로 새 unit test를 만들거나 Worker가 테스트를 실행하지 않는다. `tests/test_scope_ignored_artifacts.py`, `test_planning_error_recovery.py`, `test_scope_block_revalidation.py`, `test_worker_validation.py`, `test_worktree_cleanup.py`, `test_cleanup_rebase_artifacts.py`, `test_pr_conflict_scope.py`의 실제 조건과 문구를 대조하고 unit 미실행 사유를 보고한다.
- Manager 전체 verify는 역할별 허용·강제 stage/ignore 변경 거부·동일 worktree 순서·역사 증거 보존·checkpoint lease·종료 정리 회귀를 유지한다. Reviewer는 새 문구가 사용자 데이터 삭제·범위 완화·미검증 성공을 허용하지 않는지 확인한다.

## Progress

- [x] 2026-10-08 기준 a3ce7f88cef4cd46b34e0ead46ec5472fb5bd6f7의 명세 전문·구현·관련 테스트와 catalog를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 관련 제품·설계 절 정합화
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- PRODUCT 전문에는 managed worktree의 ignored-only 장기 규칙이 없고, ARCHITECTURE checkpoint 2번은 ignored 파일 부재를 요구한다. 같은 문서의 `### 실행별 Agent 범위와 Manager 전이`와 실제 ignored 비교·정리 회귀는 index-free 비권위 차이를 허용한다. 새 정책 선택이 아닌 이미 Merge된 동작의 문서 누락이다.
- 기존 G-189는 R-055의 전체 차단 경로 감사이며 이 문서 반영을 Acceptance Criteria로 요구하지 않는다. G-198의 구현을 다시 계획하거나 별도 감사 완료를 합성하지 않는다.
- 읽은 기존 verify 기록 `logs/verify/8eee5019c19875f58e68a1c909fc9b1e/summary.json`은 f6e648a964b0a4417a3037b4c7af04dbb7eb1e6f 후보의 Linux 성공 기록이다. 관련 구현·테스트는 그 후보와 현재 기준 사이에 동일하나 현재 HEAD에서 새 verify를 수행한 근거로 사용하지 않는다.
- complexity low: 확정된 코드·테스트 조건을 기존 문서에 반영하는 국소 변경이다. risk low: 실행·저장·외부 상태 변경 없이 설명만 바꾸며 문서 diff로 복구할 수 있다.
