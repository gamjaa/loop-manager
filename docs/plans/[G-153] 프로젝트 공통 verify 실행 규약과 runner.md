---
id: G-153
title: 프로젝트 공통 verify 실행 규약과 runner
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

로컬 작업은 공통 verify 규약·runner와 Loop Manager 자체 unittest 연결을 우선 제공한다. 자원 배분·예약·프로세스 정리·진단을 재사용 가능한 경계로 분리하고 테스트 수집·결과 의미·격리는 adapter 경계에 둔다. 비Python adapter, 외부 프로젝트 연결과 언어 간 실제 검증은 서버의 후속 자동 작업으로 진행한다.

이 문서는 `docs/plans/`의 활성 ExecPlan이다. 원격 기준 branch에 게시되면 Loop Manager가 catalog 입력으로 읽으며 실행 상태는 SQLite가 관리한다. 사용자가 정한 로컬 기반 구현 범위와 서버 후속 범위를 구분하고, 사용자 Merge 뒤 서버 Planner가 최신 원격 코드·catalog에서 남은 작업만 계획하게 한다. 기존 미충족 구현을 검사 없이 완료로 표시하지 않고, 수동 G-148~150·G-152를 SQLite dependency로 사용하지 않는다.

## Acceptance Criteria

- R-067의 공통 기반에 해당하는 선택적 도입·규약 검증·공통 배정·결과 및 복구 조건을 Loop Manager 자체 연결에서 만족한다. R-067 전체 완료에는 서버 후속 adapter·다른 언어의 실제 검증이 필요하다.
- 공통 core와 unittest adapter가 분리되고 Loop Manager 자체가 그 core를 사용한다. 다른 도구를 연결하는 명시적 규약·지원 metadata·적합성 검증 경계를 제공하되 로컬에서 비Python adapter나 외부 프로젝트의 실제 연결을 구현하지 않는다.
- 미연결 프로젝트는 기존 실행 경로를 유지하고, 연결 오류는 실패로 드러난다. 단독 실행·필수 검증·fixture 수명·timeout·자식 종료 확인·실패 피드백이 보존된다.
- unittest adapter 적합성 검증과 역할별 지침 전달 경계가 제공된다. 수행한 검증과 서버에 남긴 실제 OS·언어 간·성능 검증을 구분하고, 미실행 결과를 성공으로 표시하지 않는다.

## Implementation

### 1. 기준선 확인과 규약 확정

