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

`tests/test_scope_ignored_artifacts.py`의 역할·artifact 행동별 정책 조합과 `tests/test_execution_guidance.py`의 Worker 문서 결과 판정을 Git 관측 mock으로 검사한다. R-080의 테스트 정리 기준에 따라 이미 별도 unit test가 보장하는 Reviewer 사후 validator no-op의 반복 호출도 제거한다. R-055에 따라 접근 권한은 sandbox가 강제하며, 최종 게시 결과와 현재 후보·복구 조건만 실제 판정의 대상으로 삼는다. 실제 ignore 규칙 수집·index·worktree·Python import 효과와 저장 단계 재개의 고유 assertion은 유지한다.

## Acceptance Criteria

- 두 기존 역할×artifact 조합에서 의미 있는 조건은 Git 프로세스 없이 실제 결과 validator·저장 단계 재개·blocked 재평가와 필요한 SQLite·파일 보존을 검사한다. `test_ignored_untracked_creation_change_and_deletion_are_allowed`의 Reviewer 세 변형과 Worker CLI 지침 case의 Reviewer subTest는 같은 즉시 반환을 반복하므로 제거하고, 남은 ignore 관측과 Worker 허용 assertion은 보존한다. readonly 역할의 validator no-op은 기존 역할 경계 unit test로 유지하며 쓰기 거부의 증거로 사용하지 않는다. Reviewer 정책의 실제 거부는 기존 provider sandbox 회귀에 연결한다.
- 시작·결과·Manager snapshot 원문, session·retry·독립 오류, 저장 Worker/verify 결과·token, 최초 해제와 두 번째 no-op 및 artifact 보존 assertion의 대응을 남긴다. 후보가 그대로일 때의 근거 보존과 후보 변경 시 필요한 성공 무효화를 구별하며 판정·정규화·해제 함수를 성공값으로 mock하지 않는다.
- ignore source·blob/index/config 관측의 정상·실패·불완전 입력은 최종 결과의 ignored 분류·파일 보존 계약에서 검사한다. 새로 숨긴 게시 결과나 허용 밖 문서 변경은 결과 오류로 거부하지만 공유 config/ref 변화·legacy ledger 부재·과거 snapshot 차이를 Agent 권한 위반으로 되살리지 않는다. 실제 현재 대상 불일치·미완료 operation·관측 경쟁과 독립 오류는 해당 동작의 기존 보류로 남는다.
- `WorkerCliGuidanceScopeTests.test_worker_can_change_cli_guidance_within_ordinary_worktree_scope`는 Worker의 일반 문서 결과 허용을 실제 validator로 검사한다. 현재 Reviewer 호출은 사후 validator의 거부 대상이 아니므로 옛 Reviewer 거부 assertion을 복원하지 않는다. 파일 인코딩을 추가 승인 gate로 만들지 않는다.
- 실제 Git의 tracked/external/negated ignore 수집, 강제 stage와 현재 index·worktree 효과, Python 자식 bytecode 및 provider 정책의 기존 회귀를 유지한다. 같은 관측에 대한 역할·저장 조합은 mock으로 분리하고 검증 대상이 제거된 권한 귀속 assertion과 유지하는 현재 결과·안전 assertion을 구분한다.
- 제거한 반복마다 동일 조건·assertion을 계속 보장하는 테스트를 보고한다. `test_historical_and_blocked_ignored_results_preserve_original_evidence`의 Reviewer 분기는 저장 Worker/verify 결과·token과 REVIEWING 재개를 추가 검증하므로 위 no-op 반복과 구분해 유지한다. 전용 fixture·helper는 남은 참조가 없을 때만 정리하며 실패·비용을 이유로 현재 호환성·보존·복구 계약을 삭제하거나 skip·assertion 약화로 바꾸지 않는다.
- unittest 전체 수집과 필수 compile, 기존 timeout·skip 의미를 유지한다. 비용 감소는 실제 호출 수·영향 모듈 시간으로 확인하며 전체 verify 또는 다른 OS의 절감률로 환산하지 않는다.

## Implementation

- [x] 확인한 `tests/support/scope.py`·`scoped_recovery.py`의 snapshot·실제 SQLite fixture를 재사용하고 `agents/scope_observation.py`의 관측만 double로 제공한다. `agents/scope_validation.py`·`scope_recovery.py`·복구 transaction을 실행하며 예상 밖 subprocess는 실패하게 한다.
- [x] R-080의 제거 기준을 두 소비 지점에 적용한다. `agents/scope_validation.py:validate_agent_scope()`의 Reviewer 조기 반환과 `tests/test_scope_role_boundaries.py:ScopeRoleBoundariesTest.test_writing_roles_validate_output_while_read_only_access_uses_sandbox`·`test_worker_product_documents_and_guidance_remain_allowed`가 보장하는 no-op에 위 Reviewer 반복을 대응시킨다. 역할별 저장·재개나 실제 ignore 관측의 assertion을 함께 버리지 않으며 공유 support는 참조가 남으면 보존한다.
- [x] 두 조합과 CLI 지침 case를 현재 R-055 계약으로 전환한다. readonly validator의 조기 반환과 저장 재개를 역사 snapshot 권한 검사로 대체하지 않고, 현재 후보·결과·독립 원인 보존을 assertion에 연결한다.
- [x] 실제 ignore/index/worktree·bytecode 효과와 provider 정책 검증의 대응을 `tests/test_scope_role_boundaries.py`·`test_scope_configuration.py`·`test_execution_guidance.py` 및 기존 관련 회귀에서 확인한다. 새 fixture 체계·cache·runner나 운영 권한 정책을 도입하지 않는다.

