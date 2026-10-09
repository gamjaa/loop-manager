---
id: G-231
title: Requirement rename 이후 source 관계 보존
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on:
  - G-230
complexity: medium
risk: high
priority: high
---

## Purpose

Requirement 제목·파일명이 바뀌어도 기존 sources 문자열을 유지한 채 같은 문서의 조회·지문·작업 관계와 archive 자격을 확인한다. 정확한 경로와 고유한 원본 ID만 사용하고 모호한 ID를 임의 선택하지 않는다.

## Acceptance Criteria

- source 조회는 정확한 활성 경로, 기존 작업에 허용된 같은 파일명 archive, 원본 R ID의 유일 후보 순서다. 정확한 경로가 존재하면 중복 ID여도 그 문서를 사용하며, 후보 여러 개·부재·조회 실패를 구분한다. R ID를 추출할 수 없는 기존 source는 정확한 경로 조회를 유지한다.
- source 유효성 검사·문서 읽기·작업 관련성·입력 지문·단계별 문서 변경과 Requirement 최종 판단/이동이 동일 resolver를 사용한다. rename만으로 비종료 작업 연결이 사라지거나 Requirement를 archive하지 않는다.
- 기존 sources·ID·branch·session·실패·사용자 결정을 수정하지 않는다. 내용 변경은 기존 입력·재사용 규칙을 적용하며 같은 R ID를 과거 성공 증명으로 사용하지 않는다.
- archived 문서·ExecPlan은 수정하지 않는다. 새 계획의 archived source를 계속 거부하고 manager:verify-inputs·PRODUCT·경로 이탈·symlink·허용 범위 검사를 유지한다. source 검증 실패를 참조 부재로 바꾸지 않는다.

## Implementation

`catalog/execplan_catalog.py`, `git/command_adapter.py`, `execution/plan_validation.py`, `planning/planning_document_relevance.py`·`requirement_retirement.py`와 기존 source 관계 소비자를 연결한다. Git snapshot과 worktree에서 G-230 원본 ID를 같은 방식으로 읽고 검증된 실제 경로를 반환하는 최소 경계를 공유한다. 기존 archive·retirement token과 관련성 비교에 실제 원문·identity를 결합하고 PRODUCT·ARCHITECTURE에 새 호환 계약을 반영한다.

## Validation

- 기존 catalog·source fingerprint·retirement·stage document fixture의 targeted unit test로 정확한 경로 우선, 활성 rename·archive 대응, frontmatter ID, 둘 다 참조된 중복·archive끼리 중복, 후보 부재/모호·관측 실패를 확인한다.
- 전체 verify는 rename 전후 실제 source 원문·연결·session/실패 이력 보존, 비종료 참조의 archive 거부, 새 archived source 거부와 변경 내용의 결과 재사용 무효화를 확인한다. Git 관측만 mock하고 관계·자격 판단은 실제 코드로 검사한다.

## Progress

- [x] eb797d3a에서 정확한 경로와 같은 파일명 archive fallback만 존재하고 retirement 연결은 sources 문자열의 경로 일치에 의존함을 확인했다. 고유 R ID rename 해석은 없다. 구현·테스트 실행 없음.
- [ ] resolver와 관련 소비자 통합·targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 조회·관계·지문 경계에 동일 해석을 연결하며 새 정책 선택은 없다. risk high: 다른 문서로 source를 바꾸거나 연결을 놓치면 잘못된 구현·Requirement 보관이 발생한다. 원문·이력 보존과 모호함 거부를 유지하고 잘못 게시된 이동은 Git 원문을 이용한 수동 복구가 필요하다.
- 자동 rename을 게시하기 전에 관계 보존을 먼저 구현한다. G-194의 source 필터도 이 경계를 재사용하며 별도 조회 정책을 만들지 않는다.
