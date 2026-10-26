---
id: G-323
title: workspace 소유권 차단의 영향 범위 분리와 자동 재평가
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
depends_on:
  - G-322
complexity: high
risk: high
priority: high
---

## Purpose

R-055 W04–W06에 따라 workspace 소유권 문제의 실제 영향 범위를 판정해 작업·의존 작업·충돌 자원만 보류하고, 기존 프로젝트 차단도 현재 근거로 해제·축소한다. G-322의 예약/현재 소유권 판정과 기존 작업별 실행·복구·알림 경계를 소비하며, 원인 작업과 그 영향으로 기다리는 작업을 구분한다.

현재 `recovery/scoped_blocking.py:reason_scope()`는 `workspace_ownership_uncertain`을 항상 project 범위로 분류한다. `execution/execution_workspace_handler.py`의 소유권 실패도 그 공통 원인으로 전달된다. `scope_reconciliation.py`는 깨끗한 예약의 원인 해소를 확인하면 프로젝트 gate를 해제하지만, 미해소 문제가 한 작업에만 남은 경우의 범위 축소는 제공하지 않는다. R-091의 기존 PLAN_BLOCKED 관리 개수 제외·독립 선택과 대상별 lease를 재구현하지 않고 이 실패의 입력·전환만 연결한다.

## Acceptance Criteria

