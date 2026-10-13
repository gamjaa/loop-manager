---
id: G-274
title: 부분 verify의 외부 프로세스 경계 변경 감지
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on: []
complexity: low
risk: medium
priority: high
---

## Purpose

부분 verify 자체의 실행·종료 계약 변경에서 전체 경로로 전환하는 기존 규칙에 `verification/` 밖의 실제 프로세스 구현을 포함한다. 현재 `_VERIFY_INPUTS`는 `execution/verify_process.py`와 `execution/windows_job.py`를 포함하지 않아, 이 종료/격리 경계를 변경해도 관련 테스트나 공통 검사만 선정해 부분 실행할 수 있다.

## Acceptance Criteria

- 공통 core·runner·Manager adapter가 사용하는 `execution/verify_process.py`의 실행·timeout·프로세스 트리 종료와 `execution/windows_job.py`의 Job 격리·자식 종료 구현 변경을 검증 자체의 계약 변경으로 판정한다. 변경 전후 경로를 확인해 해당 구현의 삭제·rename·이동도 부분 실행 허용으로 바뀌지 않는다.
- 충분한 관련 테스트와 공통 회귀 검사가 있어도 위 변경은 실행 없는 FALLBACK이며, discovery 이후 선택 모듈이나 후속 command를 실행하지 않고 Manager의 기존 전체 verify → Reviewer로 연결한다. NOT_SELECTED·전환 이유·후보/설정/manifest identity를 보존하고 부분 PASS나 전체 receipt를 합성하지 않는다.
- 일반 실행 코드의 Git·SQLite·복구·공개 API·루트 진입점·문서 병행 변경과 전역 분석 불확실성은 R-086의 부분 선정·한계 보고를 유지한다. 프로세스 관련 이름·모든 execution 파일·전체 역방향 의존성이라는 이유로 전환 범위를 확대하지 않는다.
- 선정 규칙의 identity를 갱신해 과거 규칙의 선택 결과와 선행 승인으로 새 후보를 실행·게시하지 않는다. 기존 후보 안정성·결과 완결성·guard·예약·자식 종료 및 실제 전체 verify 뒤 최종 승인 경계를 유지한다.
- ARCHITECTURE·VERIFY의 전환 설명을 실제 책임 경계와 일치시킨다. 프로세스 구현·OS 지원·timeout·adapter나 verify 입력 재사용 정책 자체를 변경하지 않는다.

## Implementation

`verification/impact.py`의 검증 계약 변경 판정에 현재 core·runner·manager_adapter의 명시적인 외부 프로세스 책임을 연결한다. 기존 변경 전후 경로 수집과 선정/알고리즘 identity를 재사용하고 새로운 범용 의존성 분석이나 설정 파일을 만들지 않는다. 내부 테스트 이름이나 폴더 위치만으로 검증 엔진의 책임을 추정하지 않는다.

`tests/test_verify_impact.py`의 실제 선정 로직과 `tests/test_partial_verify_runner.py`의 실행 경계 fixture를 사용한다. R-086의 예산·문서 소비 분석·일반 선행 승인 전체 경로는 재구현하지 않는다. G-241의 전체 verify 입력 증명/생략과 G-183의 최종 Review 재사용은 별도 목표로 유지한다.

## Validation

- Worker는 두 외부 프로세스 구현의 변경·삭제·rename을 실제 source graph와 충분한 manifest/공통 검사 입력으로 제공하는 최소 선정 unit case를 확인한다. 대상 부재 때문에 우연히 fallback하는 fixture를 사용하지 않으며, 전환 이유가 검증 계약 변경임을 검사한다.
- 기존 runner fixture에서 새 이유의 FALLBACK 뒤 adapter 실행·command 호출 수 0과 NOT_SELECTED를 확인한다. 일반 Git/SQLite/복구·문서 변경은 같은 fixture에서 부분 실행을 유지하고, 이전 알고리즘의 선택/승인 거부와 현재 identity 대조 회귀를 보존한다.
- Manager 전체 verify는 부분 FALLBACK 후 기존 전체 경로, 선행 FAIL의 전체 미호출, 선행 PASS 뒤 실제 전체 run·최종 승인·게시 재확인과 프로세스 종료/예약 보호를 유지해야 한다. mock Windows 결과를 실제 OS 검증으로 기록하지 않는다.

## Progress

- [x] 2026-10-13 delta 기준 `f56739c31a49ec8fb5547a459688bfb02772586e`의 R-086·`_VERIFY_INPUTS`·core/runner/Manager adapter import와 선정/runner 회귀를 정적으로 확인했다. 검증 폴더·스크립트·설정 변경은 전환하지만 위 외부 프로세스 소유 경로는 빠져 있다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 외부 프로세스 계약 변경 판정·규칙 identity·설명과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity low: 기존 경로 기반 계약 판정과 identity/실행 없는 fallback을 재사용하는 국소 보완이다. risk medium: 누락은 검증 계약이 바뀐 후보에서 부분 실행을 허용하고 과도한 확대는 일반 선행 경로를 막는다. 정확한 책임 근거·이전 선택 거부·양쪽 실행 경로 회귀와 기존 전체 검증/진단 보존으로 교정한다.
- priority high: 무인 검증의 명시적인 안전 전환 조건을 보완한다. 이미 구현된 R-086 선정 전체나 catalog의 입력 증명·OS 실측 목표를 중복하지 않으며, G-273의 배정 동작 교정과 독립적으로 검증·Merge할 수 있다.
