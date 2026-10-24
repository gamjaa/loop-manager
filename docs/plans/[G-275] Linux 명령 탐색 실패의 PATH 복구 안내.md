---
id: G-275
title: Linux 명령 탐색 실패의 PATH 복구 안내
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-089] setup PATH 저장과 bootstrap 전 실행 환경 적용.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

Linux에서 필요한 CLI 또는 실제 실행 의존성을 찾지 못한 오류에 설치 경로 확인·정상 터미널의 setup 재실행 안내를 연결한다. 직접 반영된 PATH 저장·source 진입 적용·자식 상속은 재사용한다. 현재 손상 캐시는 setup 복구를 안내하지만 등록·provider 탐색 오류와 bootstrap/직접 subprocess의 실행 파일 누락은 기존 오류만 출력하며, Node 등 실제 의존 실행 실패에도 PATH 복구 안내가 연결되지 않는다.

## Acceptance Criteria

- Linux source bootstrap의 Git 실행 파일 누락, 등록의 Git·gh/선택 provider 탐색 실패 및 Agent 사전 확인·실제 호출의 확인된 명령 탐색 실패에서 기존 원인과 함께 같은 실행 계정의 정상 터미널에서 설치 경로를 확인하고 해당 source의 `bash scripts/setup.sh`를 재실행하는 조치를 안내한다. cwd·별도 설정·state_dir 또는 runtime 경로를 캐시 원본으로 안내하지 않는다.
- 선택 CLI가 실행됐지만 필요한 Node 등 의존 실행 파일을 찾지 못한 확인 가능한 실패에도 기존 비밀값 제거를 유지한 원래 출력·종료 결과를 보존해 안내한다. 전체 환경·인증값을 추가 출력하지 않는다. 일반 명령 실패, session 부재, 인증 실패·권한 거부를 탐색 실패로 재분류하지 않으며 모든 provider에 Node 설치를 새 필수 조건으로 요구하지 않는다.
- 캐시 부재와 사라진 저장 디렉터리 모두 현재 PATH·시스템 후보의 기존 정리·우선순위를 유지한다. 필요한 도구를 찾을 수 있을 때는 추가 오류 없이 진행하며 손상 캐시는 첫 외부 명령 전에 계속 중단한다. 오류 안내는 캐시를 자동 변경하거나 로그인·다른 provider/버전 실행을 시작하지 않는다.
- 임시 source·설정·명령 fixture에서 실패 뒤 정상 setup 재실행과 다음 source 진입으로 탐색이 회복되는 것을 확인한다. 진행 중 실행의 PATH는 바뀌지 않고 다음 실행에만 새 캐시가 적용된다. 기존 interpreter·인자·lock·종료 코드와 작업·session·실패 기록, 인증 환경 필터·설정 홈·격리·권한 경계를 유지한다.
- README·운영 안내와 관련 PRODUCT·ARCHITECTURE 설명은 실제 진단·복구 경계와 일치한다. macOS·Windows 동작·지원 범위와 기존 cron 설정은 바꾸지 않는다.

## Implementation

`loop_manager_internal/config/execution_path.py`의 Linux 판정·source 캐시 규칙과 기존 오류 전달 책임을 재사용한다. source 진입, `runtime/registration_service.py`의 필수 명령 검사, `agents/agent_profiles.py`의 provider 검사와 실제 명령 실패를 사용자에게 전달하는 경계에서 필요한 안내를 결합한다. 공통 명령 외의 직접 subprocess와 provider 출력 처리도 읽어 실제 누락 관측이 있는 경로만 다룬다. 불명확한 출력에 광범위한 문자열 규칙을 적용하거나 새 실행·재시도·차단 정책을 만들지 않는다.

