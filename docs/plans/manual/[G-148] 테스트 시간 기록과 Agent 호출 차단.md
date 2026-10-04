---
id: G-148
title: 테스트 시간 기록과 Agent 호출 차단
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-058] Test Timing and Agent-Free Test Execution.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on:
  - G-145
complexity: medium
risk: medium
priority: normal
---

## Purpose

직렬 unittest의 테스트·fixture·공통 단계 시간을 남기고 실제 Agent 호출을 차단해 후속 비용 개선과 부하 기반 병렬 실행의 기준선을 확보한다. 전체 검증은 Manager가 실행하며 이 계획에서는 테스트 순서와 실행량을 바꾸지 않는다.

## Acceptance Criteria

- Windows·Linux에서 각 테스트의 ID·시작·종료·결과·소요 시간과 공통 준비·정리, discovery/import·compile 및 전체 시간이 구분된다. setUp·tearDown·cleanup과 class/module fixture의 큰 비용이 기록에서 사라지지 않는다.
- 실행 revision·명령·도구 버전과 환경 제한, 관측 가능한 프로세스 메모리 비용을 기록하고 느린 항목을 비교할 수 있다. 관측하지 못한 값은 추정치와 구분한다.
- 강제 종료 뒤 완료 기록과 현재 테스트 또는 공통 단계의 시작·경과 시간을 확인할 수 있다. 결과·종료 코드와 원래 검증 범위는 유지된다.
- 실패 피드백의 길이 제한과 무관하게 필요한 시간 진단이 보존되고 비교용 집계가 생성된다. 성공 출력 전체의 Worker 전달은 추가하지 않는다.
- 기존 전체 테스트의 실제 Agent 실행 경계가 mock·stub·고정 출력으로 대체되고 예상 밖의 호출은 실행 전에 실패한다. 정상·오류·재개 인자 계약과 필요한 실제 Git·SQLite·운영체제 검증은 유지된다.

## Implementation

1. 현재 unittest 수집·실행 경로와 테스트의 Agent CLI·API 호출 경계를 확인한다. 표준 라이브러리 TestResult의 기록 경계를 재사용하고, 공통 fixture와 discovery/import·compile은 별도 단계로 기록한다. 각 테스트 본문을 시간 측정 코드로 일괄 수정하지 않는다.
2. 실행·단계·테스트의 시작과 완료 이벤트를 즉시 보존하고, 진단 로그는 기존 Manager 상태·로그 경계에 둔다. 순수 runner 테스트에는 임시 경로를 주입한다. 읽기용 시간 순 집계와 최소 환경·자원 요약을 생성하며 Worker용 자료에서 성공 stdout과 비밀값을 제외한다.
3. `execution/verify_process.py`, verify 피드백 경계와 두 OS 스크립트를 연결한다. G-145의 종료 확인을 유지하고 완료 이벤트가 없는 항목을 성공 또는 완료 시간으로 합성하지 않는다. 새로운 외부 저장·모니터링 서비스를 추가하지 않는다.
4. 실제 Agent 실행을 막는 테스트 전용 경계를 두고 현재 테스트를 전수 점검한다. 명령·session·출력·사용량·실패·복구를 fake로 검증하며 Git·SQLite subprocess까지 일괄 차단하지 않는다.
5. Manager의 직렬 전체 verify 결과에서 기준선과 측정 자체의 비용을 확인한다. 후속 계획에 제공할 시간·자원 집계는 코드로 준비하고 원본 성공 출력의 자동 전달 정책은 유지한다.

## Validation

- Worker는 새 시간 기록 unit test와 변경 경계에 직접 관련된 `tests/test_verify_process.py`, `tests/test_verify_timeout_policy.py`의 최소 unit case만 실행한다. 가짜 시계·runner·Agent adapter로 준비 실패, cleanup 오류, skip, subtest 실패, expected failure와 중단 이벤트를 확인한다.
- Manager 전체 verify는 기존 수집 범위·결과 판정, 실제 Agent 무호출, 기록 flush와 강제 종료 후 잔존 진단을 확인한다. 실제 프로세스 중단·자식 종료 회귀와 전체 기준선 측정은 Manager가 수행한다.

