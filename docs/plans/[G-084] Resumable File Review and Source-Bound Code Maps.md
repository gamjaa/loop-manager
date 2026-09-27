---
id: G-084
title: Resumable File Review and Source-Bound Code Maps
sources:
  - docs/requirements/[R-042] Periodic Repository-Wide Code Assessment for Token-Efficient Refactoring.md
depends_on:
  - G-083
complexity: high
risk: high
priority: normal
---

## Purpose

선정된 파일의 실제 내용을 여러 작업 단위로 읽고 검토하면서, 같은 읽기에서 후속 관계 확인에 쓸 짧은 코드맵과 잠정 발견을 만든다. 중단 뒤 검증된 단위를 재사용하되 검토하지 않은 파일을 완료로 간주하지 않는다.

## Acceptance Criteria

- 유지보수 Planner는 G-083이 선정한 각 파일의 원본을 source revision에 고정해 실제로 읽고, 책임·진입점·공개 심벌·직접 참조·관련 테스트 경로의 짧은 코드맵을 같은 과정에서 작성한다. 별도 코드맵 선행 스캔이나 배포·등록 시 Agent 호출이 없다.
- 파일별 잠정 검토는 가독성, 책임·외부 연동 혼합, 오류 복구, 테스트 경계, 중복 구현, 후속 충돌과 관련 없는 문맥 감소 근거를 기록한다. 줄 수·토큰 수 하나로 개선을 강제하지 않는다.
- 잠정 기록에는 원본 경로·지문·mode·revision, 실제 검토 근거, 아직 확인할 직접 참조·유사 책임 후보와 코드맵을 결합한다. 문맥으로만 읽은 기존 파일은 검토 완료 대상이나 코드맵 갱신 대상으로 표시하지 않는다.
- 여러 단위로 나누어도 선정 집합의 누락·중복을 검증하고 중단 뒤 같은 source에서 검증된 결과만 재사용한다. 경로·내용·mode·revision 변경 또는 잘못된 결과는 해당 잠정 기록을 무효화하거나 재검토 대상으로 되돌린다.
- 코드맵과 잠정 검토만으로 성공 ledger나 프로젝트 평가 기준값을 전진시키지 않는다. 제품 문서와 AGENTS.md의 개선 발견·자동 수정은 이 경로에서 만들지 않는다.

## Implementation

- G-083 inventory를 입력으로 하는 bounded 파일 검토 단위, source-bound 결과 형식과 검증기를 별도 모듈에 만든다. 현재 유지보수 Planner prompt의 평가 기준을 파일별 실제 내용 검토와 코드맵 동시 작성으로 확장한다.
- 파일별 잠정 결과·남은 대상·session 진행을 기존 유지보수 소유권 및 SQLite 복구 흐름에 결합하되, 성공 ledger와 분리해 저장한다. 참조 확인 후보를 후속 G-085가 소비할 수 있게 정규화한다.
- 변경되지 않은 관련 파일은 필요한 문맥으로만 읽고 source revision의 Git blob으로 검증한다. G-059의 self 전용 문서 평가 경로는 변경하지 않는다.

## Validation

- Worker는 파일 검토 결과 파서·잠정 상태의 targeted unit test와 직접 변경한 `tests/test_maintenance_planning.py`의 prompt 관련 targeted test를 실행한다.
- 다중 단위의 누락·중복, 중단·재개, source drift, 코드맵 생성·무효화, 관련 테스트·심벌 기록, 문맥 파일 비전진, 빈 내용·허위 검토 근거 거부를 확인한다.
- 전체 verify는 기존 maintenance Planner 프로토콜, self 일회성 평가와 READY 장벽의 회귀를 확인한다.

## Progress

- [x] source-bound 파일 검토와 코드맵 결과 계약
- [x] 검증된 잠정 단위의 저장·재개

## Decision Log

- 코드맵은 실제 파일 검토의 부산물이며 완전한 관계 색인이 아니다. 관계 종합과 최종 완료 판정은 G-085에서 한다.

- 검토 결과 전체의 고정 48,000자 제한을 제거한다. 필드 길이·일반 목록 수 제한과 원본 byte 구간 수 검증을 유지해, 필수 구간 근거의 증가가 유효한 파일 검토·재개를 막지 않게 한다. 160개 구간과 48,000자를 넘는 결과의 저장·재개 및 누락 구간 무효화를 targeted unit test로 확인한다.
