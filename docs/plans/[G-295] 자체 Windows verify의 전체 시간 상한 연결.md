---
id: G-295
title: 자체 Windows verify의 전체 시간 상한 연결
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-309
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: medium
risk: high
priority: high
---

## Purpose

이미 구현된 자체 Windows의 WSL 전체 → Windows 핵심 회귀에서 복사 준비·외부 명령·두 단계·정리를 같은 실제 실행 시간 예산에 연결한다. R-091의 `ExecutionClock`·공유 대기 누계·WSL lifecycle receipt와 이번 delta의 native 호출 `timeout, execution_clock=budget` 전달을 재사용한다. native가 WSL 경과 시간을 두 번 빼던 경계는 직접 교정됐다. 남은 누락은 timeout 없는 snapshot Git, 각각 고정 10초인 WSL 준비 Git, 준비 중 별도 wall clock 계산과 정리·로그 복사 뒤의 만료 확인이다. 호출 전 예산 소진인데도 native를 시작하거나 전체 성공을 반환하는 경계도 보완한다.

R-067/R-091의 새 신뢰된 runner 통합 단계·상하위 순수 대기 판정은 G-309의 공통 구현을 소비한다. 이 작업은 Windows 준비/정리 누락과 그 단계 전환을 연결하며 통합 단계나 session 대기 합산을 별도로 재구현하지 않는다.

## Acceptance Criteria

- R-065/R-067/R-091: 단독·Manager 호출 모두 준비·snapshot·WSL 경로 변환·Linux 준비/실행·native·로그 보존·정리에 하나의 실제 실행 시간 예산을 적용한다. `ExecutionClock`과 확인된 대기 누계를 재사용해 순번·자원 배정 대기만 제외하고 실제 복사·I/O·자식 실행·정리는 계산한다. 각 단계의 남은 예산을 재계산하며 고정 timeout이나 단계 전환으로 예산을 초기화하지 않는다. Manager의 바깥 timeout을 늘리지 않는다.
- snapshot Git과 WSL 준비 Git·실제 자식 실행에는 남은 시간에 제한된 실행·종료 확인을 적용한다. archive/복사·진단 보존 같은 파일 작업도 준비/정리 예산에 포함해 경계마다 만료를 확인하고, 큰 입력이나 느린 외부 명령이 단독 실행의 무제한 준비 경로가 되지 않게 한다. 확인한 기존 프로세스 경계를 재사용하며 다른 프로젝트 launcher의 계약은 바꾸지 않는다.
- 예산 소진 뒤 native나 다음 준비/테스트를 새로 시작하지 않는다. 두 단계의 종료 코드가 0이어도 필요한 정리·최종 deadline 확인까지 충족하지 못하면 전체 PASS를 만들지 않는다. 부분 요청의 기존 실행 없는 FALLBACK과 전체 검증 결과를 구분한다.
- 정리를 위해 전체 시간 안에 필요한 여유를 확보하고, timeout·취소·출력 단절·준비 실패 뒤에도 해당 실행의 자식 종료와 전용 `/tmp/`·bundle 정리 결과를 확인한다. `cleanup.json`의 stopped/removed를 추측하지 않으며 종료 불명은 기존 `VerifyTerminationUncertain`·진단으로 남긴다. 종료 불명을 숨기려고 살아 있는 자식의 자료나 다른 실행의 폴더·예약을 삭제하지 않는다.
- 현재 WSL 독립 Linux 조율과 native Manager Windows 예약 조율, Agent 차단·manifest/fixture·필수 compile·native 목록을 보존한다. 실패·미실행 단계와 단계별 결과는 그대로 기록하며 cleanup 시간 추가·검사 skip·범위 축소로 통과시키지 않는다. 문서의 시간 상한 설명은 실제 보완 결과와 일치한다.

## Implementation

