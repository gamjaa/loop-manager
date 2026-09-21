# [R-018] Cache-Efficient Worker Session Continuity

## Context

Loop Manager는 전체 verify 또는 Review 실패를 수정할 때 같은 Worker 세션을 재사용한다. 같은 세션은 Worker가 이미 확인한 코드 구조, 구현 결정, 실패한 시도와 현재 worktree의 진행 맥락을 유지하고, 변경되지 않은 대화 prefix의 prompt cache 재사용 가능성을 높인다. 실제 Agent 사용량에서도 전체 input token 중 cached input이 큰 비중을 차지할 수 있으므로, 실패할 때마다 새 Worker 세션을 만드는 것은 진행 맥락과 캐시 이점을 불필요하게 잃을 수 있다.

반면 현재 재개 prompt는 같은 verify 또는 Review 출력을 `last_error`와 저장된 validation feedback으로 중복 전달할 수 있다. 동일한 텍스트를 새 메시지 끝에 다시 추가하면 앞선 세션에 이미 존재하더라도 새로운 suffix가 되어 컨텍스트를 늘린다. 전체 원문 로그를 반복 전달하면 같은 세션의 이점을 유지하면서도 입력, latency와 context window 사용을 불필요하게 증가시킬 수 있다.

Review 기반 Worker level 승격이나 운영 설정 변경으로 effort만 바뀌는 경우에는 기존 세션의 진행 맥락이 여전히 유효하다. 그러나 실제 provider 또는 Model이 바뀌면 이전 Model의 prompt cache 재사용을 신뢰할 수 없고, 다른 Model로 저장된 세션을 재개할 때 호환성 경고나 예측하기 어려운 동작이 발생할 수 있다. 이 경우에는 현재 worktree와 저장된 검증 상태를 원본으로 새 Worker 세션을 시작하는 편이 명확하다.

현재 Agent 로그에는 provider가 보고한 token usage가 포함될 수 있지만 Loop Manager는 역할, profile, 세션 전이와 검증 결과에 연결된 사용량을 구조적으로 저장하지 않는다. 따라서 같은 세션 재사용, delta feedback과 Model 변경 시 세션 교체가 실제 비용과 latency에 미치는 효과를 비교하기 어렵다.

## Requirement

Loop Manager는 Worker 수정 재시도에서 실제 provider와 Model이 이전 Worker 세션에 결합된 값과 같으면 그 세션을 계속 재사용해야 한다. 같은 Model에서 effort만 변경된 경우에도 선택된 새 effort를 적용해 기존 세션을 재개하고, 세션 진행 맥락과 prompt cache 재사용 가능성을 보존해야 한다. 같은 세션이나 effort 유지가 실제 cache hit를 보장한다고 가정하지 않아야 한다.

실제 provider 또는 Model 식별자가 이전 Worker 세션과 달라지면 기존 세션을 새 Model로 재개하지 않아야 한다. 현재 worktree의 구현 결과는 보존한 채 선택된 새 profile로 새로운 Worker 세션을 시작해야 한다. 새 세션에는 AGENTS, 선택된 ExecPlan과 현재 worktree를 다시 확인하라는 지시, 이전 Worker의 최종 결과, 아직 해결되지 않은 verify·Review 실패의 안정적인 식별자와 제한된 최신 진단을 포함하는 결정적 recovery context를 전달해야 한다. 전체 이전 대화, 전체 verify 원문 또는 전체 Review 원문을 recovery context에 복제하지 않아야 한다.

세션에 전달하는 검증 피드백은 저장용 원문과 분리해야 한다. 전체 verify와 Review 결과는 기존 복구·감사 및 실패 지문 계산에 필요한 원문으로 보존하되, 같은 Worker 세션에는 직전 성공한 Worker turn 이후 새로 발생하거나 변경된 actionable feedback만 delta로 전달해야 한다. 하나의 실패 내용을 `last_error`와 별도 validation feedback에 중복 포함하지 않아야 한다. 이미 전달한 동일 실패 지문이나 Review `ISSUE_ID`가 다시 발생하면 전체 설명을 반복하지 않고 식별자, 발생 횟수와 이전 전달 이후 달라진 제한된 진단만 전달해야 한다.

프로세스 중단이나 Agent 응답 경계의 불확실성 때문에 같은 delta를 재전달할 가능성이 있으면 각 feedback 항목에 안정적인 식별자와 revision 또는 occurrence를 포함해 Worker가 같은 의미의 피드백을 새 issue로 해석하지 않게 해야 한다. 저장된 세션을 사용할 수 없어 기존 fallback 정책에 따라 새 세션을 시작하는 경우에도 Model 변경과 같은 recovery context 계약을 사용해야 한다.

Loop Manager는 Planner, Worker와 Reviewer의 각 Agent 호출에 대해 실제로 선택한 provider, Model과 effort, 역할, 관련 ExecPlan, 세션 시작·재개·교체·fallback 구분과 원인, 시작·종료 시각, 소요 시간, 결과 분류 및 provider가 보고한 token usage를 구조적으로 기록해야 한다. 사용량에는 제공되는 범위에서 input, cached input, cache write input, output과 reasoning output을 구분해야 한다. provider가 일부 사용량을 보고하지 않거나 로그 형식이 유효하지 않아도 Agent 결과와 제품 상태 전이를 실패시키지 않고 해당 값을 알 수 없음으로 기록해야 한다.

