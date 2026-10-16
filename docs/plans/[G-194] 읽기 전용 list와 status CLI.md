---
id: G-194
title: 읽기 전용 list와 status CLI
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
  - G-231
  - G-235
  - G-236
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: high
priority: normal
---

## Purpose

실행·복구를 시작하지 않고 기존 SQLite의 프로젝트·작업 목록과 상세 상태를 `list`·`status`로 조회한다. 2026-10-16에 직접 구현된 `status <repo>`의 관리 한도·읽기 전용 snapshot·공통 JSON 오류를 재사용해 후속 상세 근거·usage·logs와 공유할 조회 경계를 완성한다. 남은 list·작업별 status·선행 조건·Requirement 연결 조회를 담당하며 저장 근거와 조회의 무변경성이 완료 조건이다.

## Acceptance Criteria

- R-076의 `list [repository] [--source <requirement-path>] [--order priority|id] [--all] [--json]`, `status <repository> [<id>] [--json]`와 기존 전역 옵션을 지원한다. list는 프로젝트 또는 비종료 작업 요약, status는 지정 프로젝트 또는 작업의 상세이며 명시한 종료 작업도 조회한다. 작업 필터·정렬은 repository를 요구한다. 별도 explain과 config list 호환 별칭은 제공하지 않는다.
- list는 경로·phase·pause·활성 작업 수 또는 G ID·제목·status·유효 priority·선행 요약·짧은 대기 사유를 한 줄씩 보여준다. MERGED 포함은 --all, 기본 정렬은 유효 priority와 숫자 ID, --order id는 숫자 ID다. 삭제·취소·비활성 이력을 활성 수에 포함하지 않고 복수 사유의 상세는 status로 연결한다.
- 선행 없음·충족·미충족·미확인을 같은 읽기 snapshot으로 판단해 확인 수/전체 수·미확인 수 및 선행 G별 ID·제목·상태·근거·사유를 제공한다. PR_OPEN·근거 없는 MERGED·잘못된 graph를 충족으로 바꾸지 않는다. 실제 manual start 허가는 현재 원격·소유권을 다시 확인해야 한다.
- --source는 허용된 Requirement 경로의 직접 sources 관계만 선택한다. G-231의 정확한 경로 우선·archive 호환·고유 ID fallback을 재사용하되 제목 유사성·dependency·다른 프로젝트로 관계를 추정하지 않는다. 공백·구분자와 active/archive 대응, 미확인·0건·잘못된 경로를 기본 출력과 JSON에 동일하게 제공하고 문서·catalog를 새로 관측·갱신하지 않는다.
- 프로젝트 phase와 계획 status를 구분하고 저장된 구조화 사유·복구 기록으로 사용량 제한, 일시 재시도, 사용자 판단, dependency, PR 대기와 프로젝트 차단·planning 오류를 설명한다. 여러 사유와 독립 계획을 보존하며 미충족 dependency ID·상태, 저장된 PR URL·관측값, 확인된 사용자 대상과 다음 조건을 표시한다. legacy 오류 문자열로 사유를 새로 확정하지 않는다.
- R-091: 직접 구현된 DB 저장 한도·config 기준 유효값·출처·관리 수와 실제 실행/대기의 차이를 보존한다. 프로젝트 대표 phase나 작업 하나를 전체 병렬 작업의 단계로 표시하지 않는다. 실행 중 Manager의 config·실제 호출 수·반영 여부를 증명하지 못하면 기존 `actual_calls=null`·경고 의미를 유지한다. `config set`의 양수/0 저장은 재구현하지 않는다.
- 실제 진전을 증명하는 저장 사건만 마지막 진전의 시각·출처로 사용한다. invocation 시작·종료, DB 갱신·오류 관측만 있으면 진전은 미확인이다. 프로세스 존재·활동·재개 가능성·관측 신선도를 따로 표현하고 잠금이나 미종료 호출로 활동·생존을 확정하지 않는다. 조회 시각과 원본 관측 시각·출처·경과시간을 구분하며 없는 시각과 다음 실행·제한 해제 시각을 추정하지 않는다.
- 기존 저장 결정과 순수한 선택·복구·재시도 판단을 재사용한다. 기존 유효기간이 없는 관측에 새 기준을 만들지 않고 원격 확인이나 상태 변경이 필요한 판단은 재검사 필요·미확인으로 남긴다. 다음 외부 실행의 재검사는 재개 성공을 보장하지 않는다.
- 조회 진입은 실행 잠금, bootstrap·runtime 자식, 설정/provider 초기화, schema 초기화·migration, 중단 호출 정리와 실행 경로를 거치지 않는다. 에이전트·verify·Git·원격 API·게시·알림·복구를 호출하거나 작업·retry·관측·완료 기록을 바꾸지 않는다. DB가 없으면 directory·DB를 만들지 않는다.
- 읽기 전용 SQLite와 유한한 busy 대기, 하나의 일관된 읽기 snapshot을 사용한다. 필수 table·column을 읽기만으로 검사하며 선택 정보 누락은 미확인이다. WAL 등 제약으로 읽을 수 없으면 쓰기 연결·migration·journal mode 변경·checkpoint·자동 복원 없이 오류를 반환한다. 실행 중인 다른 Manager의 실행 잠금을 변경하지 않는다.
- 기본 출력과 JSON은 같은 사실·미확인 의미를 제공한다. JSON은 schema_version·command·queried_at·repository·plan_id·source·data·warnings·error 및 `null`·`[]`·실측 `0`을 보존하며 stdout에는 문서 하나만 출력한다. source는 list 필터에서만 값이 있고 나머지는 null이다. 조회 성공은 저장된 작업 실패·차단이나 빈 결과에도 0, 조회 오류는 1과 `db_not_found`·`db_unreadable`·`db_busy`·`unsupported_schema`·`unknown_project`·`unknown_plan`, 인자 오류는 기존 stderr 안내와 2를 사용한다. 기존 비밀값 제거 규칙을 적용한다.
- 기존 사용자 문서에 실제 list/status 명령·범위, 공통 JSON 필드의 자료형·단위·nullable 의미·버전 호환, 종료 코드·오류 code와 정상·미확인·오류 예시를 기록한다. G-235의 새 add/remove 이름을 사용하며 config init·나머지 유지 명령의 동작은 유지한다. G-236의 저장 pause를 읽기만 한다.

