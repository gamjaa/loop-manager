---
id: G-234
title: 작업별 저장 로그의 읽기 전용 CLI
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
depends_on:
  - G-194
  - G-265
  - G-266
  - G-267
  - G-268
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: medium
risk: medium
priority: normal
---

## Purpose

`logs <repository> --plan <id> [--tail N]`에서 해당 작업에 연결된 기존 실행·verify·Review 로그를 조회한다. 로그를 생성하거나 상태·관측을 갱신하지 않는다.

## Acceptance Criteria

- 정확한 repository/plan의 저장 연결로 최근 로그를 선택하고 단계·실행 ID·경로·내용을 구분한다. 다른 작업의 로그를 같은 근거로 선택하지 않는다.
- 양의 N으로 각 선택 로그의 마지막 N줄을 제한한다. 로그 미수집·선택 정보 부재는 미확인, 저장 원문 접근 실패는 대상과 읽기 오류다. 잘못된 N은 인자 오류다.
- 기존 비밀값 제거를 적용하며 로그·DB·retry·invocation·outbox를 변경하지 않는다. G-194의 읽기 snapshot·조회 오류 계약을 재사용하고 잠금·bootstrap·Git·원격·Agent·verify·알림을 실행하지 않는다.
- 사용 안내에 명령·범위·tail·누락/오류·종료 코드를 설명하고 PRODUCT·ARCHITECTURE에 조회 책임을 반영한다.

## Implementation

기존 호출 ledger·저장 로그 경로·verify 진단과 G-194 조회 dispatch를 연결한다. DB snapshot에서 확인한 작업 identity에 맞는 원문만 읽고 기존 저장·redaction 경계를 재사용한다. 새 로그 저장 구조·실시간 수집기를 만들지 않는다.

## Validation

- 파일/SQLite fixture targeted unit test로 작업 격리·복수 로그·tail·빈/누락·읽기 실패·비밀값 제거를 검사한다.
- 전체 verify는 정상·오류의 외부 효과 차단과 로그/DB 전후 불변, 기존 실행 로그·verify 진단 회귀를 확인한다. 실제 provider 호출은 없다.

## Progress

- [x] eb797d3a parser에는 logs가 없고 기존 ledger·로그는 재사용 가능하다. 정적 확인만 수행했다.
- [ ] 구현·targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 저장 연결·파일 오류·출력을 여러 기존 경계에 연결한다. risk medium: 잘못된 작업 로그 표시와 진단 노출을 기존 identity·비밀값 제거로 제한하고 조회 코드 교정으로 복구한다.
