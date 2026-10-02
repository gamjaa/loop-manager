---
id: G-121
title: Extract Agent Invocation and Scope Services
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-120
complexity: high
risk: high
priority: critical
---

## Purpose

`loop_manager.py`에는 provider profile, Codex/Claude CLI, retry·usage·session, 역할 prompt와 Agent scope 검증·저장이 함께 있다. 기존 `agent_invocation`, `worker_session`, scope decision·evidence를 활용해 외부 호출과 순수한 판정을 명시적인 service/port로 분리한다.

## Acceptance Criteria

- provider별 command 생성·출력 해석·process 실행은 Agent adapter, profile/session/retry·usage 기록은 기존 담당 경계, 역할 prompt는 필요한 입력만 받는 builder로 분리한다.
- scope 시작·snapshot 수집·결과 검증·저장된 baseline 복원은 판단·증거 수집·repository의 책임을 나눈다. 관련 module에 `loop_manager` 전체를 넘기거나 임의 attribute 조회를 위임하지 않는다.
- Codex 직접 호출, 설정 경계·workspace 격리·granular 자동 승인 검토, Reviewer read-only, session binding·retry와 사용량 감사를 유지한다. 과거의 바깥 sandbox 등 현재 제품 동작과 다른 설계를 다시 도입하지 않는다.
- 같은 계약의 command/log 해석, timeout·usage 분류, path/identity 확인과 scope 증거 해석의 중복을 제거한다. provider별 실패·재개 조건과 신규/저장 scope의 적용 시점 차이를 유지한다.
- provider 선택, model/effort, prompt 권한, session과 실제 외부 command·timeout·종료 분류의 관측 결과가 분리 전과 같다. 새로운 Agent 권한이나 외부 의존성을 추가하지 않는다.

## Implementation

1. `run_codex`, `run_claude`, `run_agent`, 역할 prompt와 scope 관련 함수의 호출을 G-060/G-119 대응표에서 추적한다. G-120의 leaf identity와 기존 immutable profile/session 자료형을 재사용한다.
2. provider adapter, invocation service, 역할 prompt와 scope 관측/검증으로 분리하고 순수 판정은 기존 decision module에 연결한다. 필요한 repository와 callable만 typed port로 주입한다.
3. prompt·CLI 인자·형식·예외·로그를 유지하며 내부 소비자를 새 service로 전환한다. scope의 Git 원시 작업은 기존 adapter를 이용하고 Git mutation·rebase 통합은 G-122에서 수행한다.
4. G-119에서 나눈 scope/provider case와 `tests/test_agent_invocation.py`를 계약·session/retry·provider 격리별로 정리한다. profile이나 결과를 일률적인 fake로 바꿔 실제 경계를 검증하지 못하게 하지 않는다.
5. facade 호환에 필요한 export만 root에 남긴다. 새로운 callable registry·module proxy 또는 전역의 가변 service singleton을 만들지 않는다.

## Validation

- Worker는 수정한 provider command/result parser, profile/session 전이와 scope decision·증거 adapter의 targeted unit test만 실행한다. 외부 Agent를 실제로 시작하지 않는다.
- 정상 완료·일시 오류·usage pause·timeout·session unavailable, provider/model 변경과 effort 변경의 차이 및 재개 시 감사 기록을 검증한다.
- 정상 Manager baseline 변경과 scope 위반을 구분하고 worktree·ref·Git config·index flag 보호, 증거 누락 거부와 Reviewer 권한을 유지한다.
- Manager 전체 verify에서 provider 호환·설정·sandbox·scope·실행 재개와 prompt 권한의 기존 case가 유지돼야 한다.

## Progress

- [x] Agent/profile/prompt/scope 소비 관계와 중복 확인
- [x] 책임별 service·port 추출과 직접 소비자 전환
- [x] 관련 targeted unit test와 provider test 정리
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: 기존 Agent module을 늘리는 데 그치지 않고 root의 실제 구현과 호출자를 새 경계로 옮긴다. 신규·저장·차단 scope는 증거 적용 시점이 달라 같은 계약의 부분만 공유한다.
- 현재 Merge된 PRODUCT와 구현을 유지한다. 미구현 R-049의 신규 Planner 역할이나 model 선택은 이번 리팩터링에 포함하지 않는다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.

