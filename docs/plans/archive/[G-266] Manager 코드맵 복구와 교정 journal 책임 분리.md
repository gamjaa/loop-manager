---
id: G-266
title: Manager 코드맵 복구와 교정 journal 책임 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
depends_on:
  - G-222
complexity: medium
risk: medium
priority: high
---

## Purpose

Manager가 복원한 코드맵과 Agent가 편집할 계획의 책임을 교정 journal에서도 구분하여 기존 planning 충돌 복구를 끝까지 연결한다. 저장된 발견 6의 `loop_manager_internal/planning/planning_rebase.py:40`은 교정 경로를 계획과 FUTURE_IDEAS만 허용한다. 반면 `PlanningConflictCorrection.resolve`는 receipt를 확인해 복원한 `docs/maintenance/code-map.jsonl`을 저장·stage 대상에 포함한다. 저장 helper와 journal 재조회가 그 경로를 거부하며, 코드맵 단독·stash·계획 혼합 충돌 모두에 이 구조 불일치가 적용된다.

이 경로에서는 Manager 복원 다음에 `PlanningRebaseError`가 발생해 자동 실행의 유지보수 복구가 중단된다. `tests/test_planning_recovery.py`의 해당 사례는 저장 helper를 mock하여 연결 실패를 드러내지 못한다. 확인된 자동 실행 중단을 해소하는 작업이므로 priority high다. 실제 운영 사건의 빈도나 데이터 유실을 추정하지 않는다.

## Acceptance Criteria

- 코드맵 복원·journal 저장/재조회·stage·기존 rebase/stash 완료가 같은 Manager 산출물 계약을 사용한다. 단독·stash·계획 혼합 충돌에서 실제 journal helper가 정확한 복원 경로를 거부하지 않는다.
- Agent 편집 경로와 Manager의 코드맵 복원 경로는 분리한다. 코드맵은 기존 receipt·원본 bytes/hash·관리 worktree/target 검증을 통과한 Manager 복원으로만 취급하고 Agent prompt·scope에 쓰기 권한을 추가하지 않는다. 임의 maintenance 경로나 receipt 없는 산출물을 허용하지 않는다.
- 사용자 동작과 공개 계약은 기존 planning 교정·session 보존·원래 rebase target·stash 보존·검증 후 원격 게시 순서를 유지한다. 정상 복구의 불필요한 거부만 제거하며 원격 관측·Git 소유권·허용 파일·게시 lease 검사를 완화하지 않는다.
- 저장 형식은 기존 journal의 필드·phase·head/digest/nonce·correction 구조와 기존 기록의 읽기/재개 호환을 보존한다. 운영 SQLite schema/state 모델, journal 위치와 예약 코드맵 경로를 변경하지 않는다. receipt는 정적 산출물의 복원 근거이며 의미 검토 성공이나 새 Agent 권한이 아니다.
- 저장 직전·저장 뒤 stage 이전·stage 뒤 계속/확정 이전의 중단에서 같은 기록을 재조회한다. target·HEAD·digest·receipt 또는 권위 있는 파일이 달라지면 보수적으로 거부하고 journal·stash·session·사용자 작업물과 진단을 보존한다. journal 저장 성공만으로 SQLite base 채택이나 평가 성공을 기록하지 않는다.

## Implementation

대상 책임 경계는 `planning/conflict_correction.py`의 Manager 복원/Agent 편집 분리와 `planning/planning_rebase.py`의 저장·읽기·재개 검증이다. 기존 `maintenance/code_map_artifact.py`의 복원 receipt와 Git port를 재사용해, 복원한 고정 artifact를 journal이 표현할 수 있도록 같은 검증 계약으로 연결한다. 임의 경로 허용이나 새로운 복구 엔진 대신 현재 경계의 불일치를 해소한다.

`tests/test_planning_recovery.py`의 기존 conflict fixture에서 실제 save/read/resume helper를 연결하고 Git/Agent 관측만 주입한다. G-222가 같은 복구 테스트의 관측·격리를 먼저 정리하므로 선행 Merge 결과를 사용한다. 실제 rebase/index/stash 효과는 기존 `tests/test_planning_rebase.py` 회귀를 재사용한다.

## Validation

- Worker targeted unit test는 코드맵 단독·stash collision·계획 혼합의 실제 journal 저장/재조회와 finish 경로를 확인한다. 혼합 충돌의 Agent prompt에는 계획만 들어가고 코드맵 복원·stage는 Manager에 남아야 한다. 예상 밖 Git/Agent 호출은 거부한다.
- receipt 없음/손상·경로 우회·범위 밖 파일·target/HEAD/digest 변화에서는 stage/continue/확정이 실행되지 않고 원본/journal/stash가 보존되는지 확인한다. Manager 저장·재조회 함수를 성공 mock으로 대체하지 않는다.
- 저장과 stage/continue 사이의 중단을 주입해 같은 target과 기록으로 재개하고, 다른 commit의 새 충돌에 이전 교정 근거를 재사용하지 않는지 검사한다.
- Manager 전체 verify는 기존 계획/FUTURE_IDEAS 충돌·stash의 tracked/untracked 보존, rebase 중단 재개, 코드맵 receipt·원격 source 전진·planning 게시와 scope 권한 회귀를 유지해야 한다. 공유 verify adapter와 필수 수집/compile을 바꾸지 않는다.

## Progress

- [x] 2026-10-12 source revision `c401d563589399dbca83ffe4c9cfe13419ff128e`의 module manifest `c10544e0999d36ee6e8143dedecacb414a03b8f2205fbfd2bbbb0b472d1e00b6` 발견 6을 재사용하고 정확한 인용·HEAD 일치를 확인했다. 저장 helper 연결과 기존 mock 범위를 문맥으로 확인했으며 새로운 의미 검토·테스트·verify는 실행하지 않았다.
- [x] Manager 산출물/journal 계약 연결과 `PlanningConflictCorrectionUnitTests.test_manager_code_map_correction_uses_receipt_backed_journal` targeted unit 검증 (2026-10-12)
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 복원·저장·재조회·중단 재개의 여러 경계가 있지만 기존 artifact receipt와 journal 불변식이 제공되어 있다. risk medium: 잘못된 연결은 해당 planning 복구를 잘못 stage하거나 재개할 수 있다. 검증된 고정 경로·head/digest·소유권을 유지하고 journal/stash로 거부·재개하며 기존 코드를 되돌릴 수 있다.
- G-255/G-256은 실행 작업 branch의 최신 계획 채택·후보 재평가를 다루며 이 detached planning 산출물/journal 계약과 다르다. G-189의 전수 차단 감사에 앞서 이 국소 구조 불일치를 해결하며 해당 감사나 제품 정책 변경을 중복 구현하지 않는다.
- G-222만 미충족 선행으로 필요하며 G-224의 이미 Merge된 helper를 재사용한다. 이번 평가에서 복구 시나리오와 dependency를 확정했으며 선행 Merge 이후 추가 평가나 종료 전 사후 재검증은 없다.
