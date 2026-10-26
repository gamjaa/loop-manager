---
id: G-324
title: 일괄 fetch 경쟁의 ref별 격리와 복구
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-092] 프로젝트 원격 브랜치 일괄 동기화.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-299
  - G-325
complexity: high
risk: high
priority: high
---

## Purpose

한 원격 branch의 미확정 입력이 검증된 다른 ref의 적용·미완료 journal 복구를 폐기하지 않게 한다. 현재 `git/base_fetch_service.py:_recorded_project_fetch()`는 요청·수신 branch 집합과 선택 base OID가 일치하면 다른 branch의 실제 수신 OID를 채택한다. 안정 base와 전진한 일반 branch의 정상 적용은 직접 구현으로 소비한다. 남은 범위는 base 관측 경쟁 등으로 제한 재시도 뒤에도 전체 적용 전에 반환하는 경계에서 확인된 독립 ref의 결과·journal 복구를 보존하고 미확정 입력만 조건 대기로 연결하는 것이다. G-299의 심볼릭 대상 보호, G-325의 무진전 대기와 G-300의 나머지 회귀·비용 비교는 각 작업에서 소비한다.

## Acceptance Criteria

- 정상 동기화의 목록 조회 1회·필요한 묶음 수신 최대 1회, 변경 없는 수신 생략과 equal ref의 pending journal 복구를 유지한다. branch별 네트워크 호출로 돌아가지 않는다.
- 실제 관측 경쟁·조회 뒤 삭제의 추가 원격 재조회·묶음 수신은 최초 시도 뒤 한 번 이내다. 각 시도의 요청·수신·commit 검증 결과를 연결하고, 안정적으로 확인된 독립 ref는 정확한 이전 값과 G-299의 직접 ref 보호 아래 적용·복구한다. 서로 다른 관측의 OID를 섞어 경쟁 ref의 성공을 만들지 않는다.
- 일반 branch가 두 시도 사이 계속 전진해도 확인된 base·다른 branch의 완료 전이는 유지한다. 정확한 branch별 수신·commit 검증으로 확정된 새 OID는 현재 구현대로 적용하고 미확정 ref만 원인·실제 관측·다음 유효 재평가 조건과 함께 보류한다. 필수 base를 확정하지 못하면 그 base를 소비하는 catalog·실행은 진행하지 않고 오래된 추적 ref나 로컬 HEAD로 대체하지 않는다. 이 보류가 별도로 확인된 독립 ref의 완료 결과·pending 복구를 지우지는 않는다.
- 인증·통신·명령 실패, 누락·중복·잘못된 수신 identity·OID와 commit 검증 실패는 경쟁·삭제나 부분 성공으로 숨기지 않는다. 유효한 수신 근거가 없는 ref는 쓰지 않으며 이미 확정된 독립 전이와 실패 감사는 보존한다.
- fetch/FETCH_HEAD 잠금과 ref별 CAS·journal의 intent/marker/결과를 유지한다. 일부 ref 적용 또는 결과 기록 사이 중단 뒤 완료한 쓰기를 반복하지 않는다. 단일 branch 경로의 필수 대상·삭제 정책·소유권 범위와 사용자 refs·태그·submodule·checkout/index/파일은 유지한다.
- 같은 미해결 입력을 다음 외부 실행마다 새 경쟁으로 재생하지 않는다. G-325의 원인·입력·진전/조건 대기를 소비하며 새 원격 입력·유효 복구 전이 뒤 해당 ref만 다시 판단한다. 보류 자체를 Agent 위반·영구 소유권 오류·최종 사용자 차단으로 바꾸지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/git/base_fetch_service.py`의 `_recorded_project_fetch()`·`_remote_branch_oids()`·`_fetched_branch_oids()`에서 아직 전체 실패로 반환되는 관측 경쟁의 시도별 결과를 ref 단위로 구분한다. 요청·수신 집합/선택 base 일치 때 실제 수신 OID를 채택하는 현재 처리와 파서의 엄격한 identity·OID 검증·긴 stdin 수신을 재사용하고, 별도로 확정 가능한 독립 결과만 `_record_fetch_ref()`로 넘긴다. 실패·손상 수신을 부분 성공으로 해석하거나 새 동기화 서비스·별도 journal을 만들지 않는다.
- [ ] 확인한 `git/manager_git_journal.py`와 `runtime/resource_lock.py`의 공유 fetch·정확한 ref 잠금 및 G-299의 raw ref 보존을 유지한다. 이전 시도의 확인된 완료와 새 시도의 미확정 입력을 구분해 legacy pending 복구도 누락하지 않는다.
- [ ] 확인한 `runtime/registration_service.py:remote_base()`의 원격 base 소비 경계와 실제 호출자를 추적해 필수 base의 유효 결과, 일반 ref의 보류, 전체 조회 실패를 구별한다. G-325의 기존 recovery 관측/대기에 대상 ref와 실제 입력·진전·재확인 조건을 연결한다. 정상 base가 있어도 미확정 다른 ref를 사용한 작업은 성공 처리하지 않는다.
- [ ] 확인한 `tests/test_project_fetch_branch_deletion.py:ProjectFetchBranchDeletionTest`의 fake Git·실제 journal을 확장한다. 직접 구현된 `test_advancing_other_task_does_not_hold_selected_base`의 조회/수신 1회·실제 수신 OID 적용을 재사용하고 같은 조합을 추가하지 않는다. `test_continuous_remote_oid_race_is_bounded_and_preserves_refs`의 필수 base 미확정·해당 ref 무쓰기 assertion은 유지하며 별도로 확정한 독립 ref의 결과 보존을 구별한다. 모든 수신 무효 사례의 무쓰기 조건을 약화하지 않는다.
- [ ] PRODUCT의 원격 입력/실패 범위와 ARCHITECTURE의 일괄 fetch·journal 절에 실제 적용한 ref별 결과·재개 계약을 통합한다. G-300의 비용 비교·설정 보존 검증을 이 구현으로 이동시키지 않는다.

## Validation

- Worker targeted unit은 `tests.test_project_fetch_branch_deletion.ProjectFetchBranchDeletionTest`의 변경한 case와 관련 `tests.test_manager_git_journal` 최소 case다. 안정 base/전진 일반 branch의 기존 회귀를 연결하고, base 관측 경쟁과 별도로 확정한 독립 ref를 함께 두어 조회/수신 상한·완료 결과/pending 보존·미확정 ref 무쓰기와 원인 보존을 실제 journal로 확인한다. 실제 수신 OID로 확정된 전진 ref를 이름이나 OID 차이만으로 불안정하게 분류하지 않는다.
- 첫 시도/재시도에서 다른 ref가 바뀌는 경우, 일반 branch 삭제, 필수 base 불확정, 명령 실패·손상/누락/중복 수신을 구분한다. 검증되지 않은 ref가 catalog 입력으로 소비되는 호출은 0회여야 한다.
- 안정 ref 적용 전·일부 결과 확정 뒤·ref 쓰기 후 기록 전 중단을 기존 journal fixture로 주입한다. 완료 쓰기 중복 0회, pending 복구 지속과 FETCH_HEAD 소유 경계를 확인하고 같은 정책 조합마다 실제 Git 저장소를 반복 생성하지 않는다.
- G-325의 동일 입력 보류와 새 ref 입력 후 재개를 연결하는 최소 unit을 두되 시간 경과·runtime 재시작만으로 전체 묶음 재실행을 허용하지 않는다. 실제 Git 설정/파일 보존과 전후 비용의 후속 근거는 G-300이 담당한다.

## Progress

- [x] 지정 delta와 현행 R-092·fetch 구현·정책 회귀를 정적으로 대조했다. 전체 수신 사전 비교가 안정 ref의 적용도 막는 경계를 확인했으며 구현·테스트·verify는 실행하지 않았다.
- [ ] ref별 확인·적용·보류와 중단 복구 연결
- [ ] 최소 경쟁/실패 회귀·제품/설계 반영

## Decision Log

- revision `7f77ebb8d40d10b23d168e422ba135e38621b6d0`의 직접 구현과 `test_advancing_other_task_does_not_hold_selected_base`를 정적으로 대조했다. 정상 수신의 일반 branch 전진 허용은 재구현하지 않고, 여전히 전체 적용 전 반환하는 경쟁의 독립 결과 보존·미확정 입력 조건 대기만 남긴다. 기존 목표·ID·경로·metadata·dependency·수행 기록은 유지하며 테스트 실행·성공이나 G-299의 보류 해제를 기록하지 않는다.

- 기존 G-299는 정확한 쓰기 대상·심볼릭 ref 경쟁을 소유하므로 그 작업을 확대하지 않고 선행으로 둔다. G-325는 실행 간 무진전 정책을, 이 작업은 일괄 수신의 ref별 유효 결과를 소유한다. G-300은 실제 Merge된 두 구현을 소비해 남은 회귀·비용 근거를 확인한다. 선행 보류를 유지하며 실제 Merge 전 실행하지 않는다.
- complexity high: 두 시도의 서로 다른 ref 관측과 이미 완료한 journal 효과·필수 base 소비를 함께 보존해야 한다. risk high: 잘못된 부분 성공은 미확정 원격 입력의 실행·게시 또는 완료 ref replay로 이어진다. 엄격한 수신 검증·대상별 잠금·CAS와 중단/실패 회귀로 방지하며 불확실한 ref는 보존한다. 무인 진행을 막는 결함으로 priority high, 최초 level 5이며 Model·Effort는 Manager 설정을 따른다.
