---
id: G-004
title: Planning Input Fingerprint and Skip Gate
sources:
  - docs/PRODUCT.md
depends_on:
  - G-003
complexity: low
risk: medium
---

## Purpose

최신 원격 기준의 실제 Planner 입력으로 결정적 지문을 만들고, Planner가 추가 작업이 없다고 확인한 입력만 기록해 무관한 commit 때문에 Planner를 다시 실행하지 않도록 한다.

## Acceptance Criteria

- 계획 입력 지문은 최신 `origin/<base-branch>`의 추적 파일을 기준으로 계산하며 기본 worktree가 원격보다 뒤처져 있어도 그 내용을 사용하지 않는다.
- 지문에는 경로와 파일 내용을 함께 포함해 파일 추가, 삭제, 이름 변경과 내용 변경을 구분한다.
- `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/requirements/` 바로 아래의 `*.md`와 프로젝트의 추적된 소스·테스트·설정 입력을 포함한다.
- `docs/plans/`, `docs/requirements/archive/`와 활성 제품 입력이 아닌 `docs/` 문서는 제외해 ExecPlan 게시나 Requirement archive 이동만으로 Planner를 다시 실행하지 않는다.
- 같은 파일 집합과 내용은 플랫폼, 파일 열거 순서와 기본 worktree 상태에 관계없이 같은 지문을 만든다.
- Planner가 파일을 변경하지 않아 추가 ExecPlan이 필요 없다고 판단한 경우에만 해당 지문을 SQLite에 저장한다.
- 저장된 지문이 현재 지문과 같으면 Planner를 호출하지 않고, 포함된 입력이 바뀌거나 저장된 지문이 없으면 Planner를 호출한다.
- Planner 실패, 결과 검증 실패 또는 새 ExecPlan 생성 시에는 현재 지문을 완료된 입력으로 저장하지 않는다.
- 포함·제외 경계, 결정성, 원격 기준 사용과 지문 저장·skip 조건을 단위 테스트로 검증한다.

## Implementation

- `origin/<base-branch>` Git tree에서 경로와 blob 내용을 읽어 기본 worktree checkout에 의존하지 않는 지문 계산 경계를 추가한다.
- 전체 추적 파일 중 명시된 제품 문서와 활성 Requirement를 포함하고, `docs/` 밖의 추적 파일은 언어별 allowlist 없이 소스·테스트·설정 입력으로 보수적으로 포함한다.
- 정렬된 상대 경로, 내용 길이와 blob 내용을 모호하지 않게 직렬화해 SHA-256 지문을 계산한다.
- 기존 SQLite `projects` schema에 계획 입력 지문 필드를 호환 가능한 방식으로 추가하고 기존 DB를 열 때도 보존적 migration을 수행한다.
- 현재 `processed_sha` 기반 Planner skip을 저장된 계획 입력 지문 비교로 교체하고, 계획 없음 경로에서만 지문을 갱신한다.
- 새로 MERGED된 ExecPlan 감지, `planning_error` 전용 상태와 전체 ExecPlan 런타임 상태 모델은 후속 계획으로 남긴다.

## Validation

- `scripts/verify.ps1`
- `scripts/verify.sh`
- 임시 Git 저장소에서 원격 tree 입력 변경, 제외 경로 변경, 파일 순서와 기본 worktree 차이를 검증하는 단위 테스트
- Planner 성공·실패·계획 생성 결과별 지문 저장 및 재호출 여부 단위 테스트

## Progress

- [ ] 원격 Git tree 기반 계획 입력 선택과 결정적 지문 계산
- [ ] SQLite 지문 저장과 Planner skip gate 연결
- [ ] 포함·제외 및 실행 결과 경계 단위 테스트 추가

## Decision Log

- 전체 ExecPlan 상태 모델에 앞서 독립적인 계획 입력 경계를 도입한다. 새 MERGED 상태를 지문과 별도로 처리하는 예외는 SQLite ExecPlan 상태가 추가될 때 연결한다.