## Progress

- [x] 시간·결과·공통 단계 기록과 비교용 집계 구현
- [x] 테스트 native 실행 경계 정적 점검·차단과 targeted unit test
- [ ] Manager 직렬 기준선·전체 verify, 읽기 전용 Review와 사용자 Merge

## Decision Log

- 사용자는 시간 기록 → 측정된 병목 개선 → 코드 기반 부하 적응형 병렬 실행 순서의 문서 작성을 요청했다. 이 계획은 첫 단계다.
- G-145가 수정하는 verify 종료·정리 경계를 소비하므로 해당 Merge 뒤 진행한다. 기존 계획의 파일·실행 이력은 수정하지 않는다.
- 시간·자원 집계는 진단 입력이며 과거 검증 성공을 현재 후보의 성공 근거로 사용하지 않는다. 특정 속도 개선율은 아직 측정하지 않았다.
- 2026-10-04: 사용자가 G-148·G-149·G-150의 로컬 직접 구현을 요청해 G-152의 격리 branch에서 함께 준비한다. 이 문서는 `manual/`의 직접 작업 기록이며 Manager의 active catalog·SQLite 실행 상태와 원래 dependency gate를 변경하지 않는다. G-145의 기존 Linux subreaper를 재사용하고 이번 연결에 필요한 독립 cleanup 시도만 `verify_process.py`에서 보완한다. process lock 정리는 건드리지 않았다.
- `verification/timing.py`는 즉시 flush하는 JSONL과 별도 집계를 남긴다. 테스트 시간에는 setUp·tearDown·cleanup이 포함되고 class/module fixture, import, compile, 자원 대기와 자식 정리는 별도 단계다. 포함 관계인 시간은 합산하지 않는다. 중단된 테스트를 완료로 합성하지 않으며 종료 시각이 있으면 나중에 읽어도 경과 시간이 늘지 않는다.
- Agent 차단은 테스트 프로세스의 native subprocess·exec·socket audit와 Python 자식 bootstrap에서 적용한다. 기존 mock 경계와 실제 Git·SQLite 사용은 유지한다. 모든 `tests/**/*.py`의 AST에서 Agent 이름을 직접 실행하는 native literal 호출은 0건이었다. 외부 API/DNS와 실제 CLI 시도는 가짜 audit 입력으로 거부 동작을 확인했으며 실제 Agent를 호출하지 않았다.
- Manager 진단은 상태 디렉터리 `logs/verify/<실행 ID>/`, 단독 실행은 `.cache/verify/<실행 ID>/`에 둔다. 실패 모듈 원문은 재사용 worker 로그의 해당 구간만 전달하고 비밀값을 가린다. bounded Worker 피드백에 집계 경로를 보존한다. `--report`는 manifest·관측 환경과 실측 시간을 나란히 제공하며 검증을 실행하지 않는다.
- G-148~150에 직접 관련된 targeted unit case 50개가 통과했다. 최종 묶음에서 49개 통과 후 새 wiring test의 mock import 누락을 고쳤고 실패했던 1개를 재실행해 통과했다. Python AST 21개와 PowerShell 구문 확인이 통과했고 기존 compile 대상 215개가 새 225개 대상에 모두 포함됐다. Manager 전체 verify·실제 중단 회귀·직렬 기준선과 측정 오버헤드·Review·Merge는 미수행이다.
- 최신 main `99f7971`을 구현 commit `03d15c9`에 충돌 없이 Merge한 뒤 import·compile·설정 전달·진단·기존 fixture의 unit case 5개가 통과했다. 최신 base와 비교한 변경 Python AST 23개가 파싱됐고 추가된 main 모듈까지 compile 대상 226개에 포함된다. 기존 G-151 번호 충돌은 이 PR의 seed 기록을 G-152로 옮겨 해소했다.
