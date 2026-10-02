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

- [ ] Agent/profile/prompt/scope 소비 관계와 중복 확인
- [ ] 책임별 service·port 추출과 직접 소비자 전환
- [ ] 관련 targeted unit test와 provider test 정리
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: 기존 Agent module을 늘리는 데 그치지 않고 root의 실제 구현과 호출자를 새 경계로 옮긴다. 신규·저장·차단 scope는 증거 적용 시점이 달라 같은 계약의 부분만 공유한다.
- 현재 Merge된 PRODUCT와 구현을 유지한다. 미구현 R-049의 신규 Planner 역할이나 model 선택은 이번 리팩터링에 포함하지 않는다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
