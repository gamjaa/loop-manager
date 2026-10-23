---
id: G-191
title: Claude 내장 쓰기 경계와 자동 승인 검토 검증
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

Claude의 관리 호출을 provider 이름만으로 일괄 거부하는 현재 경계를, 기존 내장 기능과 유효 정책에 따라 역할별 쓰기 제한·자동 승인 검토를 확인하는 경계로 완성한다. 최신 R-023에 따라 공식 웹 문서에 근거한 일반 호출 구성과 모의 테스트로 구현·완료를 판단한다. 실행 중 managed 정책 변경의 감시·적용 전 차단·지속 제한 보장과 실환경 점검은 FUTURE_IDEAS의 후속 확인이며 현재 구현·검증·사용자 판단의 선행 조건이 아니다. 이를 위한 별도 hook·polling·외부 격리나 조직 관리 환경 도입을 요구하지 않는다. 실제 선택 호출의 미지원·정책 충돌·제한 적용 실패는 근거를 보존하고 안전 중단한다. R-023의 provider 검증 범위만 다루며 원격 bootstrap과 공통 지침 주입은 기존 담당 계획에 남긴다.

## Acceptance Criteria

- 지원되는 Claude 환경의 Planner·Worker·Reviewer·유지보수 역할에서 신규·재개·기존 session-unavailable fallback이 같은 역할별 정책을 적용한다. 쓰기 역할의 허용 파일 쓰기는 성공하고 source checkout·다른 worktree·shared Git metadata 쓰기는 거부되며 Reviewer의 선택 workspace 쓰기도 거부된다.
- 실행별 `--settings`의 파일 권한·sandbox 정책을 조합해 파일 도구와 명령 도구에 동일한 경계를 적용한다. `Edit(경로)` 규칙은 Edit·Write·NotebookEdit에 적용하고 Bash·하위 프로세스 쓰기는 sandbox로 제한한다. source checkout·다른 worktree·`.git` pointer file·resolved shared Git·worktree Git 경로를 양쪽 도구 경계에서 보호한다. 보호 경로 검증은 사용자 파일·index·Git 상태를 변경하지 않는다.
- `--add-dir`·`sandbox.filesystem.allowWrite` 등 추가 허용 설정만으로 허용 목록 밖 쓰기가 모두 차단됐다고 판단하지 않는다. 기본·상속 설정 source, 우선순위, 배열 병합, 파일 권한에서 파생된 허용, linked worktree의 기본 Git 허용과 임시 경로를 확인하며 역할별 유효 경계를 넓히는 충돌·무시를 거부한다.
- 지원 모델·계정·CLI의 자동 승인 검토가 유지되고 쓰기 경계 확대·sandbox 밖 재실행은 거부된다. 설정 문자열이나 프롬프트만으로 효력을 인정하거나 acceptEdits·일괄 명령 승인·자동 검토 비활성화로 대체하지 않는다.
- 확인·거부 근거는 CLI·환경·모델 지원·역할·canonical workspace·repository·유효 설정·session·호출 identity에 결합된다. 재개, CLI·설정 변경 및 중단 후 호출은 필요한 근거를 다시 확인한다. 최신 R-023·R-055에 따라 Git 상태의 전후 차이를 Agent 권한 위반으로 판정하는 검사는 신규·legacy·재개·fallback에서 제거한다. Manager의 작업 대상·원격·후보 검증에 필요한 현재 조건과 실제 관측한 provider 권한 오류·보안 설정 오류 및 독립 차단은 보존한다. 과거 snapshot·격리 성공을 합성하지 않는다.
- Claude CLI·유료 계정이 없는 개발 환경에서는 공식 웹 문서의 기능·버전·설정 병합·역할별 경계와 모의 호출로 지원 구현을 검증할 수 있다. CLI 설치·계정 확보·유료 호출을 개발 시작이나 지원 구현 완료 조건으로 추가하지 않는다. 문서 검토·설정 문자열·mock 성공과 실제 정책 적용 결과를 구분해 미수행 범위를 명시한다.
- 실제 선택된 Claude 호출에서 native Windows, 미지원 모델·계정·CLI, 정책 충돌·무시, 경계 초기화·증거 저장 실패 또는 요구 정책의 적용을 확인할 수 없는 경우 해당 호출을 진단·중단하고 상태·작업물·session·과거 거부·retry 이력을 보존한다. 실행 중 실제 권한·인증·도구 오류가 관측되면 해당 호출을 중단하고 구체적 오류와 기존 근거를 보존한다. 별도의 managed 변경 감시·적용 전 차단이나 변경 전후 정책 수집 구현은 요구하지 않는다. 개발 환경의 CLI·계정 부재나 Claude 호출 실패로 독립 Codex 작업을 막거나 선택 provider를 임의 전환하지 않는다. 더 넓은 권한의 재실행이나 일괄 차단 해제를 하지 않으며 기록에 비밀값과 source 로컬 파일 내용을 포함하지 않는다.
- 기술 조사 미완료와 범위 밖 managed 변경 대응을 지원 구현 전체의 사용자 판단 대기로 확대하지 않는다. 기존 기능으로 충족할 수 없는 요구가 구체적으로 확인돼 실제 요구사항 변경이 필요한 경우에만, 해당 요구·조사한 기능과 CLI 버전·근거·남은 제약·필요한 변경을 제시해 사용자 판단을 요청한다. 조직 관리 환경으로의 지원 범위 축소나 외부 격리 도입은 아직 선택된 방향이 아니다.
- Codex의 R-050 직접 exec·내장 도구 정책 확인, 기존 session·사용량·timeout·중단 처리, 역할별 파일 검사와 Worker 이후 Reviewer 흐름이 유지된다. Reviewer의 읽기 전용 조사 권한과 프롬프트의 테스트·verify 재실행 금지는 별개로 유지한다.

