---
id: G-269
title: ignored scope 정책 조합의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

`tests/test_scope_ignored_artifacts.py`의 역할·artifact 행동별 정책 조합과 `tests/test_execution_guidance.py`의 CLI 지침 파일 허용 판정을 Git 관측 mock으로 검사한다. 실제 ignore 규칙 수집·index·worktree·Python import 효과의 통합 회귀는 유지하고, 동일 관측에 대한 역할·저장 상태 조합을 위해 저장소와 Git 프로세스를 반복 준비하는 비용만 제거한다.

## Acceptance Criteria

- `test_ignored_untracked_creation_change_and_deletion_are_allowed`의 planner/worker/reviewer × 생성/수정/삭제와 `test_historical_and_blocked_ignored_results_preserve_original_evidence`의 같은 조합은 Git 프로세스 없이 실제 scope 판정·저장 scope 재검사·blocked 재평가를 검사한다. 실제 SQLite와 필요한 artifact 파일은 유지한다.
- 시작·결과·Manager snapshot 원문, session·retry, 독립 오류와 reviewer의 저장 Worker/verify 결과·token 보존, 최초 해제와 두 번째 no-op 및 artifact 보존 assertion을 전환 전 조건에 대응시킨다. 판정·정규화·해제 함수를 성공값으로 mock하지 않는다.
- ignore source·blob/index/config 관측의 정상·실패·불완전 결과와 연속 관측 변경을 명시적으로 제공한다. 시작 규칙 증명 부재, 새로 숨긴 경로, 권위 변경 혼합과 현재 관측 경쟁은 실제 판정에서 거부되며 예상 밖 Git 명령은 테스트 실패다.
- `WorkerCliGuidanceScopeTests.test_worker_can_change_cli_guidance_within_ordinary_worktree_scope`의 Worker 허용·Reviewer 거부는 snapshot 입력으로 확인한다. 파일 인코딩을 CLI 지침 승인 gate로 바꾸지 않으며 기존 역할·경로 회귀와 불필요한 실제 Git 실행을 중복하지 않는다.
- 실제 Git이 필요한 tracked/external/negated ignore 규칙, 강제 stage·tracked 파일, worktree 소유권과 Python 자식의 bytecode 효과를 확인하는 기존 회귀를 유지한다. 역할에 따라 실제 수집 동작이 달라지지 않는 부분은 공통 수집 근거와 정책 조합으로 분리하고 그 대응을 남긴다.
- unittest 전체 수집과 필수 compile, 기존 timeout·skip 의미를 유지한다. 비용 감소는 실제 호출 수·영향 모듈 시간으로 확인하며 전체 verify 또는 다른 OS의 절감률로 환산하지 않는다.

## Implementation

`tests/support/scope.py`의 snapshot 생성과 `tests/support/scoped_recovery.py`의 실제 SQLite·상태 판정 패턴을 먼저 재사용한다. mock은 `agents/scope_observation.py`의 Git/ignore 관측 입력과 현재 snapshot 경계에 적용하고 `scope_validation`·persisted/blocked 소비자·복구 transaction은 실행한다. 필요한 작은 준비 차이만 기존 support에 보강하며 별도 전역 cache나 범용 fixture 체계를 만들지 않는다.

같은 파일 안의 실제 수집 회귀와 정책 회귀를 구분하고 전환한 assertion의 대응을 현재 ExecPlan에 기록한다. 운영 scope·Git·소유권 정책은 변경하지 않는다. 테스트 이름이나 위치를 바꿀 필요가 있으면 기존 검증 참조와 계속 수집되는 대응 테스트를 함께 남긴다.

## Validation

- Worker는 전환한 정책 unit case와 새 실패/관측 변화 case의 최소 범위만 실행한다. subprocess 시작을 거부하는 double로 Git no-call과 실제 판정·SQLite 결과를 확인한다.
- Manager 전체 verify는 기존 실제 ignore/index/worktree·bytecode·격리·중단 재개 회귀와 전환한 모든 조합을 포함해야 한다. Reviewer는 원래 assertion의 대응, 성공값 mock 부재와 실제 효과 회귀의 보존을 확인한다.
- 저장된 전체 기준 검증을 바탕으로 Manager가 같은 계측·병렬도·선택 범위에서 영향 모듈의 전후 호출 수와 시간을 비교한다. Worker에게 전체 suite나 통합 검증 실행을 요구하지 않는다.

## Progress

- [x] 2026-10-13 기준 `20e6f0fbfdb6ceb5384d42c83f3904490b441356`의 원본·기존 support·현재 catalog를 정적으로 확인했다. 역할 정책·반복 verify 실패의 기존 전환은 재사용하며 이 잔여 조합만 대상으로 한다.
- [x] 저장된 전체 verify `64fd60600657b242f833a1b1355aaab0`은 3,631 success·5 skip과 compile PASS다. 해당 후보와 현재 기준의 코드·테스트·검증 입력에는 diff가 없다. 위 historical 조합은 1,425 Git 호출, 생성/수정/삭제 조합은 300 호출을 기록했다. 이는 Python 3.14.3/Linux의 한 표본이며 새 변경의 성공이나 절감량이 아니다.
- [ ] 관측 분리·assertion 대응과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 관측·scope·저장 경계는 명확하지만 legacy 시작 규칙과 여러 저장 소비자의 입력을 함께 구성해야 한다. risk medium: 잘못된 mock은 보존·거부 회귀를 놓칠 수 있으며 실제 판정·DB와 통합 회귀의 대응 검토 및 테스트 교정으로 복구한다.
- R-080의 남은 테스트 실행 경계만 변경한다. catalog의 차단 경로 감사나 Merge된 Git 관측 전환 작업을 다시 구현하지 않고 정책 변경·검증 성공을 합성하지 않는다.
