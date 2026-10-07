---
id: G-212
title: legacy 평가 batch의 receipt 처리 중복 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-074] Single-Pass Maintenance Cycle.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

저장된 종전 파일별 평가 복구의 파일·관계·최종 처분 batch에 반복된 receipt 순회/오류 수집을 국소 재사용 경계로 모은다. 새 모듈 평가와 성공 근거의 의미를 분리한 채 legacy 재개 계약을 보존한다.

## Acceptance Criteria

- `review_files`, `review_relations`, `resolve`의 공통 envelope 오류 처리·누락 확인·개별 검증/저장·오류 누적이 같은 규칙을 사용한다. 단위별 parser·identity·session·fail/complete는 각 책임에 유지된다.
- 유효 결과의 부분 저장, 전체 형식 오류의 단위별 fail, SourceInventoryError 즉시 중단과 중단 뒤 미완료 단위 재개를 보존한다. 이미 저장된 성공을 반복 호출하거나 실패/낡은 원본을 성공으로 합성하지 않는다.
- synthesis의 순차 cursor·처분과 코드맵/의미 receipt 구분을 유지한다. 사용자 동작·공개 계약(port/결과 프로토콜)·저장 형식(ledger/receipt)·게시 transaction 및 R-074의 평가 1회/종료를 보존한다.

## Implementation

- `loop_manager_internal/planning/assessment_batches.py`에서 반복된 receipt 판독·순회·오류 누적까지만 기존 helper로 묶는다. 단계별 원본 조회·parser·저장 및 synthesis 제어는 남기고 범용 batch framework를 만들지 않는다.
- 종전 cycle 복구 경로만 정리한다. 새 파일별/관계/종합 호출이나 모듈 평가의 루트 조사 정책을 추가하지 않는다.

## Validation

- Worker targeted unit: `tests.test_assessment_batches`의 누락·잘못된 원본·전체 형식 오류·부분 최종 처분·중단 재개. SourceInventoryError 발생 전의 유효 저장 및 이후 미처리, 성공 단위 재호출 부재를 확인한다.
- Manager 전체 verify: legacy 평가/게시 복구와 신규 모듈 receipt 재사용, session/cursor 보존·성공 미전진 및 단일 cycle의 확정 범위 Merge 뒤 종료를 유지한다.

## Progress

- [x] 저장된 평가 발견 4의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/planning/assessment_batches.py:224` 및 저장된 세 처리 루프 중복 판단을 재사용했다.
- G-179는 장벽 편집, G-206은 모듈 루트 조사이며 이 legacy receipt 처리 추출과 별개다. 추가 기존 READY prerequisite는 없다. complexity medium은 단계별 실패/부분 저장의 명확한 경계 연결, risk medium은 해당 평가의 재개/처분 오류이며 단위별 근거·session·intent 보존으로 복구한다.
