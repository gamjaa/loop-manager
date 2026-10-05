---
id: G-162
title: Windows Linux 전체 검증 회귀 수정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-058] Test Timing and Agent-Free Test Execution.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

운영체제 호환 변경 이후 Linux 테스트 오류를 수정하고 Windows와 Linux의 전체 검증을 확인한다.

## Acceptance Criteria

- Windows와 Linux에서 동일한 전체 검증 범위를 유지하고 성공한다.
- 플랫폼 차이로 인한 실패를 기존 실행·테스트 경계에서 수정한다.
- 실제 Agent 호출 금지와 프로세스 정리·실패 종료 코드 보장을 유지한다.

## Implementation

1. 두 운영체제의 전체 검증 실패와 로그를 조사한다.
2. 재현된 원인을 수정하고 필요한 회귀 테스트를 보강한다.
3. 전체 검증 결과를 기록하고 일반 PR을 게시한다.

## Validation

- 변경 부분의 targeted unittest를 실행한다.
- 사용자가 직접 요청한 Windows·Linux 전체 테스트 확인을 수동 작업으로 수행한다.
- 운영 timeout 기본값과 검증 범위를 유지하며 실패·skip 및 미검증 환경을 기록한다. 사용자가 승인한 이번 수동 검증의 timeout 예외는 Decision Log를 따른다.

## Progress

- 제공된 Linux 실패는 G-143 작업 공간에서 Windows launcher 호출을 세 개로 센 테스트 두 개다. 최신 main `035f099`에는 단일 호출 수정이 이미 있으며 WSL Ubuntu Python 3.12 전체 검증에서 성공 2,427개·플랫폼 skip 2개를 확인했다.
- Windows 첫 전체 실행에서 경로 fixture, CRLF 바이트 전제, SQLite 연결 정리 순서와 실행 권한 fixture의 결함을 확인했다. 600초까지 1,435개 결과를 기록한 뒤 기존 종료 경계로 중단했다.
- Git 바이너리 stdout과 stderr를 분리해 경고의 파서 유입을 막고 실패·잠금 진단은 유지했다. scheduler와 CLI 명령이 소유한 SQLite 연결은 transaction 처리 뒤 명시적으로 닫는다. 문서 등록의 공통 파일·미수정 템플릿 비교는 checkout 줄바꿈 차이를 허용하며 Agent 승인 hash는 원격 blob 기준을 유지한다.
- 관련 fixture는 native/canonical 경로, LF 또는 명시적 Git 변환 정책, DB 종료 후 임시 디렉터리 정리, Git index 실행 비트 및 기존 symlink 권한 skip 경계를 사용하도록 수정했다.
- Windows 경로 복구·문서 등록·Git replay·유지보수 publication의 targeted 회귀 검증이 통과했다. 문서 줄바꿈과 Git stderr 분리, scheduler 연결 종료를 직접 확인하는 검증을 보강했다.
- Windows BLOCKED 복구의 원자적 파일 교체는 파일 fsync를 유지하고 POSIX에서만 디렉터리 fsync를 실행한다. Manager fetch는 최신 Git의 origin/HEAD 자동 변경을 끈다. 두 문제는 실제 복구 테스트로 확인한다.
- 수정본 Linux 전체 검증은 자동 병렬 실행에서 2,431개(성공 2,429개, 플랫폼 skip 2개)와 compile을 통과했다. 이후 fetch의 origin/HEAD 억제를 모든 Manager fetch 경로에 확장했으며 최종 snapshot을 다시 검증한다.
- Windows 30분 전체 실행은 복구 모듈을 완료하기 전에 timeout됐다. 확인된 실패는 수정 후 targeted 검증으로 확인했으며 전체 성공은 아직 확인 중이다.

## Decision Log

- 자동 Worker 실행이 아닌 사용자의 직접 수정·전체 테스트·일반 PR 요청이다. 이번 수동 검증에서 두 OS의 전체 verify를 실행한다.
- 기존 결정에 따라 GitHub Actions를 추가하지 않고 WSL Ubuntu의 독립 Linux snapshot을 사용한다.
- 사용자가 이번 수동 검증에만 30분 제한을 승인했다. 운영 설정·기본 600초는 변경하지 않으며 기존 `run_verify_process`의 Job/process-tree 종료 경계와 전체 runner의 discovery·테스트·compile을 그대로 사용한다.
