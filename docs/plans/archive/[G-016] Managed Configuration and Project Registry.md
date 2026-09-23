---
id: G-016
title: Managed Configuration and Project Registry
sources:
  - docs/PRODUCT.md
depends_on:
  - G-015
complexity: high
risk: high
priority: high
---

## Purpose

명시적인 설정 절차에서 여러 대상 프로젝트와 provider를 검증·등록하고, 반복 실행이 임시 명령행 승인 대신 저장된 안전 정책과 유효한 전역 설정을 사용하게 한다.

## Acceptance Criteria

- 설정 파일이 없거나 형식이 유효하지 않으면 일반 Loop 실행은 SQLite나 프로젝트 상태를 자동 생성·변경하지 않고 설정 방법을 안내한다.
- 명시적인 설정 모드에서 상태 DB 초기화, 프로젝트 목록 조회, 프로젝트 추가와 제거를 수행할 수 있다.
- 프로젝트 추가 시 저장소 루트, GitHub origin, base branch, 선택 provider CLI와 인증, 필수 문서, 활성 PRODUCT/Requirement 및 공통 `docs/PLANS.md` 해시를 코드로 검증한다.
- 변경되지 않은 PRODUCT 또는 ARCHITECTURE 템플릿만 있는 프로젝트는 작업 가능 프로젝트로 등록하지 않는다.
- 공통 `AGENTS.md` 내용이 없으면 경고하고 사용자 승인 없이는 등록하지 않는다. 승인 시 파일을 바꾸지 않고 현재 파일 해시와 승인 사실을 저장한다.
- 등록 시 선택한 provider, base branch와 AGENTS 승인 해시를 SQLite에 저장하고 자동 Loop에서는 등록 검사를 매번 다시 수행하지 않는다.
- `READY`, `IMPLEMENTING`, `VERIFYING`, `REVIEWING`, `PUBLISHING`, `PR_OPEN`, `BLOCKED` ExecPlan 또는 해결되지 않은 `planning_error`가 있으면 프로젝트 제거를 거부한다.
- 프로젝트 제거는 프로젝트 파일, branch, worktree, PR 또는 감사용 MERGED 이력을 삭제하지 않는다.
- `[loop].max_workers`는 기본값 3의 양의 정수로 검증하고, Telegram token과 chat ID는 둘 다 비어 있거나 둘 다 있어야 한다.
- 일반 실행에서 명령행 repository와 일회성 `--allow-agents-mismatch`로 프로젝트를 암묵 등록하는 경로를 제거하고 저장된 registry를 원본으로 사용한다.
- config 오류와 등록 오류 메시지에 Telegram token, 인증 정보 또는 다른 비밀값을 포함하지 않는다.

## Implementation

- config parsing/validation과 프로젝트 registry 저장소를 Agent profile 해석 및 실행 orchestration에서 분리한다.
- 설정용 CLI subcommand와 비대화식으로도 자동 테스트 가능한 입력 경계를 제공한다.
- 현재 `projects` 테이블을 호환 migration으로 확장하고 기존 자동 등록 행은 Codex 프로젝트로 보존한다.
- 공통 파일, 템플릿과 AGENTS 검증은 등록 시에만 호출되는 순수 검사 함수로 구성한다.

## Validation

- Worker는 config validation과 project registry 테스트 class만 targeted unit test로 실행한다.
- 유효한 추가·조회·제거, AGENTS 강제 승인 저장, 활성 작업과 planning_error가 있는 제거 거부를 검증한다.
- 잘못된 max_workers, 일부 Telegram 설정, template-only 문서, PLANS 해시 불일치, provider 인증 실패를 검증한다.
- 전체 verify에서 기존 SQLite 데이터, repository 안전 검사와 상태 DB 손상 오류 처리가 유지돼야 한다.

## Progress

- [ ] 전역 config validation 경계 구현
- [ ] 프로젝트 registry schema와 설정 CLI 구현
- [ ] 등록 시 공통 파일·provider·AGENTS 승인 검증 구현
- [ ] 호환 migration과 등록 수명주기 테스트 추가

## Decision Log

- 공통 파일 불일치 승인은 일회성 run flag가 아니라 등록 당시 파일 해시에 결합한다.
- 프로젝트 제거는 Loop Manager의 등록만 해제하며 사용자 Git 자산은 삭제하지 않는다.