이미 구현된 PATH 저장·원자 교체·bootstrap 적용을 재작성하지 않는다. source 위치는 기존 runtime-source 문맥을 사용하고 이를 알 수 없는 오류에서는 경로를 추측하지 않는다. 기존 실패 진단과 복구 안내의 책임을 유지하며 별도 PATH 관리 도구·설정값이나 외부 서비스를 추가하지 않는다. 관련 문서의 현재 동작·실패 조치를 같은 PR에서 반영하고 ARCHITECTURE의 실행 환경 절에 있는 helper 경로도 실제 `loop_manager_internal/config/execution_path.py`로 교정한다. 자체 setup 입력 목록 누락은 G-241의 기존 입력 완전성 목표로 처리한다.

## Validation

- Worker는 `tests/test_execution_path.py`와 기존 bootstrap·provider·등록 오류 fixture 중 변경한 경계를 확인하는 최소 targeted unit만 실행한다. 외부 명령을 모의해 Git·gh·선택 provider 부재, 캐시 없음/저장 경로 소실, 명령이 발견되는 정상 경로, Node 의존 실행 실패와 일반/session/인증 실패의 구별을 확인한다. 실제 신규·재개·재시도 호출의 모의 오류에도 같은 조치와 기존 결과·이력이 유지되는지 검사한다.
- 복구는 임시 캐시·CLI fixture와 모의 setup 준비 단계로 확인한다. 실제 Linux 명령 탐색이 필요한 효과는 최소 PATH와 임시 실행 파일만 사용하고 실제 계정·로그인·Agent 및 `loop_manager.py run`을 실행하지 않는다. 기존 실행 snapshot·다음 source 실행의 갱신과 실패 캐시 보존 회귀를 재사용한다.
- Manager 전체 verify와 Reviewer는 기존 오류 분류·종료/lock·환경 필터·일반 소유권/복구 경계, 다른 플랫폼의 무변경 및 setup 안내에 의한 자동 실행 확대가 없는 것을 확인한다. 실제 cron 운영 검증을 하지 않았다면 그 환경·범위 한계를 기록하며 fixture를 운영 성공으로 표시하지 않는다.

## Progress

- [x] 2026-10-13 `041e6ee8ba3145eae988e99fdd4055cb743f08d1`의 R-089 직접 구현·`tests/test_execution_path.py`·README·운영 안내와 bootstrap/등록/provider/명령 오류 전달을 정적으로 대조했다. 손상 캐시 복구와 PATH 기본 동작은 구현돼 있으며 명령 탐색 실패의 실행 시 복구 안내가 남아 있다. 구현·테스트·verify는 실행하지 않았다.
- [x] 2026-10-24 Linux source bootstrap·등록·Agent preflight/호출의 확인된 CLI·의존 명령 탐색 실패에 setup 복구 안내를 연결했다. provider 원문과 exit code·비밀값 제거, 기존 인증/session/권한 분류·재시도를 보존하고 README·운영 안내·PRODUCT·ARCHITECTURE를 실제 동작에 맞췄다. 관리 rebase 후 HEAD `000ad57c1049438bf7c985bfcd4038e7c15d9b41`의 변경 전체를 다시 확인하고 관련 targeted unit 12개를 새로 실행해 통과했다.
- [ ] Manager 검증·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 경계가 정해진 source bootstrap·등록·provider 오류 전달을 연결하고 의존 실행 실패와 다른 실패를 구별한다. PATH 설계·저장 계약과 복구 상태 전이를 새로 정하지 않는다.
- risk medium: 오판한 안내는 인증/session 오류의 복구를 잘못 유도하거나 실패 내용을 가릴 수 있다. 기존 원인·종료/분류·기록을 보존한 메시지 변경과 좁은 fixture로 검증하며 자동 실행·캐시 변경을 하지 않는다. 잘못된 메시지는 코드와 안내 교정으로 복구할 수 있다.
- priority normal: R-089의 필수 실패 복구 완결 작업이며 안전한 실행 경계나 새 OS 지원을 변경하지 않는다. 직접 구현에 소급 계획을 만들지 않고 남은 안내만 계획한다. G-189의 전체 차단 감사와 G-241의 입력 증명을 재구현하지 않는다.
