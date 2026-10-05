---
id: G-191
title: Claude 내장 쓰기 경계와 자동 승인 검토 검증
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

Claude의 관리 호출을 provider 이름만으로 일괄 거부하는 현재 경계를, 지원 환경에서 실제 내장 쓰기 제한과 자동 승인 검토를 확인하는 경계로 완성한다. 확인 불가 환경의 안전 중단은 유지한다. R-023의 provider 검증 범위만 다루며 원격 bootstrap과 공통 지침 주입은 기존 담당 계획에 남긴다.

## Acceptance Criteria

- 지원되는 Claude 환경의 Planner·Worker·Reviewer·유지보수 역할에서 신규·재개·기존 session-unavailable fallback이 같은 역할별 정책을 적용한다. 쓰기 역할의 허용 파일 쓰기는 성공하고 source checkout·다른 worktree·shared Git metadata 쓰기는 거부되며 Reviewer의 선택 workspace 쓰기도 거부된다.
- 명령 sandbox와 Edit·Write·NotebookEdit 등 파일 도구에 동일한 경계를 적용한다. linked worktree의 resolved shared Git·worktree Git 경로, 상속 설정의 추가 writable 경로 및 넓은 임시 경로가 허용 범위를 확대하지 않는다. 보호 경로 검증은 사용자 파일·index·Git 상태를 변경하지 않는다.
- 지원 모델·계정·CLI의 자동 승인 검토가 유지되고 쓰기 경계 확대·sandbox 밖 재실행은 거부된다. 설정 문자열이나 프롬프트만으로 효력을 인정하거나 acceptEdits·일괄 명령 승인·자동 검토 비활성화로 대체하지 않는다.
- 확인·거부 근거는 실제 CLI·환경·모델 지원·역할·canonical workspace·repository·유효 설정·session·호출 identity에 결합된다. 재개, CLI·설정 변경 및 중단 후 호출은 필요한 근거를 다시 확인하며 증거 없는 과거 실행의 엄격한 scope 판정은 유지한다.
- Claude native Windows, 미지원 모델·계정·CLI, 정책 충돌·무시, 경계 초기화·증거 저장 실패 및 실제 효력을 증명할 수 없는 경우 호출 전에 진단하고 상태·작업물·session·과거 거부·retry 이력을 보존한다. 더 넓은 권한의 재실행이나 일괄 차단 해제를 하지 않는다. 기록에 비밀값과 source 로컬 파일 내용을 포함하지 않는다.
- Codex의 R-050 직접 exec·내장 도구 정책 확인, 기존 session·사용량·timeout·중단 처리, 역할별 파일 검사와 Worker 이후 Reviewer 흐름이 유지된다. Reviewer의 읽기 전용 조사 권한과 프롬프트의 테스트·verify 재실행 금지는 별개로 유지한다.

## Implementation

- docs/ARCHITECTURE.md의 `R-023: workspace 쓰기 제한과 자동 승인 검토 (구현 예정)` 설계를 적용한다. 기존 provider adapter의 Claude permission·sandbox 설정, isolation evidence와 invocation 저장 경계를 재사용하며 필요 부분만 provider 책임으로 분리한다. Codex 전용 launch 값을 Claude 검증 성공으로 대용하거나 범용 격리 framework를 추가하지 않는다.
- 설치 CLI의 공식 지원 기능·병합 우선순위·모델/계정 조건과 실제 도구 경계를 확인한다. 정책 작성, 효력 확인, 증거 영속화와 실제 호출 직전 재검증을 같은 호출에 결합한다. 기존 provider 내부 경계를 확인할 수 없으면 verified를 기록하지 않고 원인별 미지원으로 보존한다.
- 공유 Git 및 source 보호와 소유 임시 경로 판정에는 기존 canonical worktree·repository 검증을 사용한다. 실제 파일 도구와 명령의 허용·거부 확인은 보호 파일을 변경하지 않도록 설계한다. 새로운 외부 격리 infrastructure, OS 전환, 별도 승인 서비스나 루트 항목은 도입하지 않는다.
- session 재개·fallback의 기존 recovery context와 결과·사용량·중단 계약을 유지한다. AGENTS.md와 docs/PLANS.md의 문서·역할 권한을 따르며 정책 확인 실패를 문서 교정이나 무조건 새 session 시작으로 우회하지 않는다.

## Validation

- Worker는 tests/test_agent_write_isolation.py·tests/test_provider_runtime.py·tests/test_native_platform_agents.py의 관련 fixture를 재사용해 수정 경계의 최소 unit case만 작성·실행한다. 모든 역할의 신규·재개·fallback, 명령/파일 도구 경계, shared Git deny, Reviewer 읽기 전용, 자동 검토 유지와 확대 거부, 상속 설정 충돌·무시, CLI/모델/계정 미지원, native Windows 및 증거 저장·중단·재확인 실패를 확인한다. 모델·provider subprocess는 test double로 대체한다.
- Manager 전체 verify는 Claude 검증 성공과 거부가 실제 managed invocation·scope 저장·session 복구로 연결되는 회귀 및 Codex 직접 exec, process lock, remote-only 작업 입력, scope 안전 차단·후보 Review를 유지한다. 자동 테스트는 실제 Agent를 실행하지 않는다. test double의 인자 확인을 실제 provider 효력 증거로 취급하지 않으며 실제 호출의 격리 확인이 불가능한 환경은 성공으로 기록하지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 32e496c에서 prepare_isolation의 non-Codex 일괄 거부와 run_claude의 관리 호출 경계를 대조했다. 기존 테스트는 Linux의 모든 역할·신규·재개에서 Claude 거부만 확인하며 supported 호출의 실제 경계 확인은 없다. 비종료 G-147은 공통 지침 전달·버전 보존으로 이 범위를 포함하지 않는다. 기존 파일 최대 ID는 G-190이다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 지원 여부를 실제로 확인하지 않는 provider 단위 일괄 거부는 안전한 실패 동작이지만 R-023의 지원 환경 검증 완료 근거가 아니다. 동일한 보안 기준 안에서 확인 가능한 호출만 허용하므로 새 제품 목표나 권한 확대가 아니다.
- provider 효력·역할·session·설정 병합이 연결돼 complexity high, 잘못된 확인의 경계 밖 쓰기 영향으로 risk high다. 설정된 Claude 작업의 무인 실행을 복구하는 필수 작업으로 priority high다.
- 원격 실행 gate의 G-161과 공통 지침 주입의 G-147은 이 구현의 선행 조건이 아니다. 기존 성공 호출 계약을 유지해 독립적으로 검증·Merge하며, 전체 차단·재개 감사 G-189는 이 변경 뒤 수행한다.