## Implementation

- docs/ARCHITECTURE.md의 `R-023: workspace 쓰기 제한과 자동 승인 검토 (구현 예정)` 설계를 적용한다. 기존 provider adapter의 Claude permission·sandbox 설정, isolation evidence와 invocation 저장 경계를 재사용하며 필요 부분만 provider 책임으로 분리한다. Codex 전용 launch 값을 Claude 검증 성공으로 대용하거나 범용 격리 framework를 추가하지 않는다.
- 공식 웹 문서에서 조사한 기능·적용 버전·지원 조건과 남은 제약을 기록하고, 실행별 `--settings`·파일 권한·sandbox 정책을 기존 adapter의 역할별 호출에 연결한다. 파일 도구 경로 규칙은 `Edit(경로)`를 사용하며 경로 기준·절대경로 표기·canonical 경로를 확인한다. Bash·하위 프로세스에는 sandbox를 적용한다. `--add-dir`·`permissions.additionalDirectories`·`allowWrite`는 추가 허용으로 해석하고 기본 허용·명시적 거부·설정 source·배열 병합·임시 경로를 함께 검토한다. 빈 배열이나 실행별 설정이 상속된 허용을 모두 제거한다고 가정하지 않는다.
- 정책 작성, 호출별 적용 확인, 증거 영속화와 호출 직전 재검증을 기존 adapter에 연결한다. 요청 설정과 병합 후 유효 정책·적용 source, 공식 조사·모의 테스트·실제 호출 관측을 구분해 저장한다. 이용 가능한 환경의 실제 호출 검증은 추가 근거로 기록한다. 단순 설정 문자열이나 mock 성공만으로 실제 정책 적용 성공을 기록하지 않으며, 실제 호출의 정책 확인 실패는 원인별로 보존한다.
- 신규·재개·fallback의 시작 설정·권한·지원 조건을 확인하고, 시작 시 정책 충돌·초기화 실패와 실행 중 실제 권한·인증·도구 오류를 기존 호출별 중단·근거 보존 경로에 연결한다. 실행 중 managed 변경의 감시·적용 전 차단·지속 제한 보장은 현재 구현·검증에서 제외하며 docs/FUTURE_IDEAS.md의 Claude 실환경 후속 점검으로 남긴다. 후속 점검의 미완료만으로 기존 질문이나 추가 infrastructure 선택을 다시 요구하지 않는다.
- 공유 Git 및 source 보호와 소유 임시 경로 판정에는 기존 canonical worktree·repository 검증과 provider 쓰기 제한을 사용한다. 과거 Git snapshot 비교를 접근 권한 증명이나 차단 해제 gate로 재도입하지 않는다. 정상 fetch·다른 작업의 ref/worktree 변화는 Agent 위반으로 귀속하지 않고 Manager의 현재 대상·후보 검증을 유지한다. 별도 유료 금지 쓰기 probe를 시작 조건으로 추가하거나 보호 파일을 변경하지 않는다. 새로운 외부 격리 infrastructure, OS 전환, 별도 승인 서비스나 루트 항목은 도입하지 않는다.
- session 재개·fallback의 기존 recovery context와 결과·사용량·중단 계약을 유지한다. docs/AGENTS.common.md·해당 역할의 docs/AGENTS.*.md·AGENTS.md·docs/PLANS.md의 문서·역할 경계를 따르며 정책 확인 실패를 문서 교정이나 무조건 새 session 시작으로 우회하지 않는다.

