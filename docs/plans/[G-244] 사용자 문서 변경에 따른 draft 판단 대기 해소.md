---
id: G-244
title: 사용자 문서 변경에 따른 draft 판단 대기 해소
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
  - docs/requirements/[R-053] Timely Progress Notifications.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on:
  - G-243
complexity: high
risk: high
priority: high
---

## Purpose

사용자가 원격 Requirement 또는 ExecPlan 본문으로 기존 질문을 해소하면, Planner의 의미 판단과 Manager의 근거 검증을 거쳐 같은 ID의 draft 대기를 해제한다. 현재 구현은 `draft` 제거와 정확한 `User Decision` 필드를 함께 요구하고, 관련 문서 변경은 새 판단 지문으로 대기를 연장한다. R-061이 추가한 사용자 문서 변경에 의한 해소를 처리하지 못한다.

기존 명시적 결정·자기 승인 거부·독립 작업 진행은 유지한다. 사용자 변경을 판단 근거로 인정하는 경로와 이를 게시·저장·알림에 반영하는 경로를 함께 완성한다.

## Acceptance Criteria

- 변경 전 질문과 최신 사용자 Requirement/ExecPlan의 원격 revision·내용을 대조한다. Planner 결과는 같은 계획 ID와 질문·입력 근거, 해소한 선택/명세, 남은 질문, 목표·완료 조건에 미치는 영향을 포함한다. 사용자가 `draft`를 직접 제거하거나 `User Decision` 형식을 다시 작성하지 않아도 확인된 해소를 처리한다. 기존 명시적 결정 경로도 계속 지원한다.
- Manager는 원격 변경의 사용자 출처, 실제 문서 내용과 판단 입력의 일치, 현재 계획 identity·revision과 허용된 개정 범위를 검증한다. Agent/Manager가 작성한 변경이나 원격에 있다는 사실만으로 사용자 결정을 합성하지 않는다. 출처·해소가 불확실하거나 결과가 누락·상충·오래된 입력에 결합됐으면 대기를 유지하고 확인하지 못한 근거를 표시한다.
- Planner는 의미 판단과 허용된 수정안만 반환한다. 검증된 사용자 근거에 따른 draft 표시 해제·필요한 동일 ID 개정은 Manager가 수행하며 `User Decision`을 대신 작성·변경하지 않는다. 근거 없는 Agent의 draft 제거와 Worker 자기 승인은 계속 거부한다. 수동 소유·준비 intent, 실제 PR 등록, 실행 이력과 snapshot 변경은 기존 권한 검사로 보호하고, 다른 mode의 개정 권한을 확대하지 않는다.
- 무관한 수정, 질문에 답하지 않은 수정, 부분 답변, reject/defer와 시간 경과는 해소로 처리하지 않는다. 부분 해소와 새 모호함은 현재 근거에 맞는 남은 질문으로 보존하고, 새 draft 전환은 기존 미착수·무소유 개정 경계 안에서만 허용한다. 변경된 질문이나 scope에는 과거 결정의 효력을 그대로 적용하지 않는다.
- 원격 게시와 저장된 판단 대기 해제는 동일한 검증 근거에 결합한다. 게시 전 원격·계획·소유권이 달라지면 이전 결정을 적용하지 않고 다시 확인한다. 게시·SQLite 반영 사이 중단은 기존 intent/checkpoint로 복구하며, 원격 개정 확인 전 Worker gate를 해제하지 않는다. 유효한 Manager 전환을 Agent 자기 승인 오류로 반복하거나 프로젝트 planning 오류로 확대하지 않는다.
- 질문·선택 영향·판단 근거와 알림 링크는 같은 원격 입력을 가리킨다. 질문이 바뀌거나 해소됐을 때 과거 질문에 최신 링크만 붙여 재통지하지 않는다. 남은 질문·확인 불가 근거를 전달하고 동일 판단의 알림을 중복 생성하지 않으며, 과거 질문·실제 사용자 변경·전환·이미 전송한 알림의 감사 이력은 보존한다.
- 해소 뒤에도 dependency MERGED·실행 및 게시 gate를 모두 적용한다. 미해소 draft와 의존 작업은 대기하고 독립 작업은 계속한다. 미완료 판단 대기는 프로젝트 완료·Requirement archive에서 빠뜨리지 않으며, catalog hold·수동 소유·독립적인 실패/session/level 이력을 해소로 초기화하지 않는다.
- PRODUCT·ARCHITECTURE와 draft 판단에 관한 Planner instruction·작성 안내를 위 계약과 정합하게 갱신한다. 고정 instruction과 실제 검증 경계를 함께 연결하며 prompt 문구만으로 권한을 우회하지 않는다. Worker의 일반 문서 권한·PR 보고 등 G-171의 다른 목표는 이 작업에 포함하지 않는다.

## Implementation

`catalog/execplan_drafts.py`, 원격 catalog 입력 수집과 `storage/execplan_draft_state.py`의 기존 질문 identity·입력 hash·감사를 재사용한다. 의미 판단은 Planner에, 출처·입력·결과 형식·권한·적용 여부는 Manager에 맡긴다. hash만으로 질문 해소를 판단하지 않고 필요한 이전 질문과 실제 최신 문서 내용을 같은 원격 관측에 결합한다. 기존 명시적 결정 검증과 근거 없는 draft 해제 거부는 별도로 유지한다.