## Implementation

- 구현 시작 시 최신 원격 코드에서 같은 조회 기능을 확인한다. 확인한 `loop_manager.py`의 조기 status dispatch와 `application/commands.py:_project_limits_status()`의 읽기 전용 연결·JSON envelope·등록/schema 오류, `runtime/project_registry.py:task_limit()`과 `storage/task_calls.py:managed_count()`를 재사용한다. parser와 초기화 이전 dispatch, 조회 연결·투영·출력의 책임을 기존 경계 안에서 나누며 새 루트 항목·별도 엔진·관측 체계·설정은 추가하지 않는다.
- 기존 schema·프로젝트 registry·작업 상태와 `recovery/recovery_runtime.py`, 선택·알림 사유의 순수 판단을 확인해 재사용한다. schema ensure나 기록 갱신을 숨긴 Repository·복구 helper를 조회에 연결하지 않는다. 쓰기 실행과 설명이 공유할 최소 순수 경계만 분리하며 정책을 복제하지 않는다.
- 읽기 snapshot의 수명 안에서 범위 확인·유효 priority·source 관계·선행 근거를 조회하고 같은 결과에서 두 출력을 만든다. 기존 source resolver·검증된 dependency/priority와 저장 pause를 재사용하며 조회용 정책을 복제하지 않는다. 목록과 상세의 책임을 구분하고 PRODUCT·ARCHITECTURE 및 사용자 안내의 관련 절에 Merge된 동작을 반영한다.