- R-055 W04: 현재 증거로 해당 작업·dependency·실제 충돌 자원에 한정됨이 확인된 소유권 실패는 그 범위만 보류한다. 동일 프로젝트와 다른 프로젝트의 적격 독립 작업은 R-091의 기존 관리·Agent·verify 한도·수동/RETRY 우선순위·필수 gate 안에서 같은 실행에 진행한다. 종료 불명 프로세스의 실제 호출 자리와 자원 예약은 보존한다.
- 공유 Git 설정·ref 소유권·다른 worktree 쓰기 등 프로젝트 전체의 안전을 확정하지 못하면 구체적 근거와 원인을 보존하고 프로젝트 차단을 유지한다. 오류 code·예외 문자열·작업 ID 존재만으로 scope를 축소하지 않는다. 원인 작업과 영향받는 호출 대기를 구분해 기존 로그·저장 근거로 확인할 수 있다.
- R-055 W05: 기존 `workspace_ownership_uncertain` 프로젝트 차단은 원격 문서 변경이나 수동 DB 수정 없이 현재 자원·원인·G-322의 복구 근거로 재평가한다. 완전 해소면 해당 원인과 연결된 대기/확인 요청만 해소하고, 작업 범위의 미해소면 그 작업 차단을 보존한 채 프로젝트 gate만 축소한다. 별개 계획 차단·승인·Review 실패·planning 오류·다른 작업의 투영과 감사를 변경하지 않는다.
- 범위 전환은 현재 작업·자원 소유·프로젝트/작업 차단 원인·checkpoint generation을 다시 확인한 뒤 기존 짧은 transaction/CAS에서 적용한다. 중단·DB rollback·재시작에 작업 차단 없이 프로젝트 gate만 사라지는 틈·중복 실행·해소된 원인의 재차단이 없어야 한다. 경쟁 변경은 보존하고 현재 적용만 보류한다. 기존 project 원인 code와 저장 기록의 해석을 일괄 변경하지 않는다.
- R-055 W06: 예약 확정·차단 축소/해제는 Worker·verify·Review·rebase·게시 성공이나 사용자 승인·실패 초기화가 아니다. 실제 저장 단계·현재 후보와 유효한 성공 근거에 따른 필요한 단계를 유지하며, 해소된 요청을 낡은 원인으로 다시 게시하지 않는다.
- R-053: 정상적인 자동 재평가·작업 대기·차단 축소/해제·독립 작업 재개에 일반/무음 Telegram을 만들지 않는다. 해소된 원인의 미전송/잔여 조각은 현재 조치 판정으로 취소하고 전송·실패·취소 감사는 보존한다. 실제 미해결 사용자 판단·권한 조치·PR 검토와 복구 불가 중단·확인된 무진전 지속 장애는 별도 현재 원인으로 유지하며, scope 자체로 사용자 조치 필요를 합성하지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/recovery/recovery_runtime.py`의 원인/최종 scope 결정, `scoped_blocking.py`의 단일 code-scope 검증과 plan/project 저장, `recovery/block_service.py` 및 `execution/execution_workspace_handler.py`의 실패 전달을 대조한다. G-322의 판정과 현재 자원 관계로 plan/project 결과를 명시적으로 연결하고 기존 project 원인을 단순히 plan으로 재분류하지 않는다. 필요한 구분은 기존 호환 저장·감사 구조 안에서 구현하며 비호환 schema나 범용 새 상태 모델을 도입하지 않는다.
- [x] 확인한 `recovery/scope_reconciliation.py:resume_project_block_if_safe()`·`project_block_service.py`와 `storage/recovery_state.py:resolve_rebase_block()`·`execution_checkpoint_state.py`의 원인 일치·CAS·감사를 재사용한다. 잔여 작업 차단과 프로젝트 gate 축소, 해소된 확인/게시 요청 처리를 함께 확정하고 무관한 차단 row·현재 승인·실패 근거를 덮어쓰지 않는다. `project_blocked_recovery.py`의 기존 재평가와 충돌하거나 과거 요청이 원인을 재생하지 않도록 소비 경계를 연결한다.
- [x] 확인한 `application/task_scheduling.py`, `storage/task_calls.py`, `catalog/project_selection.py`와 `recovery/blocked_plan_reconciliation.py`의 기존 작업 lease·관리 개수·dependency 선택을 사용한다. 새로운 배정기·한도·polling이나 G-297의 admission/명세 drain·재시도 구조를 만들지 않는다. 원인·대기 대상·충돌 자원과 scope 판단 근거는 기존 복구 snapshot/로그에 남기고 새 조회 CLI는 추가하지 않는다.
- [x] 확인한 `notifications/policy.py`·`incidents.py`·`delivery.py`의 현재 조치·잔여 조각 취소를 이 원인의 해소/범위 전환과 연결한다. 이미 구현된 공통 무통지·outbox 엔진을 중복 구현하지 않고 실제 누락 소비자만 보완한다. PRODUCT의 계획/프로젝트 차단 해제와 ARCHITECTURE의 원인 code·프로젝트 복구 절에는 실제 동작·호환 판정을 통합한다.

## Validation

- Worker는 `tests/test_scoped_blocking.py`의 `ReservedWorkspaceRecoveryTests`·`ScopedBlockingGateTests`, `tests/test_execution_workspace_phase.py`에서 변경한 최소 판정/전환 unit을 실행한다. 실제 SQLite·checkpoint·복구 정책을 사용하고 자원/Git/프로세스 관측만 mock한다. 기존 단일 code-scope 무결성·프로젝트 gate·독립 차단 보존 assertion을 유지한다.
- 한 작업 A의 예약 근거 부족·독립성이 검증된 B·A에 의존한 C·충돌 자원을 공유한 D를 둔다. B만 적격 한도 내에서 진행하고 A/C/D의 보류 근거에서 원인과 영향 대기를 구분해야 한다. 같은 오류 code라도 공유 config/ref·다른 worktree의 안전 미확정이면 프로젝트 gate를 유지한다. 종료 불명 호출의 자리/자원 예약과 PLAN_BLOCKED 관리 개수 제외는 기존 `tests/test_task_scheduling.py` 회귀를 재사용해 실제 다음 선택까지 확인한다.
- 완전 해소와 작업 범위 미해소를 나누어 프로젝트 차단 재평가→원인별 해제/축소→다음 선택을 검사한다. 새 문서 변경 없이 재시작하고, 작업 차단 저장·프로젝트 gate 해제·checkpoint/요청 확정의 중간 DB 실패와 원인/generation/자원 경쟁을 주입한다. rollback·멱등 재개·독립 승인/Review 실패·session/retry·현재 후보 보존과 성공 token 미생성을 확인한다. 이미 다른 원인의 plan block이 있는 경우 그 근거를 잃으면 실패다.
- `tests/test_notification_incidents.py`·`tests/test_notification_delivery.py`의 실제 outbox/부분 전송 fixture에서 해소된 원인만 취소되는 최소 연결 회귀를 둔다. 작업 범위 대기·재개·같은 입력 재관측은 전송 0건이고, 별개 미해결 사용자 행동과 과거 전송/실패 감사는 남아야 한다. 같은 공통 알림 정책 조합을 반복하지 않는다.
- 이 변경의 고유 회귀는 scope 판단·저장·선택·통지 연결이다. 실제 worktree 생성·파일 보존은 G-322의 기존 Git 회귀를 소비하고 새 Git lifecycle을 복제하지 않는다. 선택 adapter·수집/fixture·Agent guard·필수 compile 및 부분/전체 결과 구분을 유지한다.

## Progress

- [x] `11261321c5e88f581430e3bbe3889a972a8b8cc0`의 문서 변경과 이를 포함한 기준 `9ba8b64c`의 Git 이력 재구성·무진전 억제 구현을 통합했다. 과거 Agent snapshot의 존재·공유 상태 일치 조건을 제거하고 현재 관측 쌍으로 범위를 판정한다. 확인 구간의 공유 자원 안정성·writer 종료·예약 lease·CAS·rollback은 유지한다. 과거 공유 변경·snapshot 부재의 범위 축소와 실제 관측 경쟁의 차단 유지를 확인했으며 WSL `/tmp` 전용 복사본의 관련 회귀 300개와 Windows 최소 회귀 16개가 통과했다. 전용 복사본·임시 폴더는 정리했고 이번 점검에서 전체 verify는 실행하지 않았다.
- [x] revision `26140c614cf5881242ce8e3abbcdd060d6cd68c6`의 R-055 W04–W06·R-091·R-053과 고정 project scope·깨끗한 예약 해제·기존 작업별 선택/복구/통지 경계를 정적으로 대조했다. 구현·테스트·verify를 실행하지 않았다.
- [x] 복구점 `9f5287f1c91cc613b54243e7f6b86317ea222c00`에서 공유 metadata·branch/ref/등록의 독립성 판정과 고정 plan 원인 `workspace_ownership_local`을 보존했다. 기존 `workspace_ownership_uncertain`의 project 의미는 유지했다. 해당 단계의 판정 4개·단일 code-scope/저장 1개 unit이 통과했다.
- [x] 실행 실패와 기존 프로젝트 차단의 재평가에 판정을 연결했다. 예약 lease·실제 writer 종료/미시작을 먼저 확인하고 외부 관측 뒤 작업·프로젝트/계획 원인·복구 기록·게시 요청·checkpoint generation·자원 claim을 짧은 transaction에서 다시 확인한다. 잔여 plan block/추가 hold와 감사 저장 후 project gate를 해제하며 독립 승인·질문·Review block row와 recovery snapshot은 보존한다.
- [x] 같은 입력의 계획 재평가는 일반 질문/입력 처리보다 추가 hold를 먼저 해소하고 G-322 복구기를 재사용한다. 단독 원인 해소는 저장 단계로 복원하고 기존 recovery admission·관리 한도를 거친다. READY 선택과 활성화도 같은 충돌 자원 검사를 사용한다. 미착수 READY claim은 호출/미종료 invocation/게시 소유가 없을 때만 영향 대기로 구분하고, 종료 불명 호출 자리와 실제 소유 예약은 보존한다.
- [x] 소유권 차단만으로 기록된 후보·verify/Review·session·실패 근거를 지우지 않으며, 해소된 원인의 게시 요청과 Telegram 잔여 조각만 종료한다. 원래 요청과 scope 전환 근거·충돌 대기 claim은 checkpoint 감사에 남긴다. PRODUCT의 차단 해제와 ARCHITECTURE의 프로젝트 복구 계약을 구현과 함께 갱신했다.
- [x] 프로젝트 Python `python3`와 `LOOP_MANAGER_TARGETED_TEST_RUNNER`로 workspace 실행 class 10개, 소유권 연결 신규 unit 9개, 알림 연결 unit 1개와 기존 scope/선택/예약/관리 한도의 관련 개별 회귀를 실행했다. 기존 예약 복구 class 20개는 최초 요청 부재 처리 오류 수정 후 같은 명령으로 재실행해 통과했다. 독립 질문 보존의 초기화 순서 오류도 동일 개별 명령 재실행으로 통과했으며 미해결 targeted 실패는 없다. gate 삭제 직후 실패의 rollback·재개, checkpoint/공유 상태 경쟁, 관측 중 원인 교체, A/C/D 보류와 B 선택·D 활성화 거부, 저장 단계 재개·admission 및 후보/감사 보존을 확인했다.
- [x] `scripts/verify.json`의 unittest/launcher·독립 수집·compile 계약과 기존 `scripts/verify-inputs.json`의 코드/테스트 glob을 유지했다. 새 검증 파일·의존성은 추가하지 않았다. 전체 verify·프로젝트 전체 lint/build/type check와 Windows native 검증은 Worker에서 실행하지 않았으며 Manager의 전체 검증·Review가 남아 있다. 목록 밖 입력의 미소비를 확정하지 않아 VERIFY_INPUT_EXCLUSIONS는 선언하지 않는다.


- [x] Manager 전체 verify `verify:f0ca3e959372885829a18aab107c0bb47a7150862c8eb72bfc177a3fe3456d11` occurrence=1/revision=1의 실패를 교정했다. 일반 프로젝트 복구에서 소유권 전용 게시 요청 조회를 분리해 최소 planning DB 호환을 유지하고, 활성화는 READY 상태 오류를 자원 대기보다 먼저 검사한다. 원인 매핑 회귀는 기존 code의 기본 round-trip assertion을 유지하면서 새 local code에 기본 project 판정·명시적 plan 선택을 함께 검사한다.
- [x] 해당 delta의 `tests.test_origin_isolation.OriginIsolationScopeTest.test_saved_scope_block_reopens_only_for_exact_runtime_delta`(mixed 두 조건), `tests.test_recovery_runtime.RecoveryPolicyTests.test_every_legacy_block_reason_keeps_its_cause_and_scope`, `tests.test_runtime_state.RuntimeStateRepositoryTest.test_expected_status_mismatch_rejects_second_activation`을 각각 targeted runner로 실행해 통과했다. `ReservedWorkspaceRecoveryTests.test_shrunk_gate_selects_only_independent_ready_and_keeps_resource_owner`와 `test_local_residual_narrows_gate_and_restarts_from_saved_phase`도 통과했다. Manager 전체 verify의 실패 기록은 보존하며 전체 verify 성공으로 대체하지 않는다. 전체 verify 재실행·Review는 Manager에 인계한다.

## Decision Log

- G-322가 예약의 생성/변경 근거와 writer/자원 소유권을 먼저 완성한다. 이 작업은 그 결과를 차단·후속 소비에 연결하므로 G-322의 실제 Merge를 선행으로 요구한다. G-297의 배정기 보완과 G-189의 전수 감사는 별도 목표이며 기존 PLAN_BLOCKED 독립 진행을 이 작업으로 소급 계획하지 않는다.
- complexity high: 원인별 영향 범위와 기존 단일 code-scope 계약을 보존하면서 project→plan 전환·요청·후속 선택을 중단/경쟁에도 일치시켜야 한다. risk high: 잘못 축소한 gate는 실제 공유 안전 문제를 우회하고 잘못 옮긴 원인은 독립 승인/실패를 잃게 한다. 현재 근거·원래 cause·자원 예약을 보존하고 원자 확정·경쟁 거부·공유 문제 유지 및 재시작 회귀로 방지한다. priority high는 안전한 독립 자동 진행을 막는 과도한 차단의 교정이다. 최초 level 5이며 Model·Effort는 Manager 설정을 따른다.
