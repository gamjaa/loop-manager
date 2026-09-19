# [R-006] Timestamped Execution Progress Logs

## Context

Loop Manager를 cron이나 작업 스케줄러로 실행하면 현재 Planner, Worker 또는 Reviewer 중 어느 단계가 어떤 작업을 처리하는지 실행 출력만으로 파악하기 어렵다. 기존 run 스크립트는 실행 시작 시각만 한 번 출력하므로 단계별 진행 시점과 실패 후 되돌아가는 흐름도 알 수 없다.

스케줄 간격보다 이전 실행이 길어질 때 발생하는 잠금 충돌은 정상적인 중복 실행 방지 결과지만, 현재는 오류로 출력돼 실제 장애와 구분하기 어렵다.

## Requirement

Loop Manager는 AI를 사용하지 않고 프로그램 코드가 가진 상태와 검증된 ExecPlan 정보만으로 주요 실행 단계와 실패 반환 흐름을 시각이 포함된 로그로 출력해야 한다.

Planner는 새 ExecPlan을 만들기 전이라 ID와 제목을 알 수 없으므로 다음 ExecPlan 계획을 시작한다는 사실을 출력한다. Worker와 Reviewer는 시작할 때 대상 ExecPlan의 ID와 제목을 출력한다. 전체 verify 또는 Review 실패로 Worker에 작업을 되돌릴 때도 대상 ExecPlan과 반환 이유를 출력한다.

cron이나 작업 스케줄러에서는 선택적 실행 인자를 사용해 이미 다른 Loop Manager 프로세스가 실행 중인 경우 오류 없이 해당 실행을 건너뛸 수 있어야 한다.

## Acceptance Criteria

- `loop_manager.py`가 출력하는 각 운영 로그 앞에 로컬 현재 시각이 `YYYY-MM-DD HH:MM:SS` 형식으로 표시된다.
- Planner 호출 직전에 다음 ExecPlan 계획을 시작한다는 로그가 출력된다.
- Worker와 Reviewer 호출 직전에 코드로 파싱한 ExecPlan ID와 제목이 로그에 포함된다.
- 전체 verify 실행을 시작할 때 대상 ExecPlan ID와 제목이 로그에 포함된다.
- 전체 verify가 실패해 Worker로 되돌아갈 때 대상 ExecPlan ID와 제목 및 verify 실패가 로그에 포함된다.
- Review가 실패해 Worker로 되돌아갈 때 대상 ExecPlan ID와 제목 및 Review 실패가 로그에 포함된다.
- 로그 생성과 ExecPlan 식별에는 SQLite 상태와 프로그램이 파싱한 frontmatter만 사용하며 추가 AI 호출을 하지 않는다.
- `run --skip-if-running`을 지정한 실행이 전역 잠금을 얻지 못하면 메시지를 출력하지 않고 종료 코드 0으로 종료한다.
- `--skip-if-running`을 지정하지 않은 잠금 충돌은 기존과 같이 오류를 출력하고 종료 코드 1로 종료한다.
- `run.sh`와 `run.ps1`은 별도의 실행 시각을 출력하지 않는다.
- run 스크립트의 `git pull` 출력은 시각 접두사 없이 기존과 동일하게 유지된다.
- README에서 cron 또는 작업 스케줄러용 `--skip-if-running` 사용법을 확인할 수 있다.
- 시각 접두사, Agent와 verify 시작, 실패 후 Worker 반환 및 잠금 충돌 선택 동작을 자동 테스트로 검증한다.

## Product Impact

- 사용자는 스케줄러 출력만으로 Loop Manager의 현재 실행 단계와 대상 ExecPlan을 대략적으로 확인할 수 있다.
- verify 또는 Review 실패 후 Worker가 다시 수정 작업을 맡게 된 사실을 확인할 수 있다.
- 예상 가능한 스케줄 중복 실행을 실제 오류와 구분해 조용히 건너뛸 수 있다.

## Non-Goals

- 로그 파일 저장, rotation 또는 보존 정책은 추가하지 않는다.
- 구조화 로그, 로그 레벨 또는 외부 로그 수집 서비스를 도입하지 않는다.
- 로그 문구를 만들거나 요약하기 위해 AI를 호출하지 않는다.
- `git pull` 출력의 각 줄에 시각을 추가하지 않는다.
- 잠금 충돌 이외의 오류를 숨기거나 성공으로 처리하지 않는다.

## Open Questions

- 없음
