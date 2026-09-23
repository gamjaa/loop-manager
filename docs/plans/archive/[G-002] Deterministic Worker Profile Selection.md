---
id: G-002
title: Deterministic Worker Profile Selection
sources:
  - docs/PRODUCT.md
depends_on:
  - G-001
complexity: low
risk: medium
---

## Purpose

ExecPlan의 `complexity`와 `risk`를 코드로 Worker level에 매핑하고, 현재 Codex 기반 실행기가 해당 level의 Worker Model과 Effort를 사용하도록 한다.

## Acceptance Criteria

- `complexity`는 low=1, medium=3, high=5로, `risk`는 low=0, medium=1, high=2, critical=3으로 환산하고 합계를 최대 7로 제한해 `level1`부터 `level7`까지 결정한다.
- 선택된 level은 `config/config.ini`의 `[codex.worker]` 항목과 `[codex.models]` alias를 통해 Model과 Effort로 해석된다.
- Worker 실행에는 선택된 Worker Model과 Effort가 사용되며 Planner와 Reviewer의 기존 profile 선택은 바뀌지 않는다.
- 필요한 Worker level, Model alias 또는 Effort 설정이 없거나 비어 있거나 형식이 잘못된 경우 Codex Worker를 시작하기 전에 명확한 설정 오류로 중단한다.
- level 경계, 최대값 제한, 정상 profile 해석과 잘못된 설정을 단위 테스트로 검증한다.

## Implementation

- 검증된 ExecPlan 메타데이터에서 Worker level을 계산하는 부작용 없는 함수를 추가한다.
- 역할 profile과 Worker level profile이 각자의 config section에서 Model alias와 Effort를 해석하도록 설정 읽기 경계를 정리한다.
- 구현 단계에서 현재 ExecPlan을 검증한 뒤 계산된 Codex Worker profile로 Agent를 실행하도록 연결한다.
- 프로젝트별 provider 선택, Claude Code 실행, 전체 ExecPlan 상태 모델과 다중 프로젝트 처리는 후속 계획으로 남긴다.

## Validation

- `scripts/verify.ps1`
- `scripts/verify.sh`
- Worker level 계산과 config profile 해석에 대한 단위 테스트

## Progress

- [ ] Worker level 계산 구현
- [ ] Codex Worker profile 해석 및 실행 연결
- [ ] 정상·오류 경계 단위 테스트 추가

## Decision Log

- 최소 부트스트랩의 Codex 단일 provider 경계를 유지하면서, PRODUCT와 ARCHITECTURE에 이미 고정된 결정적 Model/Effort 선택 규칙부터 연결한다.