- [ ] 확인한 `scripts/verify_windows.py:windows_main()`·`git()`·`snapshot()`·`run_wsl()`·`wsl_main()`의 남은 시간 계산과 예외/정리를 기존 `verification/execution_clock.py:ExecutionClock`에 연결한다. 공유 budget 디렉터리·WSL 경로 전달·종료 receipt와 native의 원래 timeout/같은 clock 전달은 재구현하지 않는다. 남은 시간을 timeout에 다시 넘겨 앞선 경과를 이중 차감하지 않으며, native 호출 전에 만료를 검사해 프로세스 생성 자체를 막는다. Git·archive·복사 준비와 최종 정리를 계산하고 Windows/WSL monotonic 값은 직접 빼지 않는다. 필요한 국소 helper는 이 launcher 안에 두고 새 scheduler·설정을 만들지 않는다.
- [ ] 확인한 `loop_manager_internal/execution/verify_process.py:run_verify_process()`의 timeout·Windows Job·Linux 자식 종료 확인과 기존 heartbeat/cleanup receipt를 재사용한다. 기존 helper의 반환/종료 불명 의미를 완화하지 않고 남은 실행 시간과 정리 여유를 호출 경계에서 전달한다. 공통 helper 변경이 필요하면 두 OS의 현재 호출자·관련 최소 회귀를 함께 대조한다.
- [ ] G-309의 실제 통합 단계·상하위 예산 연결을 현재 WSL/Windows launcher의 동일 실행 범위에서 소비한다. 하위 runner의 확인된 대기 공제와 launcher 자신의 snapshot·복사·결과 처리/정리 시간을 구분하고, 단계 사이의 기존 누계 identity를 보존해 이중 공제나 예산 초기화를 막는다. 일반 테스트/예제 자식에 인증값·예산 제어값을 전달하지 않는다.
- [ ] 확인한 `tests/test_verify_windows_launcher.py:WindowsVerifyPipelineTests`·`WslCleanupTests`의 fake process/예외 fixture를 확장해 실제 시간 계산·다음 호출 차단·결과 기록을 검증한다. `tests/test_verify_windows.py`의 실제 두 단계/native 실패·Windows 모체 종료 후 Linux 정리 회귀는 유지하며 정책 조합별 실제 Git/WSL 실행을 추가하지 않는다.
- [ ] 확인한 `docs/VERIFY.md`의 전체 timeout·불완전 결과 계약과 ARCHITECTURE의 공통 verify 실행/종료 설명에 구현한 경계만 통합한다. 단계별 관측은 G-248, Windows 임시 경로 선택은 G-253, 실제 OS·부하 자료는 G-187의 책임으로 유지한다. Worker는 Requirement·다른 ExecPlan·선택 frontmatter를 수정하지 않는다.

## Validation

- Worker는 `tests/test_verify_windows_launcher.py`에서 변경한 최소 unit case만 실행한다. fake monotonic clock·외부 명령/프로세스 관측을 사용하되 deadline 계산·단계 전환·파일 결과 기록은 실제 launcher로 검증한다. 남은 시간을 인수로 받았다는 assertion만으로 전체 예산 준수를 확정하지 않는다.
- snapshot Git 지연·snapshot 도중 만료, WSL 변환 뒤 만료, Linux의 여러 준비 명령 누적, WSL PASS 뒤 예산 소진, native 종료 직후/정리 중 만료를 구분한다. 만료 뒤 후속 process 호출 수 0, 전체 passed=false·실패 종료, 실제 완료/미실행 단계 보존을 확인한다. 여유 있는 정상 경로는 두 단계와 정리까지 성공해야 한다.
- `tests/test_task_scheduling.py:ExecutionClockTests`와 `tests/test_verify_windows_launcher.py:test_native_does_not_subtract_wsl_elapsed_time_twice`의 기존 누계/이중 차감 방지 회귀를 재사용한다. 남은 launcher 연결에서 대기 30초·실제 처리 2초·예산 10초와 실제 처리 초과를 구분한다. 다른 자식이 실행 중인 시간·복사/정리 시간은 제외하지 않으며 WSL → native 전환에서 앞선 실행 시간을 유지한다. 현재 원래 timeout·동일 clock을 확인하는 assertion을 축소된 timeout 기대값으로 되돌리지 않고 실제 timeout·종료 보호는 유지한다.
- 출력 단절·부모 취소·자식 종료 불명·receipt 부재/손상/removed=false에서 실제 미확정 상태와 기존 파일을 보존한다. 준비 실패의 미시작 자식과 실행 후 종료 불명을 구분하고 다른 run marker·예약/폴더를 변경하지 않는지 검사한다. slow cleanup을 빠른 성공 mock으로 대체하거나 sleep/전체 timeout 확대로 검증하지 않는다.
- G-309의 신뢰된 통합 단계가 WSL 전체 범위에 포함된 상태에서 launcher 준비/정리와 하위 대기가 겹친 경계를 기존 fake clock/process fixture로 확인한다. 하위 생존 중 확인된 순수 대기만 제외하고 launcher 처리·형제 실행·heartbeat 단절/관측 불명은 계산한다. 같은 구간의 상하위 중복 공제·WSL → native 예산 초기화를 거부하고 하위 기록/상위 판정 불일치에서는 전체 PASS를 만들지 않는다. 해당 실제 통합 실행은 Worker targeted로 호출하지 않는다.
- Manager의 실제 Windows 회귀에서는 기존 모체 강제 종료 사례로 WSL 자식 종료·전용 복사본 제거를 확인하고, 전체 deadline 근처의 정상/실패 자료와 두 단계 범위를 구분한다. Linux unit/mock 결과나 테스트의 존재를 Windows 실행 성공으로 기록하지 않는다. 기존 Windows native 실패의 실패 exit code, Linux 전체 필수 compile, Agent guard와 다른 실행 보존 회귀를 유지한다.

