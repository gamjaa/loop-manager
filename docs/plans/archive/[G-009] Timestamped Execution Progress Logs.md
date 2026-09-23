---
id: G-009
title: Timestamped Execution Progress Logs
sources:
  - docs/requirements/[R-006] Timestamped Execution Progress Logs.md
depends_on: []
complexity: low
risk: medium
---

## Purpose

Loop Manager의 주요 실행 단계와 실패 후 Worker 반환 흐름을 로컬 시각, 대상 ExecPlan과 함께 일관되게 출력하고, 스케줄러의 정상적인 중복 실행은 선택적으로 조용히 건너뛸 수 있게 한다.

## Acceptance Criteria

- `loop_manager.py`가 생성하는 운영 로그는 로컬 현재 시각의 `YYYY-MM-DD HH:MM:SS` 접두사를 사용하며 로그 문구를 만들기 위한 AI 호출이나 새 영속 상태를 추가하지 않는다.
- Planner 호출 직전에 다음 ExecPlan 계획을 시작한다는 로그를 출력하고, 아직 정해지지 않은 ExecPlan ID나 제목을 추정하지 않는다.
- Worker, 전체 verify와 Reviewer 호출 직전에는 검증된 ExecPlan의 ID와 제목을 포함한 단계 시작 로그를 출력한다. 최신 base 반영 후 전체 verify를 다시 실행하는 경우에도 대상 계획을 식별할 수 있다.
- 전체 verify 실패 또는 Review 실패로 상태를 Worker 단계로 되돌릴 때 대상 ExecPlan ID·제목과 반환 이유를 로그에 포함한다.
- `run --skip-if-running` 실행이 전역 잠금을 얻지 못하면 stdout과 stderr에 메시지를 남기지 않고 종료 코드 0으로 끝난다.
- `--skip-if-running`을 지정하지 않은 잠금 충돌은 시각이 포함된 기존 오류를 출력하고 종료 코드 1을 유지하며, 잠금 충돌이 아닌 오류는 이 옵션으로 숨기지 않는다.
- `run.sh`와 `run.ps1`은 별도의 실행 시각 배너를 출력하지 않으며 `git pull`의 기존 출력과 실패 종료 코드는 바꾸지 않는다.
- README의 cron 및 작업 스케줄러 예시는 정상적인 중복 실행을 조용히 건너뛰려면 `--skip-if-running`을 사용하도록 안내한다.
- 시각 접두사, 각 Agent와 verify 시작, verify·Review 실패 반환, 선택적 잠금 충돌 처리와 실행 스크립트 출력 경계를 자동 테스트로 검증한다.

## Implementation

- 로컬 현재 시각을 주입 가능하게 얻고 stdout 또는 stderr에 운영 메시지를 기록하는 작은 공통 로그 경계를 추가한 뒤, `loop_manager.py`가 직접 출력하는 상태·오류 메시지를 이 경계로 통일한다.
- `run_project`의 Planner, Worker, VERIFYING과 REVIEWING 진입부에서 호출 직전 로그를 남긴다. Worker 이후에는 검증된 계획 메타데이터를 재사용하고, Planner는 계획 대상이 아직 없다는 경계를 유지한다.
- 최초 verify와 rebase 후 verify 실패, Reviewer 실패 경로에서 상태를 `WORKING`으로 되돌리기 전에 계획 ID·제목 및 반환 사유를 기록한다. Agent나 verify의 원본 진단 출력은 기존 상태 저장과 재시도 입력에 계속 사용한다.
- 예상한 잠금 경합만 구분할 수 있는 전용 예외 또는 결과 경계를 만들고 `run` 하위 명령에 `--skip-if-running` 플래그를 추가한다. `main`은 이 플래그가 있는 잠금 경합만 조용한 성공으로 바꾸고 다른 실행 오류는 기존 실패 처리로 보낸다.
- Bash와 PowerShell 실행 스크립트의 시각 배너만 제거하고 `git pull --ff-only` 출력 필터링 및 Loop Manager 인자 전달은 유지한다.
- README의 반복 실행 예시와 잠금 설명에 `--skip-if-running`의 스케줄러용 의미와 기본 잠금 충돌 동작을 함께 기록한다.

## Validation

- 고정된 로컬 시각을 주입해 stdout·stderr 운영 로그가 `YYYY-MM-DD HH:MM:SS` 접두사를 갖는지 검증하는 targeted unit test
- Planner 시작에는 계획 예고만, Worker·verify·Reviewer 시작에는 검증된 ExecPlan ID와 제목이 포함되는지 검증하는 단계별 targeted unit test
- 최초 verify, rebase 후 verify와 Review 실패가 대상 ExecPlan 및 실패 종류를 기록하고 같은 Worker 흐름으로 반환되는지 검증하는 targeted unit test
- 잠금 경합에서 기본 `run`은 오류와 종료 코드 1을 유지하고 `run --skip-if-running`만 무출력 종료 코드 0이 되며 다른 오류는 숨기지 않는지 검증하는 CLI targeted unit test
- `run.sh`와 `run.ps1`에 별도 시각 배너가 없고 기존 `git pull` 처리와 인자 전달이 유지되며 README에 스케줄러용 옵션이 안내되는지 확인하는 회귀 테스트
- 전체 verify에서 기존 Planner→Worker→VERIFYING→REVIEWING→PR 생성 순서와 잠금 해제가 회귀하지 않는지 확인

## Progress

- [ ] 공통 시각 로그 경계와 기존 운영 메시지 적용
- [ ] Planner·Worker·verify·Reviewer 시작 및 Worker 반환 로그 추가
- [ ] `--skip-if-running` 잠금 경합 처리와 CLI 문서화
- [ ] 실행 스크립트 시각 배너 제거 및 로그·잠금 회귀 테스트 추가

## Decision Log

- R-006은 기존 단계 상태와 검증된 ExecPlan 메타데이터만 사용해 구현할 수 있고 SQLite schema나 다른 미완료 ExecPlan의 결과가 필요하지 않아 선행 의존성을 두지 않는다.
- 조용한 성공은 사용자가 명시한 `--skip-if-running`의 예상 잠금 경합에만 한정해 실제 실행 오류가 스케줄러에서 성공으로 오인되지 않게 한다.
