---
id: G-191
title: Claude 내장 쓰기 경계와 자동 승인 검토 검증
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

Claude의 관리 호출을 provider 이름만으로 일괄 거부하는 현재 경계를, 기존 내장 기능과 유효 정책에 따라 역할별 쓰기 제한·자동 승인 검토를 확인하는 경계로 완성한다. 목표와 공식 웹 문서·모의 테스트를 사용하는 개발 방식은 확정됐으며, 실행 중 managed 정책 갱신에도 제한을 유지할 native 경로의 조사 미완료는 남은 구현 조사다. 이것만으로 조직 관리 환경 한정이나 외부 격리가 필수라고 판단하거나 지원 구현 전체를 새로운 사용자 정책 판단 대기로 바꾸지 않는다. 실제 선택 호출의 미지원·정책 충돌·제한 적용 실패는 근거를 보존하고 안전 중단한다. R-023의 provider 검증 범위만 다루며 원격 bootstrap과 공통 지침 주입은 기존 담당 계획에 남긴다.

## Acceptance Criteria

- 지원되는 Claude 환경의 Planner·Worker·Reviewer·유지보수 역할에서 신규·재개·기존 session-unavailable fallback이 같은 역할별 정책을 적용한다. 쓰기 역할의 허용 파일 쓰기는 성공하고 source checkout·다른 worktree·shared Git metadata 쓰기는 거부되며 Reviewer의 선택 workspace 쓰기도 거부된다.
- 실행별 `--settings`의 파일 권한·sandbox 정책을 조합해 파일 도구와 명령 도구에 동일한 경계를 적용한다. `Edit(경로)` 규칙은 Edit·Write·NotebookEdit에 적용하고 Bash·하위 프로세스 쓰기는 sandbox로 제한한다. source checkout·다른 worktree·`.git` pointer file·resolved shared Git·worktree Git 경로를 양쪽 도구 경계에서 보호한다. 보호 경로 검증은 사용자 파일·index·Git 상태를 변경하지 않는다.
- `--add-dir`·`sandbox.filesystem.allowWrite` 등 추가 허용 설정만으로 허용 목록 밖 쓰기가 모두 차단됐다고 판단하지 않는다. 기본·상속 설정 source, 우선순위, 배열 병합, 파일 권한에서 파생된 허용, linked worktree의 기본 Git 허용과 임시 경로를 확인하며 역할별 유효 경계를 넓히는 충돌·무시를 거부한다.
- 지원 모델·계정·CLI의 자동 승인 검토가 유지되고 쓰기 경계 확대·sandbox 밖 재실행은 거부된다. 설정 문자열이나 프롬프트만으로 효력을 인정하거나 acceptEdits·일괄 명령 승인·자동 검토 비활성화로 대체하지 않는다.
- 확인·거부 근거는 CLI·환경·모델 지원·역할·canonical workspace·repository·유효 설정·session·호출 identity에 결합된다. 재개, CLI·설정 변경 및 중단 후 호출은 필요한 근거를 다시 확인한다. R-023·R-055에 따라 격리 증거 없는 과거 실행도 기본 checkout의 파일·index·HEAD·로컬 base ref를 scope 비교에서 제외하며, 실제 범위 밖 쓰기·선택 자원 위반·보안 설정 변조와 독립 차단은 보존한다. 과거 snapshot·격리 성공을 합성하지 않는다.
- Claude CLI·유료 계정이 없는 개발 환경에서는 공식 웹 문서의 기능·버전·설정 병합·역할별 경계와 모의 호출로 지원 구현을 검증할 수 있다. CLI 설치·계정 확보·유료 호출을 개발 시작이나 지원 구현 완료 조건으로 추가하지 않는다. 문서 검토·설정 문자열·mock 성공과 실제 정책 적용 결과를 구분해 미수행 범위를 명시한다.
- 실제 선택된 Claude 호출에서 native Windows, 미지원 모델·계정·CLI, 정책 충돌·무시, 경계 초기화·증거 저장 실패 또는 요구 정책의 적용을 확인할 수 없는 경우 해당 호출을 진단·중단하고 상태·작업물·session·과거 거부·retry 이력을 보존한다. 실행 중 managed 정책 갱신으로 충돌·제한 적용 실패가 확인된 경우에도 해당 호출을 중단하고 변경 전후 정책과 근거를 보존한다. 개발 환경의 CLI·계정 부재나 Claude 호출 실패로 독립 Codex 작업을 막거나 선택 provider를 임의 전환하지 않는다. 더 넓은 권한의 재실행이나 일괄 차단 해제를 하지 않으며 기록에 비밀값과 source 로컬 파일 내용을 포함하지 않는다.
- 기술 조사 미완료를 지원 구현 전체의 사용자 판단 대기로 확대하지 않는다. 기존 기능으로 충족할 수 없는 요구가 구체적으로 확인돼 실제 요구사항 변경이 필요한 경우에만, 해당 요구·조사한 기능과 CLI 버전·근거·남은 제약·필요한 변경을 제시해 사용자 판단을 요청한다. 조직 관리 환경으로의 지원 범위 축소나 외부 격리 도입은 아직 선택된 방향이 아니다.
- Codex의 R-050 직접 exec·내장 도구 정책 확인, 기존 session·사용량·timeout·중단 처리, 역할별 파일 검사와 Worker 이후 Reviewer 흐름이 유지된다. Reviewer의 읽기 전용 조사 권한과 프롬프트의 테스트·verify 재실행 금지는 별개로 유지한다.