- 2026-10-02 구현: G-060의 root inventory와 G-119/G-120의 소비자·fixture 대응표를 재사용했다. 새 root 항목은 없으며 launcher, resource `ROOT`, 외부 config, 공통 문서와 verify 경로를 유지한다. 이동 대상은 기존 내부 패키지의 Agent 책임뿐이다.

| 기존 구현과 소비자 | 구현 경계 |
| --- | --- |
| `run_codex`/`run_claude`, provider 선택, execution·planning·document Agent 호출 | `agents/provider_adapters.py`, `invocation_service.py`; process launch와 handoff-lock/ledger start는 명시적인 callable port다. 전역 adapter registry를 제거하고 호출마다 adapter를 구성한다. |
| model catalog·auth·profile 해석, Worker session 및 retry 감사 | `agents/agent_profiles.py`, 기존 `agent_invocation.py`·`execution/worker_session.py`·`storage/agent_invocation_state.py`; resolved profile과 물리적 attempt별 session/usage를 보존한다. |
| 일반/유지보수 Planner, Worker, Reviewer prompt | `agents/agent_prompts.py`; 기존 네 builder의 AST가 동일하며 현재 입력만 받는다. R-049 역할 확장은 포함하지 않는다. |
| Git snapshot·path/index/config/operation evidence와 역할 판정 | `agents/scope_contracts.py`, `scope_observation.py`, `scope_validation.py`, 기존 `agent_scope_decision.py`; G-120 canonical identity를 재사용하고 immutable snapshot/오류 계약을 그대로 export한다. |
| 신규/재개 scope, local tree capture·restore, 저장/차단 scope 비교 | `agents/scope_lifecycle.py`, `scope_baseline.py`, `scope_recovery.py`; Git 효과는 기존 adapter에, owner·publication 조회는 기존 runtime/checkpoint repository에 맡긴다. 저장 helper는 schema를 초기화하거나 commit하지 않는다. |

- log directory, stream JSON decode, timeout/process 종료와 retry/result 분류는 같은 계약 안에서 공유한다. Claude의 malformed-text 진단과 scalar JSON 처리 차이, Codex 신규/재개 command, native preflight, Reviewer read-only, granular auto-review, 원래 Agent snapshot과 Manager checkpoint의 적용 시점을 보존했다. scope 관련 module에 진입점 전체나 module proxy를 전달하지 않으며 Git mutation·rebase adapter 재설계는 G-122에 남긴다.
- invocation 테스트 60개를 audit/retry, provider usage, write policy, native Codex 호출과 runtime read-only 파일로 나눠 기존 case 집합이 동일함을 확인했다. native/runtime 공유 boundary는 기존 `tests/support/workspace.py`에 둔다. snapshot reader·lock mock은 실제 소비 module에 연결했으며 assert와 검증 조건은 유지한다. 두 verify script에는 새 Agent module의 compile 대상만 추가했다.
- targeted unit test 132개 PASS: scope/ledger/session/usage 46개, profile/provider/scope decision·policy와 prompt/export/verify 목록 72개, 독립 import 및 기존 합성 Manager baseline·rebase observation 6개, mocked native 호출/handoff와 runtime read-only 8개. 실제 외부 Agent, 전체 suite·lint·build·type check 및 verify는 실행하지 않았다. Manager verify, 독립 Review와 사용자 Merge는 대기 중이다.
- 최초 scope fixture에 관리 worktree 등록이 없어 42-case 명령이 ERROR였다. 등록을 보강하고 같은 명령을 재실행해 42개 PASS, repository probe 추가 뒤 같은 명령으로 43개 PASS를 확인했다. 72-case 명령은 공유 JSON decoder에서 Claude malformed-text diagnostic tail을 누락해 FAIL이었다. decode 성공 여부와 진단을 복원한 뒤 같은 명령으로 72개 PASS를 확인했다. command별 최초 결과와 동일 명령 재실행은 최종 `TARGETED_TESTS`에 보고한다.

- baseline tree capture/restore의 직접 unit test 2개를 추가했다. 임시 index·content hash, 고정 author/date, 보존 ref/tree 확인, stage-0 및 기존 index flags 복원, 최종 local snapshot 불일치 거부를 검증한다. 첫 2-case 실행은 fixture가 기존 구현에 없는 GIT_CONFIG_GLOBAL 값을 가정해 ERROR였으며, 기존 deterministic author/date 계약으로 교정하고 동일 명령 재실행에서 2개 PASS를 확인했다.
