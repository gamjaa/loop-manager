# [R-052] Worker 판정 기준 구체화와 최초 배정 완화

## Context

현재 Planner는 작업 범위와 구현 난이도로 `complexity`를, 실패 가능성과 변경 영향으로 `risk`를 판단한다. 구체적인 기준이 부족해 변경 파일 수, 핵심 경로 수정 또는 복구 코드라는 이유만으로 두 항목을 높게 평가하기 쉽다.

프로그램은 complexity를 low=1, medium=3, high=5로, risk를 low=0, medium=1, high=2, critical=3으로 환산하고 합계를 최대 7로 제한한다. 현재 제공되는 설정 예시에서 Codex level 5는 Sol medium, level 6은 Sol high, level 7은 Sol xhigh이므로 high/high 작업은 최초 호출부터 xhigh를 사용한다. 실제 운영 profile은 사용자가 지정한 설정에 따라 달라진다.

현재 Review 승격은 같은 level에서 동일 지문 두 번 또는 서로 다른 지문 세 개를 기준으로 한다. 첫 Review에서 서로 다른 issue 세 개가 발견돼도 승격할 수 있어, issue 수를 Worker의 반복 교정 실패와 구분할 필요가 있다.

Worker의 Model·Effort는 설정에서 읽지만 level 8에는 Codex의 Astra low와 Claude의 Opus 5 계열 max를 강제하는 코드 제한이 있다. 모든 Worker 단계의 profile을 사용자가 설정으로 결정할 수 있도록 이 제한을 제거하고, Worker 단계 설정 누락 시 기본값이나 다른 profile로 대신 실행하지 않는다.

일반적인 작업은 medium으로 시작해도 처리할 수 있다는 사용자 관찰을 바탕으로, 판정의 의미를 명확히 하고 위험도에 따른 최초 배정 상승을 줄인다. medium의 충분성이나 비용 절감률을 검증된 사실로 가정하지 않으며, 교정 후의 Review 반복과 실제 실행 기록으로 조정 결과를 확인한다.

## Requirement

### 복잡도와 위험도 판정

`complexity`는 현재 명세, 계획, 코드와 테스트를 확인한 뒤에도 **구현에 남아 있는 설계·추론 난이도**로 판단한다.

| complexity | 판정 기준 |
|---|---|
| low | 동작과 수정 위치가 명확하고 기존 패턴을 적용하는 국소 변경이다. 새로운 상태 전이나 설계 선택이 거의 없다. |
| medium | 여러 함수나 모듈을 바꾸거나 상태 전이·오류 처리를 추가하지만, 목표와 경계가 명확하고 기존 구조와 테스트를 바탕으로 구현 방향을 정할 수 있다. |
| high | 여러 책임이나 상태 전이 사이의 불변식, 중단 복구, 동시성 또는 기존 구조의 재설계를 함께 풀어야 하며, 계획 작성 후에도 중요한 설계 선택과 상호작용에 대한 추론이 남아 있다. |

변경 파일 수, 코드 길이, 예상 작업 시간, 핵심 모듈이라는 사실 또는 위험도만으로 complexity를 high로 정하지 않는다. 여러 파일에 걸친 기계적인 이동도 구현에 남은 판단이 적으면 low 또는 medium일 수 있다. 승인된 목표 안의 구현 추론과 사용자 판단이 필요한 정책·설계 모호함은 구분한다. 후자는 높은 effort로 대신 결정하지 않고 R-061의 draft·사용자 결정 절차를 따른다.

`risk`는 **잘못 구현했을 때 발생할 수 있는 구체적인 영향과 복구 가능성**으로 판단한다. 구현하기 어려워 보인다는 이유나 막연한 실패 가능성만으로 높이지 않는다.

| risk | 판정 기준 |
|---|---|
| low | 영향이 국소적이고 기존 동작이나 데이터에 미치는 영향이 작으며, 변경을 되돌려 쉽게 복구할 수 있다. |
| medium | 일부 사용자 동작이나 작업 진행에 오류가 생길 수 있지만 영향 범위가 제한되고 기존 재시도·rollback으로 복구할 수 있다. |
| high | 중요한 실행·권한·게시·저장 계약을 잘못 처리해 여러 작업이나 외부 상태에 영향을 줄 수 있고 수동 복구가 필요할 수 있으나, 구체적인 복구 수단이 있다. |
| critical | 실제 변경 경로에서 비가역적 데이터 손실, 비밀정보 노출, 소유권 경계를 넘는 쓰기 등 중대한 영향이 가능하고 복구가 불가능하거나 매우 어렵다. |