## Implementation

- docs/ARCHITECTURE.md의 `R-023: workspace 쓰기 제한과 자동 승인 검토 (구현 예정)` 설계를 적용한다. 기존 provider adapter의 Claude permission·sandbox 설정, isolation evidence와 invocation 저장 경계를 재사용하며 필요 부분만 provider 책임으로 분리한다. Codex 전용 launch 값을 Claude 검증 성공으로 대용하거나 범용 격리 framework를 추가하지 않는다.
- 공식 웹 문서에서 조사한 기능·적용 버전·지원 조건과 남은 제약을 기록하고, 실행별 `--settings`·파일 권한·sandbox 정책을 기존 adapter의 역할별 호출에 연결한다. 파일 도구 경로 규칙은 `Edit(경로)`를 사용하며 경로 기준·절대경로 표기·canonical 경로를 확인한다. Bash·하위 프로세스에는 sandbox를 적용한다. `--add-dir`·`permissions.additionalDirectories`·`allowWrite`는 추가 허용으로 해석하고 기본 허용·명시적 거부·설정 source·배열 병합·임시 경로를 함께 검토한다. 빈 배열이나 실행별 설정이 상속된 허용을 모두 제거한다고 가정하지 않는다.
- 정책 작성, 호출별 적용 확인, 증거 영속화와 호출 직전 재검증을 기존 adapter에 연결한다. 요청 설정과 병합 후 유효 정책·적용 source, 공식 조사·모의 테스트·실제 호출 관측을 구분해 저장한다. 이용 가능한 환경의 실제 호출 검증은 추가 근거로 기록한다. 단순 설정 문자열이나 mock 성공만으로 실제 정책 적용 성공을 기록하지 않으며, 실제 호출의 정책 확인 실패는 원인별로 보존한다.
- 실행 중 managed 정책 갱신의 우선순위·적용 시점·파일 권한·sandbox 재적용과 제한 유지 가능성을 기능·CLI 버전별로 추가 조사한다. 시작 시 정책만으로 실행 중 경계가 고정됐다고 가정하지 않는다. 조사한 조건을 호출별 지원·거부 판정과 정책 충돌·제한 적용 실패의 중단·근거 보존 경로로 구현한다. native 경로 미확정은 이 조사·구현 항목에 남기며, 불충족 근거 없이 조직 관리 환경 한정과 외부 격리 중 선택을 반복 요청하지 않는다. 실제 요구사항 변경 요청은 Acceptance Criteria의 구체적 제시 조건을 따른다.
- 공유 Git 및 source 보호와 소유 임시 경로 판정에는 기존 canonical worktree·repository 검증을 사용한다. 신규 호출의 provider 쓰기 제한과 과거 실행의 scope 비교 경계를 구분하고, legacy 기본 checkout 비교 제외를 격리 증거의 존재에 결합하지 않는다. 별도 유료 금지 쓰기 probe를 시작 조건으로 추가하거나 보호 파일을 변경하지 않는다. 새로운 외부 격리 infrastructure, OS 전환, 별도 승인 서비스나 루트 항목은 도입하지 않는다.
- session 재개·fallback의 기존 recovery context와 결과·사용량·중단 계약을 유지한다. docs/AGENTS.common.md·해당 역할의 docs/AGENTS.*.md·AGENTS.md·docs/PLANS.md의 문서·역할 경계를 따르며 정책 확인 실패를 문서 교정이나 무조건 새 session 시작으로 우회하지 않는다.

