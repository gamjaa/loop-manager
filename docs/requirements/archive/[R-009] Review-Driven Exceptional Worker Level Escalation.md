# [R-009] Review-Driven Exceptional Worker Level Escalation

## Context

현재 Worker level은 ExecPlan의 complexity와 risk로 작업 시작 시 한 번 결정되며, 이후 Review가 반복해서 새로운 blocking issue를 발견해도 같은 Model과 Effort를 계속 사용한다. 대부분의 작업에는 기존 level 1~7이면 충분하지만, G-012처럼 level 7 Worker가 여러 차례 수정한 뒤에도 Review를 통과하지 못하는 드문 작업은 더 강한 profile을 제한적으로 사용할 필요가 있다.

전체 verify 실패는 구현이나 테스트의 구체적인 오류를 Worker에게 돌려주는 정상적인 수정 과정이며, 그 자체로 작업의 추론 난도가 과소평가됐다는 신호는 아니다. 반면 Review의 actionable blocking issue가 같은 Worker level에서 반복되거나 계속 새로 발견되는 경우에는 현재 level이 작업의 전체 상호작용을 충분히 다루지 못한다는 신호로 사용할 수 있다.

level 8은 일반적인 복잡도·위험도 계산 결과가 아니라, level 7에서도 Review 문제를 해결하지 못한 예외적인 경우에만 사용하는 비용 통제용 최종 승격 단계여야 한다.

## Requirement

Loop Manager는 ExecPlan의 complexity와 risk로 기존과 동일하게 최초 Worker level을 1~7 중 하나로 결정해야 한다. 최초 선택과 일반적인 실행에서는 level 8을 선택할 수 없다.

Worker 수정과 성공한 전체 verify 뒤 Reviewer가 `VERDICT: FAIL`과 actionable blocking issue를 반환하면 Review 실패 지문과 해당 Review를 준비한 Worker level을 저장해야 한다. 같은 Worker level에서 동일한 Review 실패 지문이 Worker 수정 뒤 한 번 다시 확인되거나, 서로 다른 Review 실패 지문 세 개가 확인되면 다음 Worker 수정부터 effective Worker level을 한 단계 올려야 한다. level 1~6의 승격은 기존 level 2~7 profile을 사용한다.

level 7에서 같은 승격 조건을 충족한 경우에만 예외적인 level 8 profile을 사용한다. level 8은 최종 상한이며 추가 자동 승격은 없다. Codex의 level 8은 `gpt-6-astra`와 `low` effort를 사용한다. Claude의 level 8은 Fable로 Model을 바꾸지 않고 `claude-opus-5` 계열의 기존 strong alias를 유지하며, level 7의 `xhigh`보다 한 단계 높은 지원 effort인 `max`를 사용한다.

전체 verify 실패, Worker 결과 형식 오류, Reviewer CLI 실행 실패·timeout·사용 한도 소진, 유효한 `VERDICT: FAIL` 없이 끝난 Review는 Worker level을 올리지 않아야 한다. 이 오류들은 기존 검증, 외부 CLI와 세션 복구 정책을 따른다.

effective Worker level과 level별 Review 승격 이력은 SQLite에 저장해 프로세스 중단 뒤에도 같은 값으로 재개해야 하며, 한 ExecPlan 안에서는 낮추지 않아야 한다. Review 실패 지문의 반복 이력은 level 승격으로 초기화하지 않아야 하며, R-008의 동일 지문 반복 `BLOCKED` 한도를 계속 적용해야 한다. 새 ExecPlan을 시작하거나 ExecPlan·근거 문서 변경으로 정상적으로 `BLOCKED`가 해제되면 effective level과 승격 이력을 최초 계산 level로 초기화해야 한다.