Git, SQLite, sandbox, 보안 또는 복구 코드를 수정한다는 사실만으로 risk를 high나 critical로 정하지 않는다. 실제로 잘못될 수 있는 동작, 영향을 받는 대상과 복구 수단을 확인한다. 단순 rollback으로 외부 부작용까지 되돌릴 수 있다고 가정하지 않는다.

Planner는 새 ExecPlan 또는 변경 권한이 있는 미착수 계획을 작성·개정할 때 기존 본문에 complexity와 risk의 짧은 근거를 남긴다. high complexity는 남은 설계 판단을, high·critical risk는 실패 영향과 복구 가능성을 구체적으로 설명해야 한다. complexity·risk 판정용 새 frontmatter 항목이나 별도 판정 Agent는 추가하지 않는다. R-061의 draft 준비도 선언과 기존 계획 변경 권한·유지보수 mode별 metadata 보존 조건은 유지한다. draft의 미결정 내용은 임의로 확정하지 않으며 작업 가능 전환 시 확정된 범위로 판정을 재확인한다.

### 최초 Worker level

complexity 점수 low=1, medium=3, high=5는 유지하고, risk 가산점은 **low=0, medium=0, high=0, critical=1**로 변경한다. 프로그램이 두 점수를 더해 최초 Worker level을 결정하며, Planner가 Model이나 effort를 직접 선택하지 않는다.

| complexity | risk low | risk medium | risk high | risk critical |
|---|---:|---:|---:|---:|
| low | 1 | 1 | 1 | 2 |
| medium | 3 | 3 | 3 | 4 |
| high | 5 | 5 | 5 | 6 |

최초 배정은 level 1~6만 사용한다. level 7과 8은 아래의 Review 교정 후 반복 기준에 따른 승격으로만 도달한다. priority, verify 실패, Worker 결과 형식 오류와 Reviewer 실행 오류는 최초 배정 상승이나 Review 승격의 추가 근거로 사용하지 않는다.

level을 정하는 규칙과 level별 Model·Effort 설정은 별개로 유지한다. 현재 제공되는 설정 예시의 값을 명시하면 high/high는 Codex Sol medium, high/critical은 Sol high로 시작하고 medium/high는 balanced medium으로 시작한다. 이는 설정값에 따른 결과이며 프로그램의 기본값이나 강제 profile이 아니다.

### Worker 단계별 Model·Effort의 명시 설정

Worker 단계별 Model·Effort의 유일한 선택 근거는 사용자가 지정한 `config.ini`다. 선택한 provider의 Worker level 1~8 profile을 모두 명시해야 한다. 최초 배정에서 도달하지 않는 level 7·8도 Worker 시작 전 필수 검증 대상에 포함한다. 사용하지 않는 provider의 설정까지 요구하지는 않는다.

각 Worker profile에는 Model alias와 Effort를 명시하고, 참조하는 alias에는 실제 Model 이름을 명시해야 한다. 코드, CLI의 기본 Model·Effort, 설정 예시 파일, 공통 기본 설정 또는 다른 profile의 값으로 Worker 단계 설정 누락을 보충하지 않는다. Worker section·level profile·참조 alias가 없거나 Model·Effort가 비어 있거나 형식이 잘못되면 오류 위치와 항목을 보고하고 Worker를 시작하지 않는다. 이 오류로 기존 작업물·session·실패 이력을 초기화하지 않는다.

level 8도 다른 level과 같은 설정·지원 조합 검증을 따른다. 특정 Model 이름·계열, alias 또는 Effort를 강제하는 특별 제한을 제거한다. Codex level 8을 Sol xhigh로 지정하거나 Claude level 8을 기존 Opus max 외의 지원 조합으로 지정해도 해당 CLI가 지원하면 사용할 수 있어야 한다. 여러 level에 같은 profile을 명시하는 것도 허용하며, level의 순서만으로 특정 Model이나 더 높은 Effort를 강제하지 않는다. 자동 승격의 level 8 상한과 최초 배정에서 level 8을 사용하지 않는 규칙은 유지한다.

