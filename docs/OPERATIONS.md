# 운영 안내

## 인증과 실행 조건

Git·GitHub CLI·선택 Agent CLI를 같은 실행 계정에서 설치·인증한다. Manager는 Git Push와 PR 생성·본문 갱신·Draft/Ready·필요 없는 PR Close·완료 문서 Archive를 수행한다. 최종 PR Merge는 사용자가 맡는다. Archive 게시에는 base Push 권한이 필요하다.

Windows는 Python 3.13 이상과 PowerShell 7을 사용한다. Agent는 Provider의 sandbox·자동 실행 정책을 사용하고 권한 확대를 자동 허용하지 않는다. 조직 정책·실계정 호환성의 관측 한계는 [Claude 실환경 후속 점검](FUTURE_IDEAS.md#claude-실환경-후속-점검)에 구분한다.

## 설정

`config/config.ini.example`을 `config/config.ini`로 복사한다. `--config <절대경로>`로 대체할 수 있다. 설정 파일은 안전한 배정 경계에서 다시 읽는다. 실행 중인 호출의 설정은 소급 변경하지 않는다.

| 항목 | 의미 |
|---|---|
| `[loop] max_workers` | 전체 프로젝트 Planner·Worker·Reviewer 공통 AI 한도, 양수 |
| `verify_timeout_seconds` | 필수 Verify 실제 실행 제한, 양수 초. 자원 대기 제외 |
| `verify_parallelism` | 이 저장소 자체 검증 도구의 내부 병렬도. `auto` 또는 양수. 다른 프로젝트에 강제하지 않음 |
| `[provider.models]` | 모델 별칭과 실제 모델 이름 |
| `[provider.roles] planner`, `reviewer` | `모델별칭,effort` |
| `[provider.worker] level1`–`level6` | complexity low/medium/high의 1/3/5에 critical risk일 때만 1을 더해 선택 |
| `[provider] fast_mode` | 선택 Provider Fast 요청. 생략·공란은 false |
| `[telegram] token`, `chat_id` | 둘 다 비우면 미사용. 하나만 설정하면 오류 |

프로젝트별 Worker와 전역 Verify는 각각 하나다. 별도 관리 대상 수·유지보수·부분 검증 설정은 없다. 잘못된 필수 모델 설정을 다른 모델로 조용히 대체하지 않는다. Linux CLI 설치 경로가 바뀌면 같은 계정의 정상 터미널에서 `scripts/setup.sh`를 다시 실행해 `config/execution-path.txt`를 갱신한다.

## 상태와 요청

상태 경로는 `--state-dir`, `LOOP_MANAGER_STATE_DIR`, 사용자별 기본 경로 순으로 선택한다. 정확한 DB 경로는 `init` 출력으로 확인한다. 모든 프로젝트와 CLI는 해당 경로의 `loop-manager.db` 하나를 WAL 모드로 사용한다.

```sh
python loop_manager.py --state-dir /path/to/new-state init
python loop_manager.py --state-dir /path/to/new-state add /path/to/project --base develop
python loop_manager.py --state-dir /path/to/new-state status
```

조회는 SELECT만 수행하며 설정·네트워크·runtime 잠금이 필요 없다. 쓰기 경합은 짧은 busy 제한 후 오류를 반환한다. runtime 중복 방지 잠금과 CLI 직접 DB 접근은 별개다.

| 명령 | 즉시 반환 | 후속 처리 |
|---|---|---|
| `pause`, `resume` | 최신 배정 의도 확정 | 신규 단계 직전 반영. 실행 중 호출은 안전한 반환 |
| `add`, `remove` | 요청 ID와 접수 결과 | 실제 준비/진행 호출 반환 확인 뒤 최종 결과 |
| `unblock` | 차단 재평가 접수 | Worker 재평가의 재개 성공·재개 불가 결과 |
| `manual start` | 실제 준비 경로·브랜치 또는 오류 | 응답 전 중단이면 같은 요청과 Git 상태 재관측 |
| `manual complete` | 게시 후보 인수 접수 | dependency·자원 충족 후 Verify·Reviewer·같은 G PR |

같은 미완료 요청은 같은 ID로 응답하고 상충하는 요청은 거부한다. 접수만 반환한 명령의 최종 결과는 DB와 Outbox에 원자적으로 남긴다. CLI에 최종 결과를 출력한 요청은 중복 Telegram을 억제한다. 출력·DB 기록 사이 중단에서는 누락보다 중복 가능성을 감수한다.

`status`는 G 상태·수동 소유·작업 경로·현재 오류·요청 결과를 표시한다. `logs`는 현재 오류와 남아 있는 호출의 작은 결과다. `usage`는 보존 중인 실제 호출만 역할·Provider·모델별로 집계한다. 종료 후 정리된 사용량은 원격 PR 본문에서 확인한다. 누락 토큰은 0으로 추정하지 않는다.

## 수동 구현

1. `manual start <project> G-001`로 base의 비 draft READY G를 준비한다. 선행 G는 실제 Merge돼 있어야 한다.
2. 반환된 격리 공간에서 구현·관련 테스트 후 `git commit`·`git push origin HEAD`로 게시한다.
3. `manual complete <project> G-001`로 접수한다. 필수 Verify·Reviewer는 생략하지 않는다.

준비 오류에도 작업물과 예약을 유지한다. 같은 `manual start` 재실행이나 runtime 재관측으로 이어간다. 미게시 변경이 남아 있으면 원격 제출 성공으로 취급하지 않는다. 다른 G는 독립적으로 진행한다.

## 차단과 재시작

중요 정책 질문이나 Agent 교정으로도 해결할 수 없는 충돌만 관련 G를 BLOCKED로 처리한다. 문서 오류·일시 네트워크 오류·원격 전진·공간 경합은 작업물을 보존하며 재시도한다. `logs`의 원인을 고친 뒤 다음 실행을 기다리거나 명시적 `unblock`을 접수한다. 문서 변경으로 차단 근거가 바뀌면 자동 재평가할 수 있다.

열린 PR에 추가 작업이 필요하면 같은 PR을 Draft로 바꾼다. 현재 후보의 Verify·Reviewer와 충돌 해소가 확인되면 Ready로 전환한다. Merge 없이 닫힌 구현 PR은 필요하면 같은 PR을 다시 연다. 삭제된 기존 관리 원격 브랜치는 자동 재생성하지 않는다.

다음 `run`은 요청·generation·실행 프로세스와 실제 Git·GitHub 효과를 대조한다. 게시 직후 중단해도 원격 결과부터 확인한다. 현재 검증 입력을 안전하게 비교할 수 없거나 성공 근거가 없으면 PASS로 가정하지 않는다.

## runtime과 스케줄러

`run.sh` 또는 `run.ps1`에 `--skip-if-running`을 전달해 cron·작업 스케줄러에서 호출한다. 내부 상시 스케줄러는 없다. 배정 가능한 작업을 처리하고 PR·pause·재시도 시각 등으로 할 일이 없으면 반환한다.

Bootstrap은 자신의 등록 원격 base, 등록이 없으면 origin 기본 브랜치의 SHA를 선택한다. `runtimes/<SHA>`의 저장소·HEAD·진입부 계약·Python 구문을 확인한 뒤 child를 실행한다. 초기 전환은 새 구현을 원격 base에 먼저 반영한다. 사용자 checkout의 미게시 수정은 실행 코드가 아니다.

새 단계 경계에서 실행 코드·스크립트·의존성·역할 지침의 실질적 변경이 확인되면 신규 배정을 멈춘다. 진행 호출은 강제 종료하지 않고 반환·Checkpoint 저장 후 인계한다. 문서 전용·SHA 전용 변화와 사용자 PR Merge 대기를 갱신 조건으로 삼지 않는다.

## 상태 보존과 환경 인수

기존 DB를 자동 초기화·마이그레이션하지 않는다. 전환은 이전 Writer 종료 → 기존 상태 디렉터리 별도 보관 → 새 상태 경로 `init` → 같은 원격/base로 `add` 순서다. 실행 중 WAL의 `-wal`·`-shm`을 임의 삭제하지 않는다. 백업은 Writer 종료 후 DB 복사 또는 SQLite 백업 도구를 사용한다.

새 DB에서는 base의 활성/보관 문서·관리 원격 브랜치·전체 PR 이력으로 ID와 실제 Merge를 복원한다. 과거 토큰·세션·PASS와 미게시 로컬 변경은 합성하지 않는다. 여러 호스트의 동시 WAL Writer는 지원하지 않는다.

완료·취소 뒤 최종 PR 집계·Archive·알림·R 최종 평가용 작은 근거를 먼저 보존한다. 이후 세션·토큰·임시 공간을 정리한다. 미게시 사용자 변경이나 삭제 불확실성이 있으면 보존하고 정리만 재시도한다. 원격 작업 브랜치는 삭제하지 않는다.

## 알림과 검증 진단

PR·Ready 재검토·차단·비동기 CLI 최종 결과를 공통 Outbox에서 전송한다. 신규 G 시작과 자율 재개는 별도 통지하지 않는다. 같은 후보의 충돌 복구·Ready는 한 사건으로 알린다. 여러 실제 Worker 시도 후 24시간 동안 후보 진전이 없을 때만 장기 무진전을 알리며 순번·PR·사용량 대기 자체는 경고하지 않는다.

전송 실패는 개발 작업을 막지 않으며 미전송 부분부터 재시도한다. Telegram을 끄면 보류 payload를 취소한다. 전송 직후 DB 확인 전 중단에는 중복 가능성이 있다. 이 저장소의 검증 명령·임시 폴더·보고서는 [VERIFY](VERIFY.md)를 따른다.
