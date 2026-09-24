---
id: G-058
title: Provider-Neutral Quality Guidance
sources:
  - docs/requirements/[R-027] Shared Quality Guidance and Full Project Refactoring Assessment.md
depends_on:
  - G-051
complexity: medium
risk: medium
priority: normal
---

## Purpose

현재 `worker_prompt`, `reviewer_prompt`와 Planner prompt는 역할별 계약을 제공하지만 코드·문서의 불필요한 증가를 막는 공통 판단 기준은 분산돼 있다. 기존 등록 프로젝트에도 provider에 관계없이 같은 기준을 적용한다.

## Acceptance Criteria

- Worker·Reviewer는 기존 코드 및 표준 도구 재사용, 현재 목표에 필요한 최소 구조, 오류 처리·보안·검증 보존을 같은 provider-neutral prompt에서 확인한다.
- Planner와 Specification Skill의 문서 검토는 중복·장황함을 줄이면서 PRODUCT·Requirement·ARCHITECTURE의 역할, 결정·예외·실패 흐름과 Acceptance Criteria의 의미를 유지한다.
- 외부 스킬 설치·호출에 의존하지 않고 Codex·Claude 및 기존·신규 등록 프로젝트에서 같은 핵심 기준이 적용된다. 스킬 사용 가능 여부가 실행 성공 조건이 되지 않는다.
- 기존 Agent 응답 protocol, 문서 위치·저장 형식, 사용자 동작과 scope 권한은 유지한다. R-027의 의미 보존 명세 문서 리팩터링 예외는 공통 `AGENTS.md`에 명시해야 하며, Worker가 기존 guidance 제안·사용자 결정 절차로 승인받은 뒤에만 적용한다. 기존 등록 프로젝트의 `AGENTS.md`는 자동 덮어쓰지 않는다.

## Implementation

- 중복 prompt 문구를 재사용 가능한 좁은 품질 지침으로 합치고 `loop_manager.py`의 역할별 prompt와 `.agents/skills/specification-docs/SKILL.md`의 문서 가독성 지침에 연결한다.
- 외부 ponytail·i-have-adhd의 설치나 실행을 필수로 하지 않는다. 현재 요구사항보다 짧은 문서나 검증 축소를 유도하는 문구는 넣지 않는다.
- 공통 명세 문서 리팩터링 예외가 현재 `AGENTS.md`에 없으면 Worker가 선택된 ExecPlan의 Decision Log에 정확한 patch와 빈 User Decision을 기록하고 승인 절차에서 중단한다. 승인된 patch만 적용하며 문서 리팩터링의 개별 ExecPlan·PR·사용자 Merge 조건을 명시한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 역할별 prompt, `tests/test_managed_configuration.py`의 공통 skill 배포 관련 targeted unit test만 실행한다.
- Codex·Claude 및 이미 등록된 프로젝트의 prompt가 동일 기준을 포함하고, 기존 protocol·권한·필수 명세 요소가 유지되는지 검증한다.
- 공통 guidance 예외의 승인 전 직접 수정 거부, 승인 후 정확한 patch 적용과 기존 등록 프로젝트 무배포를 검증한다.
- 전체 verify에서 Planner validation, Reviewer parsing, 공통 파일 설치와 Agent scope 회귀가 없어야 한다.

## Progress

- [ ] 역할별 공통 품질 지침 연결
- [ ] 문서 skill의 완전성·가독성 지침 갱신

## Decision Log

- 외부 스킬은 참고 근거이며 제품의 실행 의존성이 아니다.