CLI가 지원하는 Model·Effort 조합인지 확인하는 검증은 유지한다. 지원되지 않는 Worker 설정을 다른 profile로 자동 대체하거나 Effort를 자동 보정하지 않는다. `config.ini.example`의 Worker profile은 설정 작성 안내이며, 누락된 Worker 단계 값을 자동 보충하는 기본값으로 사용하지 않는다. 이미 모든 Worker 단계 profile이 명시된 사용자 설정은 변경 없이 사용할 수 있어야 한다.

구현 시 `config.ini.example`에도 새 최초 배정표와 Review 승격 흐름에 맞는 Worker level 1~8의 Model·Effort 예시를 반영한다. 구현 환경의 CLI가 지원하는 정확한 Model 이름과 Effort를 사용하고, 일반 구현은 medium으로 시작하며 반복 교정이 필요한 단계에서 높은 profile을 사용할 수 있는 예시를 제공한다. Worker가 참조하는 Model alias도 함께 명시한다. 아래 Planner 예시 갱신과 함께 반영하되 Reviewer의 실제 Model·Effort와 나머지 설정은 유지한다. 지원되는 CLI 환경에서는 예시를 운영 config.ini로 복사한 뒤 Worker 모델 설정을 추가로 채우지 않아도 사용할 수 있어야 한다.

설정한 실제 provider·Model·Effort를 Worker CLI 호출과 기존 호출 기록에 동일하게 적용한다. 설정 변경으로 실제 Model이 달라지거나 Effort만 달라진 경우에는 기존 Worker session 전이 정책을 따르며, profile 변경만으로 계획·작업물·검증 이력을 삭제하지 않는다.

### Planner의 명시 설정과 복사 가능한 예시

일반 Planner와 빠른 점검 Planner도 선택한 provider의 Model·Effort와 참조 Model alias를 config.ini에 각각 직접 지정해야 한다. 누락·빈 값·잘못된 형식이나 미지원 조합은 Planner 실행 전에 설정 오류로 보고하고, 코드·CLI 기본값 또는 다른 역할의 profile로 대신 실행하지 않는다. R-049의 Planner 역할 분리는 유지하며 빠른 점검 설정 생략 시 일반 Planner profile을 사용하는 fallback은 제거한다. 유지보수 Planner가 일반 Planner profile을 사용하는 기존 역할 연결은 유지하고, 별도의 유지보수 모델 설정 항목을 새로 요구하지 않는다.

config.ini.example에는 Planner 두 역할과 Worker level 1~8, 각 profile이 참조하는 Model alias를 모두 실제 값으로 채운다. 구현 환경의 CLI가 지원하는 조합인지 확인하고, 해당 환경에서는 예시를 그대로 config.ini로 복사해 모델 설정 보완 없이 사용할 수 있어야 한다. 기존의 CLI 설치·인증과 프로젝트 등록 절차는 유지하며, Telegram 설정 등 모델 외의 항목에 새로운 필수값이나 기본값 금지를 추가하지 않는다. 예시 복사는 사용자가 설정을 명시하는 행위이며 프로그램이 설정 누락을 자동 보충하는 fallback은 아니다.

Planner 예시는 다음 구성을 기준으로 한다. 모델 이름과 Effort는 코드의 선택 규칙으로 고정하지 않으며, 사용자가 같은 모델이나 다른 지원 조합으로 변경할 수 있어야 한다.

| provider | 역할 | Model | Effort |
|---|---|---|---|
| Codex | 일반 Planner | gpt-6.1-sol | high |
| Codex | 빠른 점검 Planner | gpt-6-luna | high |
| Claude | 일반 Planner | claude-opus-5-5 | medium |
| Claude | 빠른 점검 Planner | claude-sonnet-5-5 | medium |

Claude Worker 예시는 단순 구현에 Sonnet 5.5를, 복합 구현에 Opus 5.5를 사용하며 다음 단계표를 기준으로 한다. high/high는 Opus 5.5 medium, high/critical은 Opus 5.5 high로 시작하고, level 7·8은 Review 승격으로만 사용한다. 반복 교정에서 medium의 충분성을 실제 실행 기록으로 확인하며 모든 작업을 해결할 수 있다고 가정하지 않는다.