일반 planning의 허용된 개정·publication intent와 catalog transaction에 검증된 전환을 연결한다. G-243의 수동 소유 보호를 재사용하고 원격 게시 확인·대기 상태·감사·현재 알림의 적용/재개 경계를 명확히 한다. 원격 경합은 재관측으로, 의미 미해소는 개별 판단 대기로 처리한다. 삭제되거나 catalog가 보류한 기존 계획을 복원·재생성하거나 다른 작업의 소유권을 인수하지 않는다.

## Validation

- Worker는 `tests/test_execplan_drafts.py`의 최소 targeted unit case로 Requirement만의 명시적 해소, `draft: true`를 유지한 본문 답변, 명시적 `User Decision`, 부분/무관한 변경, reject/defer, 새 scope·질문과 오래된 결과를 검증한다. Planner 응답은 fixture로 주고 실제 SQLite와 catalog/판정 handler로 대기·감사·dependency 선택 결과를 확인한다.
- 사용자 출처 확인 불가, Agent 작성 문서·자기 해제, 빈/상충 결과, identity·입력 불일치, 수동 intent/membership·실제 PR의 게시 직전 출현을 검사한다. 검증 실패 때 개정과 gate 해제가 없고 독립 계획은 진행해야 한다. 기존 실행된 계획의 draft 전환 거부도 유지한다.
- 기존 planning publication/recovery와 notification fixture로 원격 게시 전 경합, 게시 성공 뒤 상태 저장 중단, 재시작 시 동일 전환 복구와 구질문 재통지 방지를 검사한다. 실제 Git은 기존 게시/checkpoint 연결 회귀를 재사용하고 정책 조합의 원격 관측은 mock으로 검사한다. 실제 모델·외부 알림 전송은 테스트에서 호출하지 않는다.
- Manager 전체 verify는 명시적 결정·자기 승인 거부·수동/PR 소유 보호·draft dependency/완료/archive·알림 중복 방지와 게시 복구 회귀를 유지한다. Reviewer는 사용자 근거가 없는 경로가 게시·실행을 허용하지 않는지, 검증된 경로가 같은 입력으로 끝까지 반영되는지와 문서 정합성을 읽기 전용으로 확인한다.

## Progress

- [x] 2026-10-09 `26993760f5c55048d8a72483ef7c116292e9fd52`의 활성 명세 전문, 비종료 catalog, draft/catalog/알림 구현과 관련 테스트를 정적으로 대조했다. 구현·테스트·전체 verify는 실행하지 않았다.
- [ ] 사용자 근거·의미 판단·검증된 게시/대기 전환·알림 및 관련 안내 구현
- [ ] targeted unit 검증과 Manager 전체 verify·읽기 전용 Review
- [ ] 사용자 Merge

## Decision Log

- R-061의 `9244ea61` 변경은 사용자 문서로 질문이 해소되면 형식 반복 없이 처리하도록 명시한다. 현재 `execplan_draft_state.observe`는 `not agent`, `not draft`, 동일 basis와 Decision/Revision/Answer를 모두 요구하고 `validate_agent_change`는 draft 제거를 일괄 거부한다. 기존 `test_only_remote_explicit_current_decision_releases_draft`와 관련 입력 변경 테스트는 종전 경계를 검증하므로 새 계약의 회귀가 필요하다.
- 현재 `observe`는 질문이 생략되면 과거 질문을 사용하면서 변경된 원격 revision으로 링크를 만들 수 있다. 저장된 질문을 그대로 전송하는 알림 경로까지 같은 판단 근거로 정합하게 처리한다. R-053의 실제 시작 알림 G-225 및 호출/사용량 작업과는 독립 목표다.
- G-171은 Worker 문서 권한·PR 보고, G-242는 실행 단계 DOCUMENT_RESULT와 PR 경합, G-243은 수동 소유 개정 보호를 다룬다. 이 계획은 새 R-061 해소 경로를 완성하며 G-243을 선행 조건으로 재사용한다. snapshot 밖의 보류 계획·사용자 판단이나 수동 G-207은 수정하지 않는다.
- complexity high: 사용자 변경과 Agent/Manager 게시 감사의 출처 결합, 의미 판단의 입력/결과 계약, 원격 게시와 pending 상태 사이 중단·경합의 일관된 재개 경계를 설계해야 한다. 사용자 정책은 R-061로 확정됐으며 Agent가 별도 승인 정책을 선택하지 않는다.
- risk high: 잘못된 해소는 사용자 결정 없는 작업 시작·원격 계획 변경으로 이어지고, 코드 rollback만으로 이미 생긴 branch·PR·알림을 복구할 수 없다. 근거·권한 검사와 게시 확인 전 gate 보존, intent/감사를 이용한 재개를 완료 조건으로 두며, 위 위조·경합·중단 시나리오를 Validation과 Review에서 확인한다.
- 보존된 2026-10-09 verify summary는 `f05cb9c6dc831530cd3b34f05ee2c4ca0994d5a3`에서 3,356 성공·5 skip을 기록한다. 해당 draft 코드·테스트는 현재 기준과 같지만 새 R-061 조건의 성공 근거는 아니다. 선택된 unittest adapter와 docs/VERIFY.md의 Python 지원 계약을 유지하며 검증 체계를 개편하지 않는다.