이 Requirement를 구현하는 ExecPlan은 `priority: critical`로 계획하고, R-008의 Review 실패 지문과 지속 재시도 상태 계약을 구현하는 G-013을 선행 조건으로 사용해야 한다. 선행 조건이 충족되면 다른 `high`, `normal`, `low` 우선순위 계획보다 먼저 선택돼야 한다.

## Acceptance Criteria

- complexity와 risk로 계산하는 최초 Worker level은 항상 1~7이며 level 8을 직접 반환하지 않는다.
- 같은 level에서 동일 Review 실패 지문이 Worker 수정 뒤 한 번 다시 확인되면 다음 Worker 호출의 level이 하나 올라간다.
- 같은 level에서 서로 다른 Review 실패 지문 세 개가 확인되면 다음 Worker 호출의 level이 하나 올라간다.
- level 1~6의 승격은 각각 다음 기존 level profile을 사용하고, 승격 전에는 현재 level을 유지한다.
- level 7에서 Review 승격 조건을 충족한 경우에만 level 8을 사용한다.
- level 8에서도 Review가 실패하면 level 8을 유지하며 더 높은 자동 profile을 선택하지 않는다.
- Codex level 8은 `gpt-6-astra,low`를 사용한다.
- Claude level 8은 Fable을 사용하지 않고 기존 Opus strong alias와 `max` effort를 사용한다.
- 전체 verify가 반복 실패하거나 Review 수정 뒤 verify가 실패해도 effective Worker level과 Review 승격 횟수는 바뀌지 않는다.
- Reviewer CLI 실패, timeout, 사용 한도 소진과 유효한 FAIL verdict가 없는 결과는 Review 승격 횟수에 포함하지 않는다.
- level 승격 뒤에도 기존 Review 실패 지문 반복 횟수를 유지해 같은 지문의 `BLOCKED` 한도를 우회하지 않는다.
- effective Worker level과 level별 Review 승격 이력은 프로세스 재실행과 Worker 세션 재개 뒤에도 보존된다.
- 하나의 ExecPlan에서는 effective Worker level이 단조 증가하며, 새 ExecPlan 시작과 정상적인 BLOCKED 해제에서 최초 계산 level로 초기화된다.
- `config/config.ini.example`과 운영 `config/config.ini`의 Codex·Claude Worker profile에 level 8 설정이 존재하고 시작 시 지원 Model/Effort 조합으로 검증된다.
- 초기 level, 동일·서로 다른 Review 지문에 따른 승격, verify 무시, level 7→8, level 8 상한, 재실행 보존과 초기화를 자동 테스트로 검증한다.
- 이 Requirement에서 생성되는 ExecPlan은 G-013에 의존하고 `priority: critical`을 사용한다.

## Product Impact

- 일반 작업은 비용이 예측 가능한 기존 Worker level 1~7만 사용한다.
- Review를 반복해서 통과하지 못하는 작업은 저장된 진행에 따라 더 강한 Worker profile로 단계적으로 승격된다.
- level 7에서도 해결하지 못한 예외적인 작업만 비용이 높은 level 8을 사용한다.
- verify 실패나 외부 실행 오류만으로 고비용 Model이 선택되지 않는다.
- Codex와 Claude는 각 provider에서 정의한 level 8 profile을 사용하되 동일한 Review 기반 승격 상태 계약을 따른다.

## Non-Goals

- Planner 또는 Reviewer Model을 동적으로 승격하지 않는다.
- verify 실패를 Worker level 승격 신호로 사용하지 않는다.
- level 8을 complexity, risk 또는 priority만으로 최초 선택하지 않는다.
- level 8보다 높은 자동 단계나 Astra의 medium·high·xhigh profile을 추가하지 않는다.
- Claude level 8에서 Fable로 Model을 변경하지 않는다.
- Review 실패 지문의 동일성 판단을 추가 AI 호출에 맡기지 않는다.
- 기존 R-008의 동일 실패 반복 `BLOCKED` 정책을 완화하거나 우회하지 않는다.

## Open Questions

- 없음