공식 조사 자료는 다음을 사용한다. 아래 기능 설명은 정책 조합의 검토 근거이며 실제 CLI 실행 검증 완료를 뜻하지 않는다.

| 자료 | 조사·대조할 항목 |
|---|---|
| [CLI 옵션](https://code.claude.com/docs/en/cli-reference) | 실행별 `--settings`, 추가 디렉터리 허용과 설정 source 선택의 의미·지원 버전 |
| [파일 권한 규칙](https://code.claude.com/docs/en/permissions#read-and-edit) | `Edit(경로)`의 Edit·Write·NotebookEdit 적용, 경로 해석과 Bash 하위 프로세스까지 제한하지 못하는 범위 |
| [Sandbox 설정](https://code.claude.com/docs/en/sandboxing#configure-sandboxing) | Bash·하위 프로세스 제한, 추가 `allowWrite`와 `denyWrite`, 기본 임시 경로·linked worktree shared Git 허용 |
| [설정 우선순위](https://code.claude.com/docs/en/settings#settings-precedence) | managed와 실행별 설정의 우선순위·예외, 상속 설정과 배열 병합 |
| [서버 관리 설정·실행 중 갱신](https://code.claude.com/docs/en/server-managed-settings) | 시작·실행 중 갱신·캐시와 적용 시점, 갱신 뒤 제한 유지·충돌·실패의 확인 및 중단 가능성 |

## Validation

- Worker는 tests/test_agent_write_isolation.py·tests/test_provider_runtime.py·tests/test_native_platform_agents.py의 관련 fixture를 재사용해 수정 경계의 최소 unit case만 작성·실행한다. 모든 역할의 신규·재개·fallback, `Edit` 규칙의 Edit·Write·NotebookEdit 적용, Bash·script·하위 프로세스 경계, source·다른 worktree·resolved shared Git deny, Reviewer 읽기 전용, 자동 검토 유지와 확대 거부를 확인한다. 기본·상속 추가 허용과 배열 병합·빈 배열·경로 해석·임시 경로, 정책 충돌·무시, CLI/모델/계정 미지원, native Windows 및 증거 저장·중단·재확인 실패를 확인한다. 모델·provider subprocess는 test double로 대체한다.
- managed 정책의 초기 적용과 실행 중 변경을 모의해 유효 정책 변경·충돌·제한 적용 실패를 호출별 판정·중단·변경 전후 근거 저장에 연결한다. 이전 호출의 성공 근거나 설정 문자열·mock 성공을 새 호출의 실제 적용 성공으로 저장하지 않는지 확인한다. 이 테스트는 실제 CLI의 managed 갱신 시 제한 유지 성공을 증명하지 않는다. 조사 미완료와 실제 호출 거부를 구분하고, 모의 실패를 근거로 지원 구현 전체를 새 사용자 정책 판단 대기로 확대하지 않는다.
- Manager 전체 verify는 Claude의 확인된 정책 적용과 거부가 managed invocation·scope 저장·session 복구로 연결되는 회귀 및 Codex 직접 exec, process lock, remote-only 작업 입력, scope 안전 차단·후보 Review를 유지한다. 자동 테스트는 실제 Agent를 실행하지 않는다. 공식 정책·병합 후 유효 설정·모의 도구 경계를 함께 확인하고 test double의 인자 확인만으로 실제 호출 성공을 기록하지 않는다. 실제 호출 검증 미수행을 명시하되 별도 유료 계정·실호출을 완료 조건으로 추가하지 않는다.
- 관련 scope fixture로 증거 없는 legacy 기본 checkout 변경의 비교 제외와 실제 선택 자원·범위 밖 쓰기·보안 설정 위반의 보존을 함께 확인한다. 개발 환경의 Claude CLI·계정 부재와 선택된 Claude 호출 오류가 독립 Codex 작업을 차단하지 않는지 확인한다.

## Progress

- [x] 2026-10-05 planning 기준 32e496c에서 prepare_isolation의 non-Codex 일괄 거부와 run_claude의 관리 호출 경계를 대조했다. 기존 테스트는 Linux의 모든 역할·신규·재개에서 Claude 거부만 확인하며 supported 호출의 실제 경계 확인은 없다. 비종료 G-147은 공통 지침 전달·버전 보존으로 이 범위를 포함하지 않는다. 기존 파일 최대 ID는 G-190이다. 구현·테스트 실행은 하지 않았다.
- [ ] 공식 기능·버전별 실행별 설정과 파일 권한·sandbox 조합, 상속·배열 병합·임시 경로의 유효 경계 조사 및 지원·거부 조건 구현
- [ ] 실행 중 managed 정책 갱신에도 제한을 유지할 native 경로 추가 조사 및 호출별 충돌·제한 적용 실패의 중단·근거 저장 검증
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

실제 CLI 정책 조합은 아직 실행 검증하지 않았다. managed 정책 갱신을 포함한 제한 유지 가능성도 추가 조사 대상이다. 이용 가능한 환경의 실호출 검증은 추가 근거이며 CLI 설치·계정 확보·유료 호출을 위 구현 완료 조건으로 추가하지 않는다. 이번 문서 보강은 구현 성공·실제 정책 적용 성공·기존 차단 해소의 근거가 아니다.

## Decision Log

- 지원 여부를 실제로 확인하지 않는 provider 단위 일괄 거부는 안전한 실패 동작이지만 R-023의 지원 환경 검증 완료 근거가 아니다. 동일한 보안 기준 안에서 확인 가능한 호출만 허용하므로 새 제품 목표나 권한 확대가 아니다.
- provider 효력·역할·session·설정 병합이 연결돼 complexity high, 잘못된 확인의 경계 밖 쓰기 영향으로 risk high다. 설정된 Claude 작업의 무인 실행을 복구하는 필수 작업으로 priority high다.
- 원격 실행 gate의 G-161과 공통 지침 주입의 G-147은 이 구현의 선행 조건이 아니다. 기존 성공 호출 계약을 유지해 독립적으로 검증·Merge하며, 전체 차단·재개 감사 G-189는 이 변경 뒤 수행한다.
- 2026-10-09 사용자 요청에 따른 최신 명세 정합화: R-023·R-055의 legacy 기본 checkout 제외를 반영해 과거 실행의 포괄적 엄격 판정 요구를 대체했다. Claude 지원 구현은 공식 정책·유효 설정·모의 호출로 검증하며 유료 실호출 시연을 시작·완료 조건으로 요구하지 않는다. 신규 호출의 쓰기 제한·자동 검토·실제 위반 보존과 선택 호출의 안전 중단은 유지한다. ID·기존 dependency·실행 이력을 보존하며 구현·검증 성공을 기록하지 않는다.
- 2026-10-11 사용자 요청에 따른 문서 보강: 기존 내장 기능 우선·공식 웹 문서와 모의 테스트 개발은 확정된 방향이다. managed 갱신 시 native 제한 유지 경로의 조사 미완료만으로 조직 관리 환경 한정이나 외부 격리가 필요하다고 단정하지 않는다. 실제 선택 호출의 정책 충돌·제한 적용 실패는 안전 중단하고 근거를 보존하며, 기존 기능으로 충족할 수 없는 요구와 조사한 기능·버전·제약·필요 변경을 구체적으로 제시할 때만 요구사항 변경 판단을 요청한다. 두 대안은 미선택이며 기존 ID·dependency·실행 이력과 남은 구현·검증 항목을 유지한다.
