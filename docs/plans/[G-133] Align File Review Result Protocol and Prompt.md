---
id: G-133
title: Align File Review Result Protocol and Prompt
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: medium
risk: medium
priority: critical
---

## Purpose

파일별 유지보수 평가가 출력 줄바꿈, 설명되지 않은 필드 길이 제한과 root 조사 결과의 혼입으로 같은 세션에서 반복 실패한다. 현재 원본에 결합된 평가 범위와 증거 검증을 보존하면서 출력 규약과 프롬프트를 일치시켜 무인 평가가 정상적으로 진행되게 한다.

## Acceptance Criteria

- `FILE_REVIEW_RESULT:`로 표시된 JSON object 하나를 검증한다. 콜론 뒤 공백, 줄바꿈과 JSON 들여쓰기 차이는 코드에서 처리한다.
- 복수 결과, 중복 JSON key, 잘못된 JSON, 누락 필드와 임의 추가 필드를 계속 거부한다. 잘못된 응답에서 의미 있는 값을 추측하거나 버리지 않는다.
- source revision·경로·mode·hash, 실제 원본 인용과 모든 byte 구간의 근거 검증을 유지한다.
- 일반 파일과 별도 root 조사 각각에 유효한 JSON 예시를 제공한다. 예시와 필드 계약은 코드의 검증 기준을 공유하며 각 문자열·배열 제한을 명시한다.
- 일반 파일의 root inventory는 문맥으로 제공한다. 별도 root 조사 결과를 일반 파일 결과의 임의 필드로 추가하지 않도록 출력 위치를 명확히 한다.
- 재시도 오류에는 누락·추가 필드 이름 또는 문자열의 실제 길이와 허용 범위가 포함된다. 기존 세션 재사용, 잠정 결과 저장과 완료 판정은 보존한다.

## Implementation

- 파일별 결과의 JSON envelope, 필드 집합과 형식 안내를 작은 protocol 모듈로 분리하고 현재 parser와 prompt에서 공유한다.
- source와 evidence의 의미 검증은 기존 파일별 review 경계에 유지한다. 저장 형식과 SQLite schema는 변경하지 않는다.
- 응답 예시는 완전한 JSON 구조로 생성하고 identity와 evidence 값은 실제 조사 결과로 교체하도록 안내한다. 일반 파일용 예시에 root 전용 필드를 포함하지 않는다.
- 새 protocol 모듈을 Linux·Windows 전체 verify의 compile 목록에 포함한다.

## Validation

- Worker는 protocol과 prompt의 순수 targeted unit test를 실행한다. 한 줄·여러 줄·공백·code fence, 복수 결과·중복 key·추가 JSON, 누락·추가 필드, 문자열 길이, 잘못된 원본·구간 근거, 일반/root 예시의 계약 일치를 확인한다.
- 기존 구조화된 참조, 큰 source의 chunk 안내, 필드·배열 상한과 비 UTF-8 원본 관련 순수 unit test를 함께 확인한다.
- Manager 전체 verify에서는 잠정 결과 저장·재개, 원격 변경에 따른 invalidation, 별도 root 조사 및 전체 평가 게시 흐름이 유지되는지 확인한다.

## Progress

- [ ] protocol 경계와 출력 예시 구현
- [ ] 관련 targeted unit test 통과
- [ ] Manager 전체 verify 및 읽기 전용 Review 통과
- [ ] 사용자 구현 PR Merge

## Decision Log

- 2026-10-03: 사용자가 반복된 `FILE_REVIEW_RESULT` 오류의 핫픽스를 요청했고 간단한 JSON 예시를 프롬프트에 제공하도록 지시했다. 현재 제품 동작의 코드 결함 수정으로 한정하며 PRODUCT·Requirement·ARCHITECTURE와 AGENTS를 변경하지 않는다.