## Validation

- Worker는 기존 CLI·project 서비스·SQLite fixture를 재사용한 targeted unit test로 범위 선택, 종료 작업, 다중 대기·차단 사유, 근거 없는 진전·생존·재개 시각, 오래된 관측과 JSON/기본 출력 동등성을 확인한다.
- 실제 Git·모델을 호출하지 않는 unit case로 list 정렬·--all·직접 source 필터와 선행 없음/충족/미충족/미확인·잘못된 graph·archive/rename 대응을 검사한다. 조회 결과를 manual start 예약으로 사용하지 않는다.
- 정상·오류·JSON 진입에서 잠금·bootstrap·runtime·provider·Git·원격·알림·verify 효과를 실패 fixture로 막고 호출이 없음을 검사한다. DB 없음·손상·읽기 접근 실패·schema 불일치·busy와 선택 column 누락에서 오류 계약 및 directory/DB 미생성을 검증한다.
- 확인한 `tests/test_task_scheduling.py`의 status 무변경·JSON 오류 회귀를 재사용한다. 복수 활성 G·verify 대기/실행·안정 PR·종료 불명 예약·공유 수동 묶음에서 관리 수와 저장 상태를 구분하고 저장 0/config 3→4·개별 5·0 복귀의 조회를 유지한다. 조회에서 scheduler 복구나 receipt 회수를 호출하지 않는다.
- Manager 전체 verify는 조회 전후 schema와 작업·복구·retry·호출·검증·outbox·완료 기록 불변, WAL 및 동시 writer에서 snapshot 일관성 또는 유한 시간의 안전한 오류, 기존 실행 CLI 회귀와 문서/JSON 계약을 실제 provider 없이 확인한다.

## Progress

- [x] 2026-10-06 기준 6b717e3의 R-076, CLI 진입·bootstrap·저장 및 복구 경계와 관련 테스트를 대조했다. status/explain/usage는 없으며 기존 ledger summary와 구조화 복구 기록은 재사용 가능하다. 구현·테스트 실행은 하지 않았다.
- [x] 2026-10-09 eb797d3a에서 최신 R-076 전문과 parser·registry·저장 근거를 대조했다. 새 조회·제어 명령은 아직 없고 기존 status/explain 구분을 최신 list/status 계약으로 개정한다. 구현·테스트 실행 없음.
- [ ] list/status 및 공유 조회 경계 구현·관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-16 delta: 한도 조회·`config set`은 직접 구현으로 소비하고 전체 list/status의 남은 범위만 유지한다. 과거 Progress의 “status 없음”은 당시 조사 기록이며 최신 구현 판단은 이 개정이 대체한다. ID·기존 dependency·이력은 보존하고 테스트/verify 성공은 합성하지 않는다.

- 2026-10-06의 최초 계획은 status 기반 연결과 explain/usage 분리를 제안했다. 최신 R-076은 list 요약과 통합 status 상세를 확정하므로 해당 명령 범위를 대체하며, 상세 근거는 G-195가 같은 status를 확장한다. ID·이전 정적 확인 기록과 기존 dependency는 보존한다.
- complexity medium: 경계가 명확한 parser·읽기 전용 연결·저장 투영·출력을 기존 정책으로 연결한다. risk high: 조회가 쓰기 초기화로 잘못 진입하면 운영 DB와 실행 기록을 바꿀 수 있어 백업·수동 상태 복구가 필요하다. 외부 효과 차단·DB 불변·동시 writer snapshot을 Acceptance Criteria와 Validation으로 검증한다. priority normal이다.
- 2026-10-09: 제공된 snapshot token과 원본 hash에서 미착수를 확인해 title에 맞게 rename한다. 실제 source identity·등록 명령·pause 저장 구현을 선행 연결하며 이 계획에서 제어 동작을 중복 구현하지 않는다.