공식 조사 자료는 다음을 사용한다. 아래 기능 설명은 정책 조합의 검토 근거이며 실제 CLI 실행 검증 완료를 뜻하지 않는다.

| 자료 | 조사·대조할 항목 |
|---|---|
| [CLI 옵션](https://code.claude.com/docs/en/cli-reference) | 실행별 `--settings`, 추가 디렉터리 허용과 설정 source 선택의 의미·지원 버전 |
| [파일 권한 규칙](https://code.claude.com/docs/en/permissions#read-and-edit) | `Edit(경로)`의 Edit·Write·NotebookEdit 적용, 경로 해석과 Bash 하위 프로세스까지 제한하지 못하는 범위 |
| [Sandbox 설정](https://code.claude.com/docs/en/sandboxing#configure-sandboxing) | Bash·하위 프로세스 제한, 추가 `allowWrite`와 `denyWrite`, 기본 임시 경로·linked worktree shared Git 허용 |
| [설정 우선순위](https://code.claude.com/docs/en/settings#settings-precedence) | managed와 실행별 설정의 우선순위·예외, 상속 설정과 배열 병합 |
| [서버 관리 설정·실행 중 갱신](https://code.claude.com/docs/en/server-managed-settings) | 시작 설정의 우선순위·캐시 확인; 실행 중 변경 대응은 FUTURE_IDEAS의 후속 확인 |

## Validation

- Worker는 tests/test_agent_write_isolation.py·tests/test_provider_runtime.py·tests/test_native_platform_agents.py의 관련 fixture를 재사용해 수정 경계의 최소 unit case만 작성·실행한다. 모든 역할의 신규·재개·fallback, `Edit` 규칙의 Edit·Write·NotebookEdit 적용, Bash·script·하위 프로세스 경계, source·다른 worktree·resolved shared Git deny, Reviewer 읽기 전용, 자동 검토 유지와 확대 거부를 확인한다. 기본·상속 추가 허용과 배열 병합·빈 배열·경로 해석·임시 경로, 정책 충돌·무시, CLI/모델/계정 미지원, native Windows 및 증거 저장·중단·재확인 실패를 확인한다. 모델·provider subprocess는 test double로 대체한다.
- 시작 설정의 초기 적용·충돌·초기화 실패와 실제 권한·인증·도구 오류의 관측을 모의해 호출별 중단·상태 보존을 검증한다. 이전 호출의 성공 근거나 설정 문자열·mock 성공을 새 호출의 실제 적용 성공으로 저장하지 않는지 확인한다. 실시간 managed 변경 감시·적용 전 차단·지속 제한 보장의 테스트나 실계정 호출은 완료 조건에 추가하지 않는다. 실환경 미검증과 범위 밖 후속 점검 때문에 지원 구현 전체를 사용자 판단 대기로 바꾸지 않는다.
- Manager 전체 verify는 Claude의 확인된 정책 적용과 거부가 managed invocation·scope 저장·session 복구로 연결되는 회귀 및 Codex 직접 exec, process lock, remote-only 작업 입력과 현재 후보 검증·Review를 유지한다. 자동 테스트는 실제 Agent를 실행하지 않는다. 공식 정책·병합 후 유효 설정·모의 도구 경계를 함께 확인하고 test double의 인자 확인만으로 실제 호출 성공을 기록하지 않는다. 실제 호출 검증 미수행을 명시하되 별도 유료 계정·실호출을 완료 조건으로 추가하지 않는다.
- 관련 fixture로 기본 checkout·다른 작업 ref/worktree의 정상 변화가 Agent 위반으로 판정되지 않는지와 실제 선택 자원·provider 권한·보안 설정 오류의 보존을 함께 확인한다. 개발 환경의 Claude CLI·계정 부재와 선택된 Claude 호출 오류가 독립 Codex 작업을 차단하지 않는지 확인한다.

## Progress

- [x] 2026-10-05 planning 기준 32e496c에서 prepare_isolation의 non-Codex 일괄 거부와 run_claude의 관리 호출 경계를 대조했다. 기존 테스트는 Linux의 모든 역할·신규·재개에서 Claude 거부만 확인하며 supported 호출의 실제 경계 확인은 없다. 비종료 G-147은 공통 지침 전달·버전 보존으로 이 범위를 포함하지 않는다. 기존 파일 최대 ID는 G-190이다. 구현·테스트 실행은 하지 않았다.
- [ ] 공식 기능·버전별 실행별 설정과 파일 권한·sandbox 조합, 상속·배열 병합·임시 경로의 유효 경계 조사 및 지원·거부 조건 구현
- [ ] 시작 설정의 충돌·초기화 실패와 실제 관측한 호출 오류의 중단·근거 보존 검증
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

실제 CLI 정책 조합은 아직 실행 검증하지 않았다. 실환경 점검과 실행 중 managed 정책 변경 대응은 FUTURE_IDEAS의 후속 확인으로 보존하며 현재 구현 완료 조건에 추가하지 않는다. 이용 가능한 환경의 실호출 검증은 추가 근거이며 CLI 설치·계정 확보·유료 호출도 완료 조건이 아니다. 이 문서 정합화는 최신 R-023의 범위 결정을 반영하며 구현·테스트·실제 정책 적용 성공을 뜻하지 않는다.

## Native 정책 갱신 조사와 남은 판단

2026-10-13 Worker는 공식 문서와 [공식 changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md)의 최신 표기 v2.1.296을 대조했다. 아래는 문서 조사 결과이며 설치 CLI에서 관측한 결과가 아니다. 개발 환경은 Linux/Python 3.12.3이고 Claude executable이 없다. CLI 부재는 아래 제약의 근거나 사용자 판단 요청의 이유가 아니다. 선택 adapter는 `scripts/verify.json` version 1의 unittest이고 `docs/VERIFY.md`의 Python 3.11–3.14 계약을 확인했다.

| 조사한 native 경로 | 확인된 기능·버전 및 제약 |
|---|---|
| 실행별 파일·명령 정책 | [파일 권한](https://code.claude.com/docs/en/permissions#read-and-edit)은 `Edit(path)`로 편집 도구를 제한하지만 임의 subprocess에는 적용되지 않는다. `--disallowedTools`는 managed 정책에 추가 제한을 줄 수 있다. [Sandbox](https://code.claude.com/docs/en/sandboxing#configure-sandboxing)의 filesystem 배열은 병합되며, 제외한 설정 source를 sandbox 구성에서도 제외하는 동작은 v2.1.246부터다. 추가 허용이나 빈 배열로 상속 허용을 지웠다고 판단할 수 없다. |
| managed보다 강한 실행별 제한 | [설정 우선순위 예외](https://code.claude.com/docs/en/settings#exceptions-to-managed-settings-precedence)에는 `autoMode.classifyAllShell=true`가 있으나 sandbox `enabled`, `failIfUnavailable`, `allowUnsandboxedCommands`, `autoAllowBashIfSandboxed`는 없다. [Sandbox의 managed 우선순위](https://code.claude.com/docs/en/sandboxing#keep-developers-from-widening-the-policy)는 managed Boolean 값이 로컬 값을 대체함을 명시한다. 명령 classifier의 검토를 OS 쓰기 제한으로 대신할 수 없다. |
| 정책 변경 hook | [ConfigChange 계약](https://code.claude.com/docs/en/hooks#configchange)은 로컬 managed 파일 변경을 통지하지만 해당 변경의 blocking 결정을 무시한다. 서버 managed 갱신에는 hook 자체가 실행되지 않는다. 변경을 막는 hook이나 변경 통지 후 종료만으로 모든 managed 갱신에 대한 사전 중단을 보장할 수 없다. |
| 시작·실행 중 관측 | [서버 managed 설정](https://code.claude.com/docs/en/server-managed-settings#fetch-and-caching-behavior)은 시작 시 fetch와 실행 중 매시간 갱신을 수행한다. `claude doctor`의 remote delivery 진단은 v2.1.248부터이며 현재 fetch 결과이지 다음 갱신을 고정하는 계약이 아니다. `-p`에서 승인 dialog가 필요한 정책도 해당 실행에 적용된다. 시작 검사나 외부 polling 사이에 다음 명령이 실행될 수 있다. |
| 설정 source 제외·bare mode | [CLI](https://code.claude.com/docs/en/cli-reference)는 `--setting-sources`에 user/project/local만 제공한다. [Bare mode](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)는 subscription OAuth/keychain을 읽지 않고 API 인증을 요구한다. 이를 기존 계정·지침 계약을 보존하면서 managed 갱신만 고정하는 기능으로 채택할 근거가 없다. `--safe-mode`도 managed 정책을 유지한다. |

구체적 반례는 시작 시 안전한 유효 정책을 확인한 `--print --permission-mode auto` 호출에 서버가 `sandbox.enabled=false` 또는 `sandbox.allowUnsandboxedCommands=true`를 갱신하는 경우다. 전자는 명령 sandbox를 해제하고 후자는 실행별 sandbox 밖 retry 금지를 대체한다. managed 우선순위와 ConfigChange 미통지 때문에 현재 직접 CLI 경로의 시작 검사·설정 지문·hook으로는 갱신 적용과 다음 도구 실행 사이의 중단을 보장할 수 없다. 별도 polling이나 종료 후 diff 검사는 이미 발생한 쓰기를 예방하지 못한다. native Windows 미지원, 상속 충돌의 시작 전 거부와 달리 실행 중 정책을 원자적으로 확인·거부하는 계약이 필요한 반례다.

이는 조사한 직접 CLI 기능에서 R-023과 이 작업의 실행 중 제한 유지·충돌 중단 Acceptance Criteria를 함께 충족할 수 없는 조건이다. 모든 Claude 환경의 기능 부재나 실제 범위 위반 발생을 선언하지 않는다. 해당 조건을 지원으로 표시하거나 mock으로 실제 정책 적용 성공을 합성하지 않으며, 현재 provider 단위 안전 거부는 변경하지 않았다. 독립 Codex 호출·기존 session·snapshot·과거 거부 기록도 변경하지 않았다.

필요한 사용자 판단: 이 조건까지 지원하기 위해 현재 Implementation에서 제외한 별도 OS 쓰기 경계를 허용할지, 또는 native CLI가 갱신 전 확인·중단 계약을 제공할 때까지 해당 조건의 지원 구현을 보류할지 결정이 필요하다. 전자는 Claude 직접 호출의 실행 경계와 설계 변경을 요구하며 자동 승인 검토는 그대로 유지해야 한다. 조직 관리 환경만으로 지원 범위를 축소하거나 유료 호출·새 계정을 요구하는 제안은 아니다. 사용자 결정은 아직 없으며 Requirement·PRODUCT·ARCHITECTURE의 보장과 이 작업의 필수 검증은 수정하지 않았다.

이번 diff는 조사·제약·필요 판단 기록만 포함한다. 실행 가능한 코드·설정·test 변경이 없어 targeted unit test를 실행하지 않았다. 전체 verify·실제 Claude 호출·managed 갱신의 실제 실행 검증도 수행하지 않았다. 구현·검증 완료와 실행 상태 판정은 기록하지 않는다.

## Decision Log

- 지원 여부를 실제로 확인하지 않는 provider 단위 일괄 거부는 안전한 실패 동작이지만 R-023의 지원 환경 검증 완료 근거가 아니다. 동일한 보안 기준 안에서 확인 가능한 호출만 허용하므로 새 제품 목표나 권한 확대가 아니다.
- provider 효력·역할·session·설정 병합이 연결돼 complexity high, 잘못된 확인의 경계 밖 쓰기 영향으로 risk high다. 설정된 Claude 작업의 무인 실행을 복구하는 필수 작업으로 priority high다.
- 원격 실행 gate의 G-161과 공통 지침 주입의 G-147은 이 구현의 선행 조건이 아니다. 기존 성공 호출 계약을 유지해 독립적으로 검증·Merge하며, 전체 차단·재개 감사 G-189는 이 변경 뒤 수행한다.
- 2026-10-09 사용자 요청에 따른 최신 명세 정합화: R-023·R-055의 legacy 기본 checkout 제외를 반영해 과거 실행의 포괄적 엄격 판정 요구를 대체했다. Claude 지원 구현은 공식 정책·유효 설정·모의 호출로 검증하며 유료 실호출 시연을 시작·완료 조건으로 요구하지 않는다. 신규 호출의 쓰기 제한·자동 검토·실제 위반 보존과 선택 호출의 안전 중단은 유지한다. ID·기존 dependency·실행 이력을 보존하며 구현·검증 성공을 기록하지 않는다.
- 2026-10-11 사용자 요청에 따른 문서 보강: 기존 내장 기능 우선·공식 웹 문서와 모의 테스트 개발은 확정된 방향이다. managed 갱신 시 native 제한 유지 경로의 조사 미완료만으로 조직 관리 환경 한정이나 외부 격리가 필요하다고 단정하지 않는다. 실제 선택 호출의 정책 충돌·제한 적용 실패는 안전 중단하고 근거를 보존하며, 기존 기능으로 충족할 수 없는 요구와 조사한 기능·버전·제약·필요 변경을 구체적으로 제시할 때만 요구사항 변경 판단을 요청한다. 두 대안은 미선택이며 기존 ID·dependency·실행 이력과 남은 구현·검증 항목을 유지한다.

- 2026-10-16 수동 복구 정합화: 최신 R-023의 명시적 범위 결정에 따라 실행 중 managed 변경의 감시·적용 전 차단·지속 제한 보장과 실환경 완전성 점검을 현재 구현·검증에서 제외했다. 2026-10-11 기록은 당시 이력으로 보존하며 해당 조사 요구를 현재 완료 조건으로 적용하지 않는다. 기존 질문의 해당 보장을 새로 완화하거나 관측 전 쓰기를 승인하는 변경이 아니다. 시작 설정의 역할별 쓰기 제한·자동 승인 검토와 실제 관측 오류의 안전 중단은 유지한다. Git 상태 차이를 Agent 위반으로 판정하는 옛 요구도 최신 R-023·R-055로 대체하며, 기존 작업물·실패·검증 이력과 ID·메타데이터를 보존한다.
