---
id: G-015
title: Provider-Neutral Codex and Claude Runtime
sources:
  - docs/PRODUCT.md
depends_on:
  - G-013
complexity: high
risk: high
priority: high
---

## Purpose

프로젝트가 선택한 Codex 또는 Claude Code provider에서 동일한 Planner, Worker와 Reviewer 상태 계약을 사용하고, 모델·Effort와 세션 복구를 코드로 검증할 수 있게 한다.

## Acceptance Criteria

- Agent orchestration은 provider 중립 인터페이스를 사용하며 Codex 전용 명령 구성이나 출력 파싱을 상태 전이 코드에 직접 포함하지 않는다.
- 프로젝트마다 `codex` 또는 `claude` provider를 저장하고, 기존 프로젝트 행은 호환 migration에서 `codex`로 해석한다.
- Codex와 Claude Code 각각 Planner, Reviewer 및 Worker level 1~7의 Model alias와 Effort를 `config.ini`에서 해석한다.
- 선택한 provider CLI가 설치·인증되지 않았거나 Model/Effort가 지원되지 않으면 Agent를 시작하거나 상태를 전이하지 않고 구체적인 설정 오류를 보고한다.
- 두 provider 모두 새 세션 시작, 저장된 세션 재개, 세션 재개 불가 fallback과 역할별 읽기/쓰기 권한을 지원한다.
- Worker 최종 결과와 Reviewer verdict는 provider와 관계없이 기존 구조화 계약으로 정규화되어 commit, verify, Review와 PR 단계가 같은 코드를 사용한다.
- provider별 timeout, 일시 실패와 사용 한도 소진을 G-013의 공통 실패 분류로 변환한다.
- Agent subprocess에는 역할 수행에 필요한 환경만 전달하고 Telegram token이나 다른 Loop Manager 비밀값을 전달하지 않는다.
- Codex의 기존 model catalog 검증, sandbox 경계와 세션 재개 동작은 회귀 없이 유지된다.

## Implementation

- Agent profile, 실행 요청과 결과를 나타내는 provider protocol을 만들고 Codex 코드를 adapter로 이동한다.
- Claude Code adapter에 모델·Effort, 권한 모드, 세션 ID와 결과 수집 경계를 구현한다.
- 프로젝트 provider 필드를 호환 가능한 SQLite migration으로 추가하고 orchestration이 선택된 adapter를 주입받게 한다.
- provider별 CLI 세부 로그는 사용자 상태 디렉터리에 저장하되 prompt나 운영 로그에 비밀값을 복사하지 않는다.

## Validation

- Worker는 provider adapter와 profile validation 테스트 class만 targeted unit test로 실행한다.
- Codex와 Claude의 역할별 명령, Worker level, read-only Reviewer, 세션 시작·재개·fallback을 mock CLI로 검증한다.
- 미설치·미인증 CLI, 잘못된 alias/Effort, timeout, 사용 한도와 비밀 환경 제거를 검증한다.
- 전체 verify에서 기존 Codex Planner/Worker/Reviewer 흐름과 저장된 세션 복구가 유지돼야 한다.

## Progress

- [ ] provider 중립 실행 계약과 Codex adapter 구현
- [ ] Claude Code adapter와 profile 검증 구현
- [ ] 프로젝트 provider 저장 migration과 orchestration 연결
- [ ] provider 오류·세션·보안 회귀 테스트 추가

## Decision Log

- 상태 머신은 provider 출력 형식을 직접 알지 않고 adapter가 공통 결과로 정규화한다.
- 기존 등록 데이터는 Codex로 해석해 비호환 schema 변경을 피한다.