## Progress

- [x] `bbce8cd25cdaa34a547b736cfc2f5d20f96a2a6d`에서 launcher·두 단계/환경/정리 테스트와 R-065/R-067의 전체 시간 상한을 정적으로 대조했다. 직접 구현된 pipeline은 재구현하지 않으며 준비/단계 전환/정리에 남은 예산 누락을 확인했다. 구현·테스트·verify는 실행하지 않았다.
- [x] `2219730c40def01f6c38ed1d68b8c35fa44c29e7`에서 native의 동일 clock/원래 timeout 전달과 이중 차감 방지 회귀가 직접 반영됐음을 정적으로 확인했다. 준비·실행 전 만료·정리의 남은 범위와 기존 수행 기록은 보존하며 Linux/Windows 테스트 성공을 뜻하지 않는다.
- [ ] 준비·두 단계·정리의 공통 시간 상한과 실패 처리 연결
- [ ] 최소 targeted unit과 기존 실제 OS 회귀·문서 정합성 대조

## Decision Log

- 이번 delta의 상하위 대기 계약은 G-309에서 구현하고 이 계획은 그 결과를 선행 소비한다. 제공 snapshot의 원본 hash와 기존 Windows 준비/정리 미충족 목표를 대조했으며 ID·경로·분류·기존 수행 기록을 보존한다. 새 통합 경계를 이유로 Windows 시간 상한·정리 조건을 축소하거나 소급 검증 성공을 만들지 않는다.
- 현재 delta의 native 이중 차감 교정은 재사용하고 남은 준비·실행 전 만료·정리 경계만 구현한다. 시작한 ID·경로·기존 수행 기록과 실제 OS 검증 조건은 보존한다. 같은 clock에 남은 timeout을 중복 적용하거나 단일 unit 회귀로 전체 시간 상한 완료를 선언하지 않는다.

- 2026-10-16 delta: R-091과 `1f8b97e6c04868281cd23672396f1fd3d47ee98e`의 직접 구현된 clock·대기 누계·lifecycle receipt를 소비하도록 개정했다. 과거 전체 wall deadline 표현은 확인된 순번 대기를 제외한 실행 예산으로 대체한다. 남은 준비/정리 누락과 기존 수행 기록·ID·dependency·분류는 보존하며 테스트·실제 OS 성공은 기록하지 않는다.

- 기존 두 단계 자체 검증을 정확히 실행하는 보완이며 새 검증 정책·OS 지원·예약 체계를 도입하지 않는다. G-187의 실측은 이 구현을 소비하고 G-248의 진단·G-253의 임시 경로 선택과 별도 목표를 유지한다. 최신 입력 재평가 과정에서 발견했으며 이전 입력의 추가 계획 없음 결과를 최신 코드의 성공 근거로 재사용하지 않는다.
- complexity medium: 기존 launcher·timeout/종료 helper의 명확한 경계에 준비와 정리의 남은 시간을 연결하는 작업이다. risk high: 잘못된 만료/종료 판단은 살아 있는 WSL 자식의 자료를 제거하거나 필수 검증·정리 전에 성공을 반환할 수 있다. 기존 소유 실행·receipt·종료 불명 진단을 보존하고 임의 정리를 거부하며, 만료 위치별 다음 호출 차단과 실제 OS 종료 회귀로 확인한다. 외부 프로세스 종료의 효과를 코드 rollback으로 복구할 수 없으므로 소유권 확인과 다른 실행 보존을 유지한다.
- priority high: 단독 실행의 무제한 준비와 전체 시간 상한 이후 성공 가능성을 차단하는 필수 검증 보완이다. Model·Effort는 Manager의 명시 설정을 따른다. 사용자 결정·SQLite 실행 상태·G-259의 작업물과 이력은 변경하지 않는다.