사용량 기록은 같은 세션 재사용과 Model 변경 전후의 cache 비율, uncached input, 출력량, latency와 재시도 횟수를 비교할 수 있어야 한다. cached input은 일반 input과 동일한 비용이라고 가정하거나 무료라고 가정하지 않고 provider가 보고한 분류를 그대로 보존해야 한다. 사용량 수집과 recovery context에는 Agent 자격 증명, Telegram token 또는 그 밖의 비밀값을 포함하지 않아야 한다.

## Acceptance Criteria

- 이전 Worker 세션에 결합된 provider와 Model이 다음 Worker 호출의 실제 선택값과 같으면 기존 Worker 세션 ID를 사용해 재개한다.
- 같은 provider와 Model에서 effort만 변경되면 새 effort를 적용하되 새 Worker 세션을 만들지 않는다.
- effort가 변경되지 않은 일반 verify·Review 수정도 기존 Worker 세션을 재사용한다.
- 실제 provider 또는 Model 식별자가 달라지면 이전 Worker 세션 ID로 재개하지 않고 선택된 새 profile로 새 Worker 세션을 시작한다.
- Model 변경으로 새 세션 시작이 실패하거나 프로세스가 중단돼도 현재 worktree와 검증 상태를 잃지 않으며, 다음 실행은 이전 Model 세션으로 되돌아가지 않고 선택된 새 Model의 세션 시작 또는 저장된 새 세션 재개를 계속한다.
- 저장된 세션을 사용할 수 없어 fallback 새 세션을 시작할 때도 현재 worktree, 선택된 ExecPlan, 이전 Worker 결과와 미해결 feedback을 포함한 동일한 recovery context 계약을 사용한다.
- 새 Worker 세션의 recovery context에는 전체 이전 Agent 대화와 전체 verify·Review 원문을 포함하지 않는다.
- 전체 verify와 Review 원문은 기존 실패 지문, 복구와 감사에 필요한 저장 상태로 유지한다.
- 같은 Worker 세션에는 직전 성공한 Worker turn 이후 새로 발생하거나 변경된 verify·Review feedback만 전달한다.
- 같은 실패 내용을 `last_error`와 별도 validation feedback에 동시에 넣지 않는다.
- 처음 전달하는 verify 실패는 실패 지문과 Worker가 수정할 수 있는 제한된 진단을 포함한다.
- 처음 전달하는 Review 실패는 각 `ISSUE_ID`와 해당 actionable 설명을 포함한다.
- 이미 전달한 동일 실패가 다시 발생하면 전체 원문을 반복하지 않고 안정적인 식별자, occurrence와 이전 전달 이후 변경된 제한된 진단만 전달한다.
- feedback delta가 중단 복구 과정에서 재전달돼도 안정적인 식별자와 revision 또는 occurrence로 같은 issue임을 구분할 수 있다.
- 성공한 전체 verify 출력은 기존과 같이 Worker에게 전달하지 않는다.
- Planner, Worker와 Reviewer 호출마다 역할, provider, Model, effort, 관련 ExecPlan, 세션 전이 종류와 원인, 시각, 소요 시간 및 결과 분류를 구조적으로 기록한다.
- provider가 제공하는 input, cached input, cache write input, output과 reasoning output 사용량을 각 호출에 연결해 저장하며 제공되지 않은 값은 알 수 없음으로 구분한다.
- 기록된 값으로 역할·profile·세션 전이별 cache 비율, uncached input, latency와 재시도 횟수를 비교할 수 있다.
- 사용량 필드가 없거나 형식이 유효하지 않아도 성공한 Agent 호출을 실패로 바꾸거나 ExecPlan 상태 전이를 차단하지 않는다.
- Model 동일·effort 변경 재개, Model 변경 세션 교체, 새 세션 시작 중단 복구, session-unavailable fallback, 신규·반복 feedback delta, 중복 feedback 제거와 부분·누락 usage를 자동 테스트로 검증한다.

## Product Impact

- 같은 Model의 Worker 수정은 effort가 바뀌어도 기존 세션의 진행 맥락을 이어간다.
- Worker Model이 바뀌면 이전 Model 세션을 재개하지 않고 현재 작업 상태를 이용해 새 세션에서 계속한다.
- 전체 검증·Review 원문은 보존하지만 Worker에는 새롭거나 변경된 actionable feedback만 전달한다.
- 역할과 profile별 Agent 사용량 및 세션 전이를 측정해 캐시 효과와 재시도 비용을 운영 데이터로 비교할 수 있다.

## Non-Goals

- 동일 Model 세션의 실제 prompt cache hit를 보장하지 않는다.
- input token 수나 context 크기만으로 정상적인 Worker 세션을 자동 폐기하거나 compaction하지 않는다.
- Model이 동일한데 effort만 바뀐 경우 새 Worker 세션을 만들지 않는다.
- 비용 또는 cache hit 판단을 위해 추가 AI Agent나 외부 서비스를 호출하지 않는다.
- Planner와 Reviewer의 기존 책임, Review 실행 조건 또는 Worker level 승격 조건을 변경하지 않는다.
- 전체 verify·Review 원문, 실패 지문 또는 기존 복구 상태를 삭제하거나 축소하지 않는다.
- provider별 가격을 Loop Manager에 내장하거나 token 사용량만으로 실제 청구 비용을 단정하지 않는다.

## Open Questions

- 없음
