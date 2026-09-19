---
id: G-019
title: Review-Driven Worker Profile Escalation
sources:
  - docs/requirements/[R-009] Review-Driven Exceptional Worker Level Escalation.md
depends_on:
  - G-013
complexity: high
risk: high
priority: critical
---

## Purpose

성공한 전체 verify 뒤에도 Reviewer의 actionable blocking issue를 해결하지 못하는 ExecPlan만 저장된 Review 진행 상태에 따라 더 강한 Worker profile로 단계적으로 승격하고, level 7에서도 같은 조건이 충족된 예외에 한해 provider별 level 8을 사용한다.

## Acceptance Criteria

- complexity와 risk로 계산하는 최초 Worker level은 기존과 같이 1~7이며 level 8을 최초 선택 결과로 반환하지 않는다.
- 유효한 `VERDICT: FAIL`의 Review 실패 지문과 그 Review를 준비한 effective Worker level을 G-013의 Review 실패 이력과 함께 SQLite에 저장하고, 프로세스와 Worker 세션 재개 뒤에도 복구한다.
- 같은 level에서 동일 Review 실패 지문이 Worker 수정과 성공한 verify 뒤 한 번 다시 확인되거나 서로 다른 Review 실패 지문 세 개가 확인되면 다음 Worker 수정 호출부터 effective level을 정확히 한 단계 올린다.
- level 1~6의 승격은 기존 다음 level profile을 사용하고, level 7에서 승격 조건을 충족한 경우에만 level 8을 사용하며 level 8보다 높은 단계는 만들지 않는다.
- 하나의 ExecPlan 안에서 effective level은 단조 증가한다. level 승격 시 G-013의 Review 실패 지문 반복 이력을 지우지 않아 동일 지문 반복 `BLOCKED` 한도를 우회하지 않는다.
- 전체 verify 실패, Worker 결과 형식 오류, Reviewer CLI 실패·timeout·사용 한도 소진 및 유효한 FAIL verdict가 없는 결과는 Review 승격 이력과 effective level을 변경하지 않는다.
- Review 수정 뒤 verify가 실패하면 verify 지문만 갱신하고, 같은 Review issue가 Reviewer에게 다시 확인되기 전에는 Review 승격 조건에 포함하지 않는다.
- 새 ExecPlan을 시작하면 complexity와 risk에서 최초 level을 계산하고, ExecPlan 또는 근거 문서 변경으로 일반 BLOCKED가 정상 해제되면 effective level과 level별 승격 이력을 그 최초 level 상태로 초기화한다.
- Codex level 8은 `gpt-6-astra,low`를 사용한다. Claude level 8은 Fable로 model을 바꾸지 않고 기존 Opus strong alias와 `max` effort를 사용한다.
- `config/config.ini.example`과 운영 `config/config.ini`의 Codex·Claude Worker level 8 값을 provider별 공통 profile 검증 경계에서 검사한다. G-015가 연결할 Claude runtime은 이 경계를 그대로 사용할 수 있고, 이 계획은 Claude CLI adapter나 프로젝트 provider registry를 중복 구현하지 않는다.
- 지원되지 않는 level 8 Model/Effort 조합은 Worker 호출과 비용 발생 전에 설정 오류로 거부한다.

## Implementation

- 최초 계산 level과 현재 effective level을 분리하고, effective level 및 level별로 관찰한 Review 지문·반복 정보를 호환 가능한 SQLite migration으로 저장한다.
- G-013의 결정적 Review fingerprint 기록 지점에서 유효한 Reviewer FAIL만 승격 평가기에 전달하고, 같은 지문 재확인 및 서로 다른 지문 개수 조건을 순수 상태 전이 함수로 구현한다.
- Worker 호출 직전에 저장된 effective level을 사용하고, 승격을 유발한 Review 다음 수정부터 새 profile이 적용되도록 상태 저장과 호출 순서를 명확히 한다.
- Codex와 Claude 설정을 같은 level 1~8 profile 계약으로 해석하는 provider-neutral lookup/validation 경계를 만들되, 실제 provider CLI adapter와 프로젝트 provider 선택은 G-015의 범위로 남긴다.
- 새 작업 시작, 일반 BLOCKED 해제, verify/Review 성공과 외부 실패 경로별 초기화·보존 책임을 G-013의 재시도 상태와 함께 한 transaction 경계에 둔다.

## Validation

- Worker는 Worker level 상태 전이와 profile validation에 직접 관련된 test class 또는 test module만 targeted unit test로 실행한다.
- 최초 level 1~7 계산, 동일 지문의 두 번째 확인, 같은 level의 서로 다른 지문 세 개, level 1~6의 단일 단계 승격과 level 7→8 전이를 검증한다.
- level 8 상한, 한 ExecPlan 내 단조 증가, 프로세스·세션 재개 시 보존 및 새 ExecPlan·일반 BLOCKED 해제 시 초기화를 검증한다.
- verify 실패와 외부 Reviewer 오류가 승격되지 않는 경우, Review 수정 뒤 verify 실패가 Review 이력에 합산되지 않는 경우와 level 승격 뒤에도 동일 Review 지문의 G-013 `BLOCKED` 횟수가 유지되는 경우를 검증한다.
- Codex `frontier,low`와 Claude `strong,max` 해석, Claude가 Fable로 바뀌지 않는 조건 및 지원되지 않는 provider별 Model/Effort의 사전 거부를 검증한다.
- 전체 verify에서 기존 level 1~7 선택, G-013의 progress-aware retry, 세션 복구와 실패 지문 초기화가 회귀하지 않아야 한다.

## Progress

- [ ] effective Worker level과 level별 Review 승격 이력 저장
- [ ] Review 실패 기반 승격 상태 전이와 orchestration 연결
- [ ] Codex·Claude level 8 profile 해석 및 설정 검증
- [ ] 승격, 제외 조건, 재개·초기화와 상한 회귀 테스트 추가

## Decision Log

- R-009가 지정한 실행 순서를 지키기 위해 G-013만 선행 조건으로 두고 `priority: critical`을 사용한다. provider-neutral level 8 profile 경계를 먼저 제공하되 Claude CLI 실행과 registry는 기존 G-015가 담당한다.
- 비용이 큰 level 8은 complexity, risk나 priority의 최초 계산에는 포함하지 않고, level 7에서 저장된 Review 실패가 승격 조건을 충족한 경우에만 선택한다.
