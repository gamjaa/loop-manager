---
id: G-194
title: 읽기 전용 상태 조회 경계와 status CLI
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] Read-Only Operational State CLI.md
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
complexity: high
risk: high
priority: normal
---

## Purpose

실행·복구를 시작하지 않고 기존 SQLite의 프로젝트와 작업 상태를 조회하는 `status`를 제공한다. 후속 `explain`·`usage`도 재사용할 읽기 전용 연결, 일관된 snapshot과 출력·오류 계약을 함께 만든다. 완료 조건은 저장된 근거에 맞는 상태 설명과 조회의 무변경성이며, 새 관측 수집이나 실행 정책 변경은 범위 밖이다.

## Acceptance Criteria

- R-076의 `status [repository] [--plan <id>] [--json]`와 기존 전역 `--state-dir`를 지원한다. 전체 조회는 등록 프로젝트와 비종료 작업, 프로젝트 조회는 해당 프로젝트만 보여준다. plan 필터는 repository를 요구하며 명시한 종료 작업도 조회한다. repository와 plan ID로 식별하고 경로·제목으로 대상을 추측하지 않는다.
- 프로젝트 phase와 계획 status를 구분하고 저장된 구조화 사유·복구 기록으로 사용량 제한, 일시 재시도, 사용자 판단, dependency, PR 대기와 프로젝트 차단·planning 오류를 설명한다. 여러 사유와 독립 계획을 보존하며 미충족 dependency ID·상태, 저장된 PR URL·관측값, 확인된 사용자 대상과 다음 조건을 표시한다. legacy 오류 문자열로 사유를 새로 확정하지 않는다.
- 실제 진전을 증명하는 저장 사건만 마지막 진전의 시각·출처로 사용한다. invocation 시작·종료, DB 갱신·오류 관측만 있으면 진전은 미확인이다. 프로세스 존재·활동·재개 가능성·관측 신선도를 따로 표현하고 잠금이나 미종료 호출로 활동·생존을 확정하지 않는다. 조회 시각과 원본 관측 시각·출처·경과시간을 구분하며 없는 시각과 다음 실행·제한 해제 시각을 추정하지 않는다.
- 기존 저장 결정과 순수한 선택·복구·재시도 판단을 재사용한다. 기존 유효기간이 없는 관측에 새 기준을 만들지 않고 원격 확인이나 상태 변경이 필요한 판단은 재검사 필요·미확인으로 남긴다. 다음 외부 실행의 재검사는 재개 성공을 보장하지 않는다.
- 조회 진입은 실행 잠금, bootstrap·runtime 자식, 설정/provider 초기화, schema 초기화·migration, 중단 호출 정리와 실행 경로를 거치지 않는다. 에이전트·verify·Git·원격 API·게시·알림·복구를 호출하거나 작업·retry·관측·완료 기록을 바꾸지 않는다. DB가 없으면 directory·DB를 만들지 않는다.
- 읽기 전용 SQLite와 유한한 busy 대기, 하나의 일관된 읽기 snapshot을 사용한다. 필수 table·column을 읽기만으로 검사하며 선택 정보 누락은 미확인이다. WAL 등 제약으로 읽을 수 없으면 쓰기 연결·migration·journal mode 변경·checkpoint·자동 복원 없이 오류를 반환한다. 실행 중인 다른 Manager의 실행 잠금을 변경하지 않는다.
- 기본 출력과 JSON은 같은 사실·미확인 의미를 제공한다. JSON은 R-076의 공통 envelope와 `null`·`[]`·실측 `0`, 경고·미확인 이유를 보존하며 stdout에는 문서 하나만 출력한다. 조회 성공은 저장된 작업 실패·차단이나 빈 결과에도 0, 조회 오류는 1과 `db_not_found`·`db_unreadable`·`db_busy`·`unsupported_schema`·`unknown_project`·`unknown_plan`, 인자 오류는 기존 stderr 안내와 2를 사용한다. 기존 비밀값 제거 규칙을 적용한다.
- 기존 사용자 문서에 실제 status 명령·범위, 공통 JSON 필드의 자료형·단위·nullable 의미·버전 호환, 종료 코드·오류 code와 정상·미확인·오류 예시를 기록한다. 기존 실행 명령의 동작은 유지한다.

## Implementation

- 구현 시작 시 최신 원격 코드에서 같은 조회 기능을 확인한다. `loop_manager.py`는 parser와 실행 초기화 이전의 조회 dispatch만 담당하고, 조회 연결·저장 사실 투영·출력 책임은 기존 application/storage 경계 안에서 분리한다. 새 루트 항목·별도 실행 엔진·관측 체계·설정은 추가하지 않는다.
- 기존 schema·프로젝트 registry·작업 상태와 `recovery/recovery_runtime.py`, 선택·알림 사유의 순수 판단을 확인해 재사용한다. schema ensure나 기록 갱신을 숨긴 Repository·복구 helper를 조회에 연결하지 않는다. 쓰기 실행과 설명이 공유할 최소 순수 경계만 분리하며 정책을 복제하지 않는다.
- 읽기 snapshot의 수명 안에서 범위 확인과 사실 조회를 완료한 뒤 사람이 읽는 출력과 JSON을 같은 결과에서 만든다. 오류·선택 정보 누락도 공통 계약으로 정규화하고 기존 README 등 사용자 문서를 갱신한다. 장기 제품 규칙 반영은 AGENTS.md와 docs/AGENTS.common.md·docs/AGENTS.worker.md의 문서 권한을 따른다.

## Validation

- Worker는 기존 CLI·project 서비스·SQLite fixture를 재사용한 targeted unit test로 범위 선택, 종료 작업, 다중 대기·차단 사유, 근거 없는 진전·생존·재개 시각, 오래된 관측과 JSON/기본 출력 동등성을 확인한다.
- 정상·오류·JSON 진입에서 잠금·bootstrap·runtime·provider·Git·원격·알림·verify 효과를 실패 fixture로 막고 호출이 없음을 검사한다. DB 없음·손상·읽기 접근 실패·schema 불일치·busy와 선택 column 누락에서 오류 계약 및 directory/DB 미생성을 검증한다.
- Manager 전체 verify는 조회 전후 schema와 작업·복구·retry·호출·검증·outbox·완료 기록 불변, WAL 및 동시 writer에서 snapshot 일관성 또는 유한 시간의 안전한 오류, 기존 실행 CLI 회귀와 문서/JSON 계약을 실제 provider 없이 확인한다.

## Progress

- [x] 2026-10-06 기준 6b717e3의 R-076, CLI 진입·bootstrap·저장 및 복구 경계와 관련 테스트를 대조했다. status/explain/usage는 없으며 기존 ledger summary와 구조화 복구 기록은 재사용 가능하다. 구현·테스트 실행은 하지 않았다.
- [ ] status 및 공유 조회 경계 구현·관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 쓰기 초기화를 우회하고 동시 실행 중 일관된 상태를 읽는 안전 경계를 먼저 확정해야 하므로 complexity high, risk high다. 운영 조회 편의의 필수 구현으로 priority normal이다.
- `explain`과 `usage`는 이 계획의 연결·범위·출력 계약에 의존하며 각각 별도로 검증·Merge한다. 저장되지 않은 정보는 미확인으로 제공한다.