## Validation

- Worker는 정리 후 잔여 두 조합과 CLI 지침 case, 제거 근거가 되는 기존 역할 경계의 최소 targeted unit만 실행해 Git no-call·실제 결과 판정·SQLite·artifact/원문 보존을 확인한다. 삭제한 no-op 반복의 역할·입력·assertion과 남은 테스트의 대응을 보고하고 테스트 개수만으로 동등성을 판단하지 않는다. 접근 권한 거부를 확인하려고 실제 Agent를 호출하지 않는다.
- 회귀는 동일 후보의 결과 유지, 바뀐 후보의 재검증, 현재 작업/operation 불확실·독립 sandbox/Review 원인 보존, readonly 사후 검사 비활성화와 실제 provider 정책의 분리를 확인한다. 기존 실제 효과 회귀와 전체 수집·필수 compile을 유지한다.
- 관측 mock 전환의 비용은 동일한 잔여 조건·계측·병렬도에서 비교하고, 중복 제거에 따른 수집 개수·범위 변화는 별도로 보고한다. 범위가 달라진 모듈 시간을 같은 실행 범위의 속도 개선으로 해석하지 않는다. 아래 역사 표본은 변경된 코드의 새 전후 기준이나 성공이 아니며 전체 verify·다른 OS의 절감률로 환산하지 않는다.

## Progress

- [x] 2026-10-13 기준 `20e6f0fbfdb6ceb5384d42c83f3904490b441356`의 원본·기존 support·현재 catalog를 정적으로 확인했다. 역할 정책·반복 verify 실패의 기존 전환은 재사용하며 이 잔여 조합만 대상으로 한다.
- [x] 저장된 전체 verify `64fd60600657b242f833a1b1355aaab0`은 3,631 success·5 skip과 compile PASS다. 해당 후보와 현재 기준의 코드·테스트·검증 입력에는 diff가 없다. 위 historical 조합은 1,425 Git 호출, 생성/수정/삭제 조합은 300 호출을 기록했다. 이는 Python 3.14.3/Linux의 한 표본이며 새 변경의 성공이나 절감량이 아니다.
- [x] 2026-10-23 Linux/Python 3.12.3 targeted 검증: 마지막 변경 뒤 focus set 7개 PASS, 실제 ignore/config·index·bytecode·ref 및 현재 race/독립 오류 supporting set 7개·6개·5개 PASS. 전체 verify와 unittest 수집·compile은 Manager 실행 경계로 남겼다.
- [x] 동일 조건의 실행 전 관측 비용은 focus의 잔여 조건에서 full snapshot 77회·관측 Git subprocess 974회·snapshot 측정 3.492초였다(Planner/Worker artifact 12회·152회·0.612초, 역사/blocked 63회·798회·2.807초, Worker CLI 2회·24회·0.073초). 전환 뒤 세 변경 테스트의 scope 판정 구간에서 full snapshot 및 관측 Git subprocess는 0회다. 테스트 fixture 준비의 Git 실행은 이 비교에서 제외한다. 세 Reviewer artifact no-op subtest와 CLI no-op assertion 제거는 별도 범위 변경으로 기록하며 총 실행시간을 절감률로 해석하지 않는다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-080의 추가된 정리 기준을 기존 범위에 통합한다. 현재 Reviewer validator는 snapshot 비교 전에 반환하고 역할 경계 unit test가 이를 검사한다. artifact 세 변형과 CLI 지침 case의 같은 호출 반복만 제거 대상으로 확정하며, 역사/blocked 조합의 Reviewer 전용 저장·재개 assertion은 유지한다. 원격에서 직접 삭제한 catalog/checkpoint wiring·단순 정상 ExecPlan 테스트는 재구현하거나 소급 계획하지 않는다. 새 독립 목표·dependency는 없고 기존 ID·metadata·수행 기록을 보존한다.
- 이번 delta는 Git 권한 귀속 검사의 폐지를 이미 반영한 코드·테스트를 기준으로 하며, 반복 Git fixture 관측 분리를 targeted 실행으로 완료했다. 폐지된 Reviewer 거부·저장 snapshot 일치 조건은 보존 대상으로 요구하지 않는다. 기존 Progress의 원문·측정 감사는 유지하고 전체 verify 성공은 기록하지 않는다.
- complexity medium: 기존 관측·scope·저장 경계는 명확하지만 legacy 시작 규칙과 여러 저장 소비자의 입력을 함께 구성해야 한다. risk medium: 잘못된 mock은 보존·거부 회귀를 놓칠 수 있으며 실제 판정·DB와 통합 회귀의 대응 검토 및 테스트 교정으로 복구한다.
- R-080의 남은 테스트 실행 경계만 변경한다. catalog의 차단 경로 감사나 Merge된 Git 관측 전환 작업을 다시 구현하지 않고 정책 변경·검증 성공을 합성하지 않는다.
