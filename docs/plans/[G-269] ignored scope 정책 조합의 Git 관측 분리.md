---
id: G-269
title: ignored scope 정책 조합의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

`tests/test_scope_ignored_artifacts.py`의 역할·artifact 행동별 정책 조합과 `tests/test_execution_guidance.py`의 Worker 문서 결과 판정을 Git 관측 mock으로 검사한다. R-055에 따라 접근 권한은 sandbox가 강제하며, 최종 게시 결과와 현재 후보·복구 조건만 실제 판정의 대상으로 삼는다. 실제 ignore 규칙 수집·index·worktree·Python import 효과의 기존 통합 회귀를 유지하고 반복 fixture 준비 비용만 줄인다.

## Acceptance Criteria

- 두 기존 역할×artifact 조합은 Git 프로세스 없이 실제 결과 validator·저장 단계 재개·blocked 재평가와 필요한 SQLite·파일 보존을 검사한다. readonly 역할의 validator no-op을 쓰기 거부의 증거로 사용하지 않으며, Reviewer 정책의 실제 거부는 기존 provider sandbox 회귀에 연결한다.
- 시작·결과·Manager snapshot 원문, session·retry·독립 오류, 저장 Worker/verify 결과·token, 최초 해제와 두 번째 no-op 및 artifact 보존 assertion의 대응을 남긴다. 후보가 그대로일 때의 근거 보존과 후보 변경 시 필요한 성공 무효화를 구별하며 판정·정규화·해제 함수를 성공값으로 mock하지 않는다.
- ignore source·blob/index/config 관측의 정상·실패·불완전 입력은 최종 결과의 ignored 분류·파일 보존 계약에서 검사한다. 새로 숨긴 게시 결과나 허용 밖 문서 변경은 결과 오류로 거부하지만 공유 config/ref 변화·legacy ledger 부재·과거 snapshot 차이를 Agent 권한 위반으로 되살리지 않는다. 실제 현재 대상 불일치·미완료 operation·관측 경쟁과 독립 오류는 해당 동작의 기존 보류로 남는다.
- `WorkerCliGuidanceScopeTests.test_worker_can_change_cli_guidance_within_ordinary_worktree_scope`는 Worker의 일반 문서 결과 허용을 실제 validator로 검사한다. 현재 Reviewer 호출은 사후 validator의 거부 대상이 아니므로 옛 Reviewer 거부 assertion을 복원하지 않는다. 파일 인코딩을 추가 승인 gate로 만들지 않는다.
- 실제 Git의 tracked/external/negated ignore 수집, 강제 stage와 현재 index·worktree 효과, Python 자식 bytecode 및 provider 정책의 기존 회귀를 유지한다. 같은 관측에 대한 역할·저장 조합은 mock으로 분리하고 검증 대상이 제거된 권한 귀속 assertion과 유지하는 현재 결과·안전 assertion을 구분한다.
- unittest 전체 수집과 필수 compile, 기존 timeout·skip 의미를 유지한다. 비용 감소는 실제 호출 수·영향 모듈 시간으로 확인하며 전체 verify 또는 다른 OS의 절감률로 환산하지 않는다.

## Implementation

- [ ] 확인한 `tests/support/scope.py`·`scoped_recovery.py`의 snapshot·실제 SQLite fixture를 재사용하고 `agents/scope_observation.py`의 관측만 double로 제공한다. `agents/scope_validation.py`·`scope_recovery.py`·복구 transaction을 실행하며 예상 밖 subprocess는 실패하게 한다.
- [ ] 두 조합과 CLI 지침 case를 현재 R-055 계약으로 전환한다. readonly validator의 조기 반환과 저장 재개를 역사 snapshot 권한 검사로 대체하지 않고, 현재 후보·결과·독립 원인 보존을 assertion에 연결한다.
- [ ] 실제 ignore/index/worktree·bytecode 효과와 provider 정책 검증의 대응을 `tests/test_scope_role_boundaries.py`·`test_scope_configuration.py`·`test_execution_guidance.py` 및 기존 관련 회귀에서 확인한다. 새 fixture 체계·cache·runner나 운영 권한 정책을 도입하지 않는다.

## Validation

- Worker는 위 두 조합과 CLI 지침 case의 최소 targeted unit만 실행해 Git no-call·실제 결과 판정·SQLite·artifact/원문 보존을 확인한다. 접근 권한 거부를 확인하려고 실제 Agent를 호출하지 않는다.
- 회귀는 동일 후보의 결과 유지, 바뀐 후보의 재검증, 현재 작업/operation 불확실·독립 sandbox/Review 원인 보존, readonly 사후 검사 비활성화와 실제 provider 정책의 분리를 확인한다. 기존 실제 효과 회귀와 전체 수집·필수 compile을 유지한다.
- 비용 비교는 이번 직접 구현 뒤의 같은 코드·수집·계측·병렬도에서 영향 모듈을 비교한다. 아래 역사 표본은 변경된 코드의 새 전후 기준이나 성공이 아니며 전체 verify·다른 OS의 절감률로 환산하지 않는다.

## Progress

- [x] 2026-10-13 기준 `20e6f0fbfdb6ceb5384d42c83f3904490b441356`의 원본·기존 support·현재 catalog를 정적으로 확인했다. 역할 정책·반복 verify 실패의 기존 전환은 재사용하며 이 잔여 조합만 대상으로 한다.
- [x] 저장된 전체 verify `64fd60600657b242f833a1b1355aaab0`은 3,631 success·5 skip과 compile PASS다. 해당 후보와 현재 기준의 코드·테스트·검증 입력에는 diff가 없다. 위 historical 조합은 1,425 Git 호출, 생성/수정/삭제 조합은 300 호출을 기록했다. 이는 Python 3.14.3/Linux의 한 표본이며 새 변경의 성공이나 절감량이 아니다.
- [ ] 관측 분리·assertion 대응과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 이번 delta는 Git 권한 귀속 검사의 폐지를 이미 반영한 코드·테스트를 기준으로 한다. 남은 일은 반복 Git fixture의 관측 분리이며 폐지된 Reviewer 거부·저장 snapshot 일치 조건을 보존 대상으로 요구하지 않는다. 기존 Progress의 원문·측정 감사는 유지하고 새 검증 성공은 기록하지 않는다.
- complexity medium: 기존 관측·scope·저장 경계는 명확하지만 legacy 시작 규칙과 여러 저장 소비자의 입력을 함께 구성해야 한다. risk medium: 잘못된 mock은 보존·거부 회귀를 놓칠 수 있으며 실제 판정·DB와 통합 회귀의 대응 검토 및 테스트 교정으로 복구한다.
- R-080의 남은 테스트 실행 경계만 변경한다. catalog의 차단 경로 감사나 Merge된 Git 관측 전환 작업을 다시 구현하지 않고 정책 변경·검증 성공을 합성하지 않는다.
