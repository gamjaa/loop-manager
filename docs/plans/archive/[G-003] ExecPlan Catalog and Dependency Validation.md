---
id: G-003
title: ExecPlan Catalog and Dependency Validation
sources:
  - docs/PRODUCT.md
depends_on:
  - G-002
complexity: low
risk: medium
---

## Purpose

프로젝트의 모든 ExecPlan을 하나의 결정적 카탈로그로 파싱하고 의존성 그래프를 검증해, 잘못된 메타데이터나 순환 의존성이 계획 게시 또는 Worker 시작 이후까지 남지 않게 한다.

## Acceptance Criteria

- `docs/plans/` 바로 아래의 모든 Markdown ExecPlan을 파일명과 무관한 순서에서도 동일한 결과가 나오도록 파싱하고 ID로 조회할 수 있다.
- 각 ExecPlan은 `docs/PLANS.md`의 고정 frontmatter key, ID 형식, 파일명, complexity와 risk 값을 만족해야 한다.
- `sources`는 존재하는 `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래의 활성 Requirement만 허용하며, 빈 목록, 절대 경로, 상위 경로 참조와 archive 경로를 거부한다.
- 모든 ExecPlan ID는 고유해야 하고, `depends_on`의 각 항목은 올바른 ExecPlan ID 형식이며 존재하는 다른 계획을 가리켜야 한다.
- 직접 또는 간접 순환 의존성을 모두 거부하고 오류에 문제를 구성하는 ExecPlan ID를 포함한다.
- Planner 결과를 게시하기 전과 Worker가 선택된 계획을 시작하기 전에 전체 카탈로그 검증을 사용해, 기존 계획을 포함한 잘못된 그래프에서는 실행하지 않는다.
- 정상 카탈로그와 중복 ID, 잘못된 source, 존재하지 않는 의존성, 자기 의존성 및 다단계 순환 의존성을 단위 테스트로 검증한다.

## Implementation

- 현재의 단일 파일 frontmatter 파싱을 재사용하되, `docs/plans/*.md`를 결정적 순서로 읽어 ID 기반 카탈로그를 만드는 경계를 추가한다.
- 개별 계획의 필드와 source 경로를 검증한 뒤 전체 ID 집합을 기준으로 의존성 참조를 검증한다.
- 의존성 그래프를 순회해 자기 의존성과 간접 순환을 검출하고 진단 가능한 오류를 반환한다.
- 기존 Planner 검증과 Worker 시작 경로가 선택된 한 파일만 독립적으로 검사하지 않고 검증된 카탈로그에서 해당 계획을 조회하도록 연결한다.
- SQLite ExecPlan 상태 저장, READY 선택, 재시도, 다중 프로젝트 처리와 provider 확장은 후속 계획으로 남긴다.

## Validation

- `scripts/verify.ps1`
- `scripts/verify.sh`
- ExecPlan 카탈로그, source 제한과 의존성 그래프 검증 단위 테스트

## Progress

- [ ] 결정적 ExecPlan 카탈로그 파싱과 개별 메타데이터 검증
- [ ] 전체 의존성 참조 및 순환 검증
- [ ] Planner 게시 및 Worker 시작 경로 연결
- [ ] 정상·오류 경계 단위 테스트 추가

## Decision Log

- 전체 SQLite 상태 모델보다 먼저 파일 기반 계획 카탈로그의 신뢰 경계를 완성해, 후속 READY 등록과 의존성 충족 판단이 동일한 검증 결과를 재사용하게 한다.