| Worker level | Model | Effort |
|---|---|---|
| 1 | claude-sonnet-5-5 | low |
| 2 | claude-sonnet-5-5 | medium |
| 3 | claude-sonnet-5-5 | medium |
| 4 | claude-sonnet-5-5 | high |
| 5 | claude-opus-5-5 | medium |
| 6 | claude-opus-5-5 | high |
| 7 | claude-opus-5-5 | xhigh |
| 8 | claude-opus-5-5 | max |

2026-10-03 기준 [Anthropic의 Opus 5.5 가이드](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5#calibrate-effort)는 medium에서 시작하고 xhigh·max의 품질 이득을 확인하도록 권한다. [Claude Code 모델 설정 문서](https://code.claude.com/docs/en/model-config#adjust-effort-level)에서 두 모델의 low·medium·high·xhigh·max 지원을 확인했다. 이 문서 기준으로 Opus 5.5는 Claude Code v2.1.280 이상, Sonnet 5.5는 v2.1.284 이상이 필요하다. 구현 시 실제 CLI 지원을 다시 확인하고 예시 주석에 필요한 버전을 안내한다. Reviewer 설정은 Planner·Worker용 Model alias 갱신의 영향을 받지 않도록 유지한다.

### 위험도에 맞는 검증과 Review

risk는 Worker effort와 별개로 검증 시나리오와 Review의 확인 항목에 반영한다. Planner는 실제 영향이 있는 실패·경계·복구 시나리오를 ExecPlan의 Acceptance Criteria와 Validation에 명시한다. high·critical 작업은 변경에 관련된 데이터·권한·소유권 보존, 중단·재시작, 외부 작업 실패와 복구 조건을 필요한 범위에서 포함하고, Reviewer는 해당 조건의 구현과 검증 누락을 확인한다.

위험도 때문에 모든 시나리오를 기계적으로 추가하거나 테스트를 중복 실행하지 않는다. Worker의 targeted unit test, Manager의 전체 verify, Reviewer의 읽기 전용 검토 및 사용자 승인·Merge 경계를 유지한다. 낮은 최초 effort는 검증 생략이나 승인 조건 완화의 근거가 아니다.

### Review 교정 후 반복에 따른 승격

각 effective level의 첫 유효한 Review는 문제를 식별하는 기준 관측으로 사용하고 교정 결과 횟수에 넣지 않는다. 이후 해당 level에서 Review 피드백에 대한 Worker 교정 시도가 완료되고 전체 verify를 통과한 뒤 얻은 유효한 Review 결과만 집계한다.

| 반복 패턴 | 승격 조건 |
|---|---|
| 같은 문제를 해결하지 못함 | 최초 지적 이후 같은 level에서 두 번 교정했는데도 동일 ISSUE_ID가 두 교정 결과에 재등장하면 다음 Worker 호출부터 한 단계 올린다. 최초 지적을 포함하면 해당 issue가 Review에서 세 번 확인된 경우다. |
| 교정마다 다른 결함이나 회귀가 나타남 | 같은 level에서 교정 결과가 세 번 연속 Review FAIL이면 다음 Worker 호출부터 한 단계 올린다. ISSUE_ID가 달라져도 교정 결과의 실패 횟수를 센다. |
| 요구사항이 모호하거나 서로 충돌함 | 기존 명세·사용자 결정 절차로 처리하며 모델 승격으로 대신 해결하지 않는다. |

한 교정 결과의 Review에 issue가 여러 개 있어도 교정 실패는 한 번으로 센다. 동일 issue는 기존 ISSUE_ID로 식별하며 설명·줄 번호가 바뀌어도 같은 문제는 같은 ID를 사용한다. 별도 패턴 분류 Agent를 추가하거나 서로 다른 issue를 추측으로 같은 문제에 합치지 않는다.

기준 관측과 두 번의 교정 결과에서 같은 issue가 계속 남은 경우 또는 세 번의 교정 결과가 연속 FAIL인 경우에만 승격한다. 첫 Review의 issue 개수, 교정 없이 반복한 Review, CLI·Reviewer 프로토콜 재시도와 중단된 동일 Worker turn의 재개는 교정 횟수를 늘리지 않는다. verify 실패, Worker 결과 형식 오류, Reviewer 실행 오류와 유효하지 않은 Review 결과는 승격 집계에 포함하지 않고 기존 집계도 초기화하지 않는다.

승격은 한 번에 한 단계만 수행하고 level 8을 상한으로 한다. 두 조건이 함께 충족돼도 두 단계 올리지 않는다. 새 level에서는 첫 유효한 Review를 기준 관측으로 다시 집계하며, level 8에서도 Worker 교정과 기존 실행 제한을 유지한다. 유효한 Review PASS는 진행 중인 반복 집계를 끝내되 effective level과 과거 승격·실패 감사 기록을 보존한다.

집계는 같은 ExecPlan과 effective level에 결합해 외부 실행 종료·중단·재시작 뒤에도 보존한다. 한 외부 실행에서 Review 교정을 최대 세 번 시작한 뒤 필요하면 다음 실행으로 이어가는 기존 한도와 정상적인 BLOCKED 해제의 초기화 규칙은 유지한다. 승격 집계 때문에 별도의 최종 차단을 추가하지 않으며, 전체 verify의 지문 반복 복구·차단 정책과 명세·권한·소유권 차단은 독립적으로 유지한다.

### 기존 계획과 저장 상태

새 계획과 실행 이력·작업 공간 소유권이 없는 미착수 READY에는 새 산식을 적용한다. 기존 계획의 complexity·risk를 일괄 낮추거나 계획 본문을 배정 변경만을 위해 자동 개정하지 않는다.

산식 변경 시 저장된 최초 level은 현재 선언과 새 산식에 맞게 재계산해야 한다. 실행 이력이 있는 계획의 effective level, Review 승격·실패 이력, session, branch, worktree와 PR 연결은 보존하며 effective level을 자동으로 낮추지 않는다. 산식만 달라졌다는 이유로 저장 상태 불일치 오류를 발생시키거나 BLOCKED를 해제하지 않는다. 기존의 정상적인 BLOCKED 해제에서만 새 산식으로 최초·effective level과 승격 이력을 초기화한다.

과거 issue 개수나 지문 발생 횟수만으로 새 정책의 교정 횟수를 추정하지 않는다. 같은 level의 Worker 교정과 Review 결과 관계가 확인되는 기록만 새 집계에 사용할 수 있으며, 확인할 수 없으면 다음 유효한 Review부터 기준 관측을 시작한다. 이미 적용한 effective level과 과거 기록은 이 전환으로 낮추거나 지우지 않는다.

적용은 실행 중인 Agent를 선점하지 않는 기존의 안전한 경계에서 이루어진다. 원격 선언과 SQLite 상태·소유권 검증 및 중단·재시작 복구 조건을 유지하며, 이미 종료된 계획과 과거 호출 기록을 새 정책의 실행 결과로 다시 쓰지 않는다. profile이 같으면 기존 session을 재개하고 effort 또는 Model이 달라지면 기존 session 전이 정책을 따른다.

### 실행 결과 확인

기존 호출 기록, verify·Review 결과와 승격 이력으로 최초 profile, 수정·승격 여부, 소요 시간과 제공되는 token usage를 확인할 수 있어야 한다. 최초부터 높은 profile을 쓴 실행과 반복 Review 실패 뒤 승격한 실행을 구분하고, 계획의 complexity·risk 및 실제 설정을 함께 확인한다. 누락 usage는 알 수 없음으로 유지한다.

운영 결과를 바탕으로 판정 기준, 최초 배정 규칙과 단계별 profile 중 무엇을 조정할지 각각 판단할 수 있어야 한다. 실행 시간이 짧거나 토큰이 적다는 이유만으로 품질 개선을 확정하지 않으며, 자동 profile 재조정이나 별도 평가 시스템은 추가하지 않는다.

## Acceptance Criteria

- Planner의 complexity·risk 안내에 위 정의와 등급별 기준이 적용되고, 새 계획 및 권한이 있는 READY 개정의 본문에서 두 판정의 구체적인 근거를 확인할 수 있다.
- 구현 방향이 명확한 여러 파일의 기계적인 이동이나 기존 패턴을 따르는 오류 처리 추가는 파일 수·핵심 경로·risk만으로 complexity high를 요구받지 않는다. 실제로 남은 복합 설계 판단이 있는 작업은 그 근거와 함께 high로 판정할 수 있다.
- risk high·critical의 근거에 실제 실패 영향과 복구 가능성이 드러난다. 외부 부작용이나 비밀 노출을 단순 코드 rollback으로 복구 가능한 것으로 취급하지 않는다.
- complexity·risk의 12개 조합이 최초 배정표와 일치한다. high/high는 level 5, high/critical은 level 6이며 최초 계산에서 level 7·8을 반환하지 않는다. 설정 예시의 profile을 명시한 Codex에서는 각각 Sol medium·high를 선택하며, profile을 바꾸면 해당 설정이 적용된다.
- 같은 metadata는 Codex·Claude에서 같은 최초 level을 계산하되 실제 Model·Effort는 각 provider의 설정을 따른다. 기존 사용자 설정과 지원 조합 검증이 유지된다.
- 선택한 provider의 Worker level 1~8을 Worker 시작 전에 검증한다. Worker section·level profile·참조 alias의 누락, 빈 Model·Effort 및 잘못된 형식은 구체적인 설정 오류가 되며 Worker를 시작하지 않는다. 다른 provider의 미사용 설정은 요구하지 않는다.
- Worker 단계 설정 누락을 코드·CLI·설정 예시·공통 기본값 또는 다른 profile의 값으로 채우지 않는다. 아래에서 변경하는 Planner 직접 지정 정책 외에 Reviewer 설정과 그 밖의 설정 기본값은 유지된다.
- Codex level 8에 Sol xhigh 등 Astra low 외의 지원 조합을, Claude level 8에 Opus max 외의 지원 조합을 명시하면 특별 Model·계열·alias·Effort 제한 없이 해당 profile을 사용한다. 서로 다른 level에 같은 profile을 명시해도 허용한다.
- 지원되지 않는 Worker Model·Effort는 Worker 시작 전에 거부하고 자동 대체·보정하지 않는다. Worker 단계 설정이 완전한 기존 config.ini는 사용할 수 있으며, 설정 예시나 검증 절차가 Worker profile을 자동 생성·보충·덮어쓰지 않는다. 실제 Worker CLI 호출과 호출 기록의 Model·Effort는 설정과 일치한다.
- 구현 결과의 config.ini.example에 Codex·Claude Worker level 1~8과 참조 Model alias가 모두 명시되고, CLI가 지원하는 조합으로 새 최초 배정·Review 승격 흐름을 설정할 수 있다. 일반 구현의 medium 시작 예시와 승격 단계의 profile을 확인할 수 있으며, 예시 변경으로 Reviewer의 실제 Model·Effort나 나머지 설정이 바뀌지 않는다. 예시는 운영 설정 누락의 자동 대체값으로 사용되지 않는다.
- 일반 Planner·빠른 점검 Planner의 profile과 참조 alias를 선택한 provider의 config.ini에 각각 명시해야 한다. 어느 역할이든 Model·Effort의 누락·빈 값·잘못된 형식이나 미지원 조합은 Planner 실행 전 설정 오류가 되고 다른 역할이나 코드·CLI·예시 값으로 대체되지 않는다. 두 역할의 호출과 인계에는 각각 명시한 Model·Effort가 적용된다.
- config.ini.example에는 위 Planner 두 역할의 실제 Model·Effort와 참조 alias가 포함되고, Worker 단계 설정까지 완전한 예시를 해당 조합을 지원하는 CLI 환경에서 그대로 복사해 사용할 수 있다. Claude는 위 Worker level 1~8의 Sonnet 5.5·Opus 5.5 조합과 필요한 CLI 버전 안내를 포함한다. Reviewer와 모델 외 설정의 기존 동작 및 유지보수 Planner의 기존 profile 연결은 유지된다.
- high·critical 계획의 실제 위험 시나리오가 Acceptance Criteria·Validation과 Review에서 확인된다. 낮은 effort에서도 targeted test·전체 verify·Review·사용자 승인 조건은 약화되지 않는다.
- 새 계획과 무소유·미착수 READY의 최초·effective level은 새 산식을 따른다. 실행 이력이 있는 계획은 최초 level cache가 새 산식과 일치하면서 기존 effective level·session·작업물·승격 및 실패 이력을 보존한다.
- 정책 적용 전후와 적용 중 중단·재시작에서 저장된 level 불일치나 실행 이력 손실이 발생하지 않는다. 산식 변경만으로 기존 차단을 해제하지 않으며, 정상적인 BLOCKED 해제는 새 최초 level로 초기화한다.
- 첫 Review에 서로 다른 issue 세 개 이상이 있어도 승격하지 않는다. 최초 관측 뒤 한 번 교정한 결과에 같은 ISSUE_ID가 남으면 level을 유지하고, 두 번째 교정 결과에도 남으면 다음 Worker 호출부터 한 단계 올린다.
- 같은 level에서 첫 기준 관측 이후 교정 결과가 두 번 연속 FAIL이면 level을 유지하고, 세 번째도 FAIL이면 ISSUE_ID가 달라도 한 단계 올린다. 복수 issue를 포함한 한 Review는 한 교정 결과로 집계한다.
- 교정 없이 반복한 Review, 같은 turn의 CLI 재시도·중단 복구와 Reviewer 프로토콜 재시도는 중복 집계하지 않는다. 중간 verify 실패나 외부 실행의 일시 중단·재시작은 Review 승격 집계를 늘리거나 초기화하지 않는다.
- 승격 조건이 동시에 충족돼도 한 단계만 올리고 새 level에서 다시 집계한다. level 8을 넘지 않으며, Review PASS와 정상적인 BLOCKED 해제는 정해진 집계를 종료·초기화한다. Review 횟수만으로 최종 차단하지 않고 기존 verify·명세·권한·소유권 차단과 외부 실행당 교정 한도를 유지한다.
- 요구사항 모호성·충돌, verify·Worker 형식·Reviewer 실행 오류와 유효하지 않은 Review 결과는 승격의 근거가 아니다. 과거 issue 개수만으로 새 교정 횟수를 추정하거나 정책 전환으로 기존 effective level과 감사 기록을 낮추거나 지우지 않는다.
- 기존 기록으로 최초 배정과 Review 승격 후 profile, 수정 횟수, 소요 시간과 확인 가능한 usage를 구분할 수 있다. 기록이 없는 값이나 아직 측정하지 않은 품질·절감 효과를 성공으로 표시하지 않는다.

## Product Impact

- 복잡도는 남은 설계·추론 난이도로, 위험도는 실패 영향과 복구 가능성으로 분리해 판단한다.
- 최초 Worker 배정은 complexity 점수에 critical risk만 1점을 가산하며, 높은 단계는 실제 Review 실패에 따라 사용한다.
- Review 승격은 issue 개수 대신 같은 문제의 두 번 교정 후 재등장 또는 교정 결과 세 번 연속 FAIL을 기준으로 한 단계씩 적용한다.
- Worker level 1~8의 Model·Effort는 config.ini에 명시해야 하며, level 8에도 코드에 고정된 profile이나 Worker 단계 설정 누락 fallback을 두지 않는다.
- 일반 Planner·빠른 점검 Planner도 Model·Effort를 직접 지정하고, config.ini.example은 Planner와 Worker 모델 설정을 모두 채워 복사해 사용할 수 있는 예시를 제공한다. Claude는 일반 Planner와 복합 구현을 Opus 5.5 medium에서 시작하는 예시를 사용한다.
- 위험도가 높은 작업은 관련 검증·Review 조건으로 다루고, 실행 이력이 있는 계획의 effective level과 복구 상태는 보존한다.

## Non-Goals

- 기존 계획의 high 판정이나 실행 중인 effective level을 일괄 낮추지 않는다.
- 모든 작업을 Sol medium으로 통일하거나 특정 Model 버전과 절감률을 강제하지 않는다.
- 사용자의 기존 profile 값을 자동으로 변경하거나 level 8 상한을 변경하지 않는다.
- Reviewer 설정과 그 밖의 설정 기본값을 변경하지 않는다. Planner 역할의 책임·권한을 바꾸거나 유지보수 Planner에 별도 Model·Effort 설정 항목을 추가하지 않는다.
- 별도 판정 Agent, 자동 튜닝, benchmark 서비스나 새로운 infrastructure를 추가하지 않는다.
- 검증·권한·사용자 승인 조건이나 Planner의 기존 계획 변경 권한을 완화하지 않는다.

## Open Questions

- 없음
