---
id: G-001
title: Minimal Bootstrap Runner
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: medium
risk: high
---

## Purpose

서버의 외부 스케줄러에서 반복 실행해 Loop Manager의 나머지 기능을 스스로 구현할 수 있는 최소 Codex 기반 실행기를 만든다.

## Acceptance Criteria

- Python 표준 라이브러리만으로 상태 디렉터리와 SQLite를 초기화할 수 있다.
- 등록 절차 없이 명령행으로 지정한 GitHub 프로젝트 하나를 처리할 수 있다.
- 대상 프로젝트의 기본 worktree를 수정하지 않고 별도 branch와 worktree에서 Codex를 실행한다.
- Planner는 최신 원격 base의 detached planning worktree에서 ExecPlan 하나만 작성하고, 검증된 계획을 base에 commit·push한다.
- 계획 push를 확인한 뒤 해당 ExecPlan 전용 branch와 worktree에서 Worker를 실행한다.
- 한 번에 하나의 작은 작업만 구현하고 프로젝트의 전체 검증과 읽기 전용 Review를 통과한 경우에만 PR을 생성한다.
- 열린 PR이 있으면 새 작업을 만들지 않고 Merge를 기다리며, 작업할 내용이 없었던 기준 commit에서는 Codex를 다시 호출하지 않는다.
- 중복 프로세스 실행을 운영체제 파일 잠금으로 거부한다.
- Windows와 Linux에서 최소 검증 스크립트로 단위 테스트를 실행할 수 있다.
- Windows와 Linux에서 준비 스크립트 한 번으로 `.venv`를 만들고 `requirements.txt`를 설치할 수 있다.

## Implementation

- 단일 `loop_manager.py`에 `init`과 `run` 명령을 구현한다.
- SQLite에는 프로젝트별 기준 commit, 현재 phase, branch, worktree, PR과 마지막 오류만 저장한다.
- 기존 `config/config.ini`의 Codex Planner와 Reviewer 설정을 재사용한다.
- worktree 생성, verify, commit, rebase, push와 PR 생성은 프로그램 코드가 담당한다.
- Planner 결과는 형식과 변경 범위를 코드로 검증하고 base에 fast-forward push한 뒤 구현 branch를 만든다.
- Codex Worker는 한 번에 하나의 최소 작업만 수행하고 commit, push, PR 생성과 전체 verify는 수행하지 않는다.
- 병렬 처리, Telegram, Claude, 세션 재개와 전체 ExecPlan 상태 모델은 후속 구현으로 남긴다.
- `requirements.txt`와 환경별 준비 스크립트로 실행 환경을 만들되 프로그램 실행 때마다 다시 설치하지 않는다.

## Validation

- `scripts/verify.ps1`
- `scripts/verify.sh`

## Progress

- [x] 최소 실행기 구현
- [x] 단위 테스트 및 전체 검증
- [x] venv 및 requirements 설치 자동화
- [x] Planner 계획 commit의 base push와 ExecPlan별 구현 branch 분리

## Decision Log

- 완성형 Loop Manager를 먼저 구현하지 않고, 후속 구현 PR을 생성할 수 있는 Codex 전용 세로 단면을 우선한다.