- PR [#138](https://github.com/gamjaa/loop-manager/pull/138)의 실제 Merge·검증 상태와 R-065의 남은 작업을 확인한다. Merge 전 코드를 현재 제품 보장이나 이미 통과한 기반으로 취급하지 않는다. Merge 후 `verification/coordination.py`, `resources.py`, `timing.py`, `manager_adapter.py`, `runner.py`, `manifest.py`와 `execution/result_validation.py`의 경계를 다시 확인한다.
- G-135 및 비종료 계획의 목표·실행 이력·dependency를 변경하지 않는다. 실제로 겹치는 종료·fixture·지침 변경은 최신 catalog의 소유권과 Merge 상태를 확인해 실행 계획별로 필요한 선행 조건만 설정한다. 전체 계획을 무조건 기존 리팩터링 완료 뒤로 미루지 않는다.
- 첫 연결 선언은 `scripts/verify.json`의 schema version·adapter 식별자·프로젝트 테스트 환경·수집 옵션·순서 있는 검증 단계로 한정한다. 실제 테스트 ID와 실행 단위는 현재 revision에서 수집하며 정적 선언을 수집 결과로 대체하지 않는다.
- 단위는 고유 ID·argv 배열·worktree 안의 cwd·직렬 또는 독립 실행 조건을 제공한다. core가 명령 문자열을 shell로 해석하지 않는다. 단계 순서는 고정하고 범용 dependency DAG·resource lock 설정은 만들지 않는다.
- 버전 있는 manifest·이벤트·결과에 run/단위/테스트 identity와 원래 도구의 결과 의미를 남긴다. 테스트 adapter는 예상 범위와 결과를 대조하고 compile·build 명령은 필수 단계 종료 코드로 판정한다. adapter registry는 필요한 정적 등록으로 시작한다.
- 비Python adapter의 도구 선택·구현은 서버 후속 계획에 둔다. 공통 계약에는 도구·지원 버전·수집 및 reporter·프로세스 단위 실행과 내부 병렬 제어의 지원 근거를 표현할 수 있어야 한다. 자유 형식 stdout 추측이나 제어 불가능한 실행의 병렬 지원 선언을 허용하지 않는다.
- 공통 계약은 process argv·입출력·identity·manifest·이벤트·결과이고, assertion·fixture·테스트 소스 형식은 도구별로 유지한다. 수집 시 확정하는 범위와 실행 중 등록되는 테스트 ID의 관계, native reporter의 결과 완결성·skip/오류 의미를 adapter별로 검증한다. 필수 범위 확인을 단순 종료 코드로 대체하지 않는다.

### 2. 로컬: 공통 실행 core와 Loop Manager unittest 연결

- PR #138에서 필요한 자원 계산·예약·실행·정리·진단을 재사용한다. 기존 unittest manifest·fixture·결과 해석을 adapter로 이동하고 core가 unittest 전용 타입이나 Loop Manager 자체 compile 경로에 의존하지 않게 한다. 불필요한 전면 재작성·새 서비스는 추가하지 않는다.
- Manager가 명시적 연결 선언을 검증해 선택된 worktree의 verify에 버전·run identity·runner 위치를 전달한다. 현재 내부 파일 존재 기반 감지를 이 경계로 교체한다. 미연결 경로는 유지하고 잘못된 opt-in을 기존 경로로 우회하지 않는다.
- Manager가 제공하는 공통 진입점을 Windows·Linux verify launcher에서 호출하게 한다. runner는 Manager runtime, 테스트는 프로젝트 interpreter와 의존성을 사용한다. 프로젝트에 내부 패키지·설정·비밀값을 복사하지 않는다. 단독 실행에도 같은 진입점과 명시적인 위치·버전 확인 방법을 제공하고 새 PATH 추측이나 자동 설치를 하지 않는다.
- 현재 `verify_parallelism` 검증과 Manager 인스턴스 coordinator를 연결 프로젝트가 공유한다. 예약에는 run·단위·프로세스 identity를 결합하고 실제 사용량과 미반영 예약을 함께 계산한다. 내부 병렬은 초기 adapter에서 끄며, 직렬 단계도 자원 대기·전체 timeout·진단에서 빠지지 않게 한다.
- `auto`·`1`은 같은 수집·필수 단계·결과 집계 경로를 사용한다. 현재 unittest의 `load_tests`, module/class fixture, skip·expected failure·unexpected success·subtest와 compile 범위를 보존한다.
- adapter 추가 경계와 unittest의 공식적으로 확인된 지원 기능·버전을 제공한다. 두 번째 adapter를 위한 임시 구현·가짜 지원 metadata는 만들지 않는다. 서버에서 같은 자원 계산·예약·종료 core를 재사용할 수 있게 도구별 내부 동작을 core에서 분리한다.
- timeout·취소·시작/통신/결과 오류·중단에서 기존 자식 종료 확인과 예약 보존을 유지한다. 조율 부재의 단독 실행은 로컬 관측을 사용하고 다른 Manager·단독 runner와 예약을 공유한다고 표시하지 않는다.

### 3. 로컬: 적합성 검증 경계와 역할별 지침

- Loop Manager unittest의 현재 수집·fixture·compile 계약으로 실제 사용 경로를 연결하고, adapter 규약의 정상·실패·중단 결과를 좁은 unit test로 검증한다. 외부 저장소·비Python 예제는 로컬 범위에 포함하지 않는다. 합성 단위·결과만으로 언어 독립성·실제 OS 격리를 검증했다고 표시하지 않는다.
- 적합성 자료는 프로젝트 runtime·의존성·수집·독립 fixture·직렬 단계·필수 compile 또는 명령 단계·Manager/단독 호출을 확인할 수 있게 한다. 일반 명령 단계 결과를 테스트별 결과로 표시하지 않는다.
- 적합성 검증은 manifest 누락·중복·버전 오류, 테스트 및 fixture 결과 의미, 내부 병렬 예산, 종료 코드·시작 실패·강제 중단·부분 결과·정리 비용을 다룬다. 새 adapter의 추가 방법과 지원하지 않는 도구의 기존 경로 유지를 설명한다.
- 공통 AGENTS의 권한·검증 규칙을 유지하고 역할별 프롬프트에 명시적 adapter·지원 버전·참조 문서를 연결한다. Worker에는 수집·격리·Agent mock·진단 보존의 작성 지침, Reviewer에는 계약·범위·격리의 검토 기준, Planner에는 adapter 도입·연결 작업의 판단 기준만 제공한다. 역할·언어별 AGENTS 사본이나 상세 지침 전체의 공통 주입을 만들지 않는다. 기존 작업별 입력·snapshot 버전 보존을 사용하고 진행 세션에 최신 지침을 강제로 섞지 않는다.
- 테스트마다 시간 기록·예약 구현을 요구하지 않는다. adapter가 수집하는 새 테스트를 작성하고, 공유 fixture·순서 의존을 발견하면 직렬 선언 또는 해당 작업 범위의 격리를 선택한다. 새 adapter·기존 프로젝트 연결은 필요할 때 그 프로젝트의 계획으로 진행한다.

### 4. 서버 인계: 후속 adapter·실제 검증·프로젝트 연결

- 로컬 기반 PR은 R-067과 G-153을 활성 위치에 유지한다. PR 본문에 구현한 계약·확인된 지원 범위·targeted 결과와 남은 OS·성능·언어 간 검증을 기록한다. 완료 판정과 Requirement eligibility·Merge 후 ExecPlan archive는 Loop Manager의 기존 절차를 따르며, 로컬에서 서버 SQLite에 완료·dependency 상태를 만들어 넣지 않는다.
- 기반 PR이 Merge된 뒤 서버의 Loop Manager Planner가 현재 코드와 R-067의 미충족 부분을 대조해 후속 계획을 만든다. 기존 사용 환경의 비Python 테스트 도구 하나를 공식 수집·reporter·지원 버전·내부 병렬 제어 가능성으로 선정하고 작은 adapter와 독립 예제를 구현한다. 모든 도구 adapter를 한꺼번에 만들지 않는다.
- Manager가 Agent 밖에서 전체 verify와 예제의 실제 프로세스·동시 실행·timeout·취소 검증을 수행한다. Windows·Linux에서 한 verify만 정리되고 다른 실행이 보존되는지 확인한다. 실패는 동일 Worker 세션의 해당 구현 경계에 전달한다.
- 같은 환경·revision·Python/Git 또는 테스트 도구 버전·검증 범위에서 직렬/auto, 단일/동시 verify의 결과·시간·메모리·부하·배정·예약을 비교한다. 병렬화가 이득 없는 작은 작업도 포함하며 개선율을 미리 확정하지 않는다.
- 로컬 기반 PR은 구현한 책임·사용법만 PRODUCT·ARCHITECTURE·README에 반영한다. 서버 후속 PR은 실제 지원 도구·검증 근거를 갱신한다. Review와 실제 사용자 Merge를 단계별 완료 근거로 삼으며 다른 언어의 실제 연결 없이 R-067을 완료·archive하지 않는다.
- 다른 프로젝트의 도입은 서버가 프로젝트별 자동 계획·구현으로 진행한다. 각 Planner는 자신의 프로젝트 명세·기존 도구·verify와 권한 범위를 읽으므로 Loop Manager의 R-067 게시만으로 다른 저장소의 목표가 자동 등록된다고 간주하지 않는다. 대상 프로젝트의 자동 입력에 연결 목표·규약 참조를 반영한 뒤 해당 Planner가 현재 구조에 맞게 계획하게 한다. 이 목표 게시·연결은 서버 후속 범위이며 로컬에서 외부 프로젝트 파일을 수정하지 않는다.
- 첫 도입은 해당 프로젝트의 기존 범위·fixture·필수 단계 보존을 확인하는 연결 계획 한 번이면 되며 이후 새 테스트마다 별도 병렬화 계획을 만들지 않는다. 미지원 도구는 기존 verify를 유지하고 필요한 adapter 작업을 별도 계획한다.

## Validation

- Worker: 변경한 규약 파서·adapter·집계·자원 계산·예약/해제·명시적 opt-in·프로젝트 환경 전달·실패 진단·지침 버전 경계의 최소 unit case만 실행한다. 새 프로토콜 검증은 실제 정상·실패·불완전 결과 계약을 확인하며 구현을 그대로 복제한 테스트를 만들지 않는다.
- Manager: 로컬 기반 후보의 전체 필수 검증은 Agent 밖에서 실행한다. 서버 후속에서는 서로 다른 두 언어의 실제 예제로 직렬/병렬·동시 실행·각 도구의 결과 의미·독립 Git/SQLite/경로·프로세스 시작 실패·timeout·취소·결과/통신 손상·다른 verify 보존을 확인한다. 실제 도구·OS 검증과 실측이 없으면 fake/mock 통과를 그 증거로 사용하지 않는다.
- Reviewer: 전달된 unit·전체 verify·실측 결과와 diff로 범위·fixture·결과 의미·종료 확인·비밀값 보호·지침 버전 보존을 검토하며 파일을 수정하거나 검증을 재실행하지 않는다.

## Progress

- 2026-10-04: 현재 main 문서·규칙, PR #138 브랜치의 runner와 예약·manifest·verify 진입 경계를 읽고 계획을 작성했다. 코드 구현, 테스트, 전체 verify, 실제 비교, 외부 프로젝트 변경, commit·push는 하지 않았다.
- 2026-10-04: 역할별 지침 전달과 언어·프레임워크 차이에 관한 사용자 의견을 반영해 최소 지원을 서로 다른 두 언어의 실제 adapter·예제로 보강했다. 상세 작성 지침은 Worker 역할 입력으로 분리한다. 문서 개정이며 실제 adapter 선택·구현·실행의 완료 근거가 아니다.
- 2026-10-04: 사용자 결정에 따라 G-153의 로컬 범위는 공통 기반·Loop Manager unittest 지원으로 제한하고, 두 번째 adapter·다른 프로젝트의 목표 게시와 연결·언어 간 검증은 서버 자동 후속으로 분리했다. PR #138은 조회 시 OPEN·일반 PR이며 Merge되지 않았다. 이번 범위 개정에서 코드 구현·검증 실행·commit·push는 하지 않았다.
- 2026-10-04: 사용자 요청에 따라 G-153을 `docs/plans/manual/`에서 `docs/plans/`로 이동하고 활성 ExecPlan으로 정리했다. 구현 범위·ID·sources·dependency는 유지하며 실행 상태를 파일에 추가하지 않았다.
- 2026-10-04: 사용자가 로컬 작업 전체를 기존 PR에 반영하도록 요청해 R-067과 G-153을 PR #138 브랜치로 옮겨 게시한다. 이 반영은 공통 규약·runner의 요구사항과 계획 문서이며 G-153 코드 구현·전체 verify·Review·Merge의 완료를 뜻하지 않는다.

## Decision Log

- 사용자는 프로젝트마다 자원 조율을 구현하는 대신 Loop Manager 기능으로 공통 규약을 제공하고 그 규약에 맞춰 다른 프로젝트도 사용하는 계획을 요청했다.
- R-065의 기존 Non-Goal을 변경하지 않고 R-067에서 선택적 연결을 정의한다. R-058·R-065의 안전·검증 원칙은 이어받지만 기존 수동 기록을 관리 대상 완료 증거로 승격하지 않는다.
- R-067 전체 범위는 공통 core + unittest·비Python 도구의 최소 두 adapter + 적합성 예제다. G-153의 로컬 단계는 core와 자체 unittest를 먼저 제공한다. 언어 독립성은 서버 후속의 실제 다른 도구 연결로 확인한다. 도구별 adapter는 결과 의미가 달라 필요한 경계이며 범용 plugin framework·자동 도구 판별·일괄 마이그레이션은 도입하지 않는다.
- 프로젝트 연결 선언은 명시적 opt-in이고, 연결된 규약의 오류는 실패다. 미지원 도구의 미연결 프로젝트는 기존 verify를 유지한다. 실제 Agent 차단은 지원 adapter의 실행 경계에서 검증하며 임의 언어의 모든 subprocess를 기존 Python guard 하나로 차단할 수 있다고 간주하지 않는다.
- 현재 합의는 기능·규약 계획이며 전체 코드 구현이나 외부 저장소 변경의 완료를 뜻하지 않는다. G-153은 활성 ExecPlan으로 관리하며 서버 자동 입력은 원격 기준 branch에 게시된 활성 Requirement와 ExecPlan catalog다.
