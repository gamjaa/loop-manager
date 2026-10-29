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

Claude의 관리 호출을 provider 이름만으로 일괄 거부하던 경계를, 기존 내장 기능과 유효 정책에 따라 역할별 쓰기 제한·자동 승인 검토를 확인하는 경계로 완성한다. 최신 R-023에 따라 공식 웹 문서에 근거한 일반 호출 구성과 모의 테스트로 구현·완료를 판단한다. 실행 중 managed 정책 변경의 감시·적용 전 차단·지속 제한 보장과 실환경 점검은 FUTURE_IDEAS의 후속 확인이며 현재 구현·검증·사용자 판단의 선행 조건이 아니다. 이를 위한 별도 hook·polling·외부 격리나 조직 관리 환경 도입을 요구하지 않는다. 실제 선택 호출의 미지원·정책 충돌·제한 적용 실패는 근거를 보존하고 안전 중단한다. R-023의 provider 검증 범위만 다루며 원격 bootstrap과 공통 지침 주입은 기존 담당 계획에 남긴다.

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

- docs/ARCHITECTURE.md의 `R-023: workspace 쓰기 제한과 자동 승인 검토` 설계를 적용한다. 기존 provider adapter의 Claude permission·sandbox 설정, isolation evidence와 invocation 저장 경계를 재사용하며 필요 부분만 provider 책임으로 분리한다. Codex 전용 launch 값을 Claude 검증 성공으로 대용하거나 범용 격리 framework를 추가하지 않는다.
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
- [x] 공식 기능·버전별 실행별 설정과 파일 권한·sandbox 조합, 상속·배열 병합·임시 경로의 유효 경계 조사 및 지원·거부 조건 구현
- [x] 시작 설정의 충돌·초기화 실패와 실제 관측한 호출 오류의 중단·근거 보존 검증
- [x] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

현재 후보는 구현·targeted unit 검증을 마쳤으며 Manager의 전체 verify·Review를 기다린다. 실제 Claude CLI·계정·OS 정책 적용은 실행 검증하지 않았다. 실환경 점검과 실행 중 managed 정책 변경 대응은 FUTURE_IDEAS의 후속 확인으로 보존하며 현재 완료 조건에 추가하지 않는다.

## 구현 결과와 검증 근거

관리 Claude 호출의 provider 단위 일괄 거부를 역할별 native 정책 준비·실행 확인으로 교체했다. `agent_claude_policy`는 restricted 파일 도구·Edit 거부·strict Bash sandbox·auto 검토를 구성하고, `agent_claude_preflight`는 시작 source·CLI·인증 홈·소유 임시 경로를 확인한다. `provider_adapters`는 신규·재개·session-unavailable fallback에 같은 경계를 연결한다. source·다른 worktree·shared/worktree Git과 `.git` pointer를 파일 도구와 sandbox 양쪽에서 보호하고 Reviewer·planner_check는 workspace 쓰기도 거부한다. 기존 Codex 직접 exec·native probe 경로는 유지한다.

| 공식 근거 | 적용한 계약과 제약 |
|---|---|
| [CLI 옵션](https://code.claude.com/docs/en/cli-reference)·[Sandbox 설정](https://code.claude.com/docs/en/sandboxing#configure-sandboxing) | v2.1.307 이상을 검사한다. restricted의 v2.1.248·excluded-source sandbox 병합의 v2.1.246 계약과 nonempty objects/refs 정리 보정을 포함한다. 빈 setting-sources로 개인·project·local 설정을 제외하고 native tool 목록과 MCP 제한을 명시한다. |
| [파일 권한](https://code.claude.com/docs/en/permissions#read-and-edit) | Edit·Write·NotebookEdit에 `Edit(//절대경로)`를 적용하고 Bash·하위 프로세스에는 sandbox `/절대경로` denyWrite를 적용한다. 추가 allowWrite만으로 완전한 allowlist를 증명하지 않는다. |
| [설정 우선순위](https://code.claude.com/docs/en/settings#settings-precedence)·[managed source](https://code.claude.com/docs/en/managed-settings) | remote cache·MDM·system JSON/drop-in의 first-wins/merge·배열 병합과 최고 source의 defaultMode를 해석한다. 하위 admin source의 env·filesystem 비활성화·binary override도 확인한다. 빈 배열로 상속 배열을 지우지 않는다. |
| [Apple Managed Preferences](https://www.apple.com/education/docs/Apple-ClientManagementWhitePaper.pdf) | macOS의 protected machine/user plist만 MDM source로 읽는다. 개인 defaults 조회로 대체하지 않으며 서로 다른 두 유효 정책은 모호한 우선순위로 거부한다. |
| [permission mode](https://code.claude.com/docs/en/permission-modes)·[시작 mode 관측](https://code.claude.com/docs/en/agent-sdk/permissions#auto-mode) | auto·classifyAllShell을 요청하고 실제 init의 mode·모델·CLI·cwd·도구·확장·재개 session을 확인한다. acceptEdits·일괄 명령 승인·sandbox 밖 재실행으로 대체하지 않는다. |

source는 제한된 일반 파일·JSON object/plist dictionary·유효 타입을 확인한다. 끊어진 링크·비일반 파일·읽기 실패·미지원 root key를 부재나 빈 정책으로 바꾸지 않는다. 권한을 넓히는 상속·hook/plugin/credential helper 충돌과 보호 경로 중첩·symlink alias·모호한 glob root를 거부한다. native Windows와 아직 host 정책 상속을 확인하지 못하는 WSL도 해당 호출만 진단·중단한다. source 경로·내용 지문과 비밀값 없는 환경 경로를 보존하며 원문·인증값은 저장하지 않는다.

준비 자료는 invocation·역할·workspace·repository·CLI·설정·session에 결합해 `prepared`로 저장한다. `basis=documented-native-configuration`, `runtime_enforcement=not_observed`를 명시하고 OS probe나 Codex verified 근거로 대용하지 않는다. 지침/profile 준비 뒤와 receipt 결합 후 spawn 직전에 설정·경로를 재확인하며 저장·재검사 실패는 프로세스를 시작하지 않는다. 실제 init 불일치·권한/인증/보안 오류를 관측하면 기존 process group 중단·오류/session 보존을 사용한다. 정상 unit 명령 실패는 교정할 수 있게 유지한다. 종료 확인 전 finished receipt를 만들지 않으며 Worker의 targeted runner·기록 경로를 같은 invocation에 결합한다. 실패·중단으로 근거가 invalidated돼도 원래 구성 사유는 보존한다. Git 상태의 전후 비교나 정상적인 다른 작업 ref/worktree 변화를 Agent 위반으로 판정하지 않는다.

### 실행 이력과 targeted unit 결과

2026-10-13 v2.1.296 공식 조사에서 기록한 실행 중 managed 갱신의 사전 차단 제약은 2026-10-16 최신 R-023에서 범위 밖 후속 확인으로 정리됐다. 이를 현재 판단 대기나 외부 격리 도입 조건으로 사용하지 않으며 기존 Decision Log를 보존한다. 2026-10-29 관리 rebase 뒤 a30388e1 기준 전체 diff와 현재 sources를 대조했다. Manager 복구점 2f794f630c61991144dbc06056bef0bbb6ec9237은 source 선택, c99603ba78143ec20fea677c1ebd684793590355는 native 정책 생성기, 1139c5b73de975cd60e62eb75c1b903ebe8eadbe는 source 수집·시작 확인 helper를 보존했다. 이번 호출은 마지막 복구점에서 관리 실행·세션·receipt·targeted 수집을 연결했다.

앞선 완료 후보에서 프로젝트 Python과 `LOOP_MANAGER_TARGETED_TEST_RUNNER`로 관련 unit 60개를 실행해 모두 통과했다(Ran 60 tests in 0.942s, OK). 정책 모듈 9개, 시작 확인 모듈 19개, 관리 runtime 모듈 16개와 기존 isolation/provider/guidance/process-start의 관련 case 16개다. 모든 역할의 신규·재개와 session fallback, readonly·상속 충돌·경로·설정 재검사·초기화 거부·보안 오류·중단 불명·저장 실패·session/사용량/timeout 보존을 확인했다. 관리 service fixture는 실제 in-memory invocation ledger와 receipt/targeted 수집을 연결하고 subprocess만 모의해 빈 targeted 기록에서 성공을 합성하지 않는지 확인한다. 기존 provider 일괄 거부 회귀는 현재 계약에 맞게 missing-environment 거부 회귀로 이름을 바꾸고 신규 runtime 회귀로 지원 호출을 확인한다. 무관한 테스트를 제거하지 않았다.

앞선 helper 검증에서는 비일반 파일을 `fdopen` 전에 검사하지 않아 descriptor가 남을 수 있는 실패를 수정했고 동일 단일 case와 최종 모듈 재실행이 통과했다.

```sh
FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_claude_preflight.ClaudePreflightTests.test_broken_links_bad_json_nonobjects_and_dropin_read_errors_are_not_absence
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_claude_preflight.ClaudePreflightTests.test_broken_links_bad_json_nonobjects_and_dropin_read_errors_are_not_absence
```

이번 호출의 기존 isolation 회귀는 고정 import가 공유 inspect_worktree mock을 우회해 실패했다. helper가 기존 모듈의 현재 함수를 참조하도록 교정한 뒤 아래 동일 명령 3개가 통과했다. 첫 test ID는 이후 현재 계약을 설명하는 `test_claude_missing_environment_records_rejection_without_launch`로 변경했고 최종 60개 실행에서도 통과했다.

```sh
FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_write_isolation.WriteIsolationTests.test_unsupported_provider_records_rejection_without_launch tests.test_agent_write_isolation.WriteIsolationTests.test_claude_preflight_rejection_precedes_log_and_process tests.test_provider_runtime.ProviderNeutralAgentRuntimeTest.test_claude_starts_resumes_and_applies_role_permissions
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_write_isolation.WriteIsolationTests.test_unsupported_provider_records_rejection_without_launch tests.test_agent_write_isolation.WriteIsolationTests.test_claude_preflight_rejection_precedes_log_and_process tests.test_provider_runtime.ProviderNeutralAgentRuntimeTest.test_claude_starts_resumes_and_applies_role_permissions
```

CLI·인증·doctor·모델·MDM/OS 분기는 test double이며 보호 파일 쓰기 probe·실계정·실제 Claude·OS 정책 적용과 전체 verify는 실행하지 않았다. 문서/mock 결과를 실제 격리 성공으로 기록하지 않는다. 다음 단계는 Manager 전체 verify·읽기 전용 Review이며 실제 Merge 완료를 선언하지 않는다.

### Review 피드백 교정

시작 HEAD c17adbc9b997b6306c3e76136468bc0bb39b3686에서 세 미해결 피드백을 교정했다. control-only remote의 managedSourcesBehavior도 병합 방식 결정에 사용하며 remote merge·MDM model·file sandbox.enabled=false 조합은 policy 생성에서 거부한다. Bash에는 canonical `/tmp` 아래 짧은 호출별 비공개 root를 전달하고 이 root와 기존 attempt만 추가 허용한다. 긴 상태 경로에서 native가 시스템 fallback으로 바꾸는 변환을 모의해 소유 root 안의 claude-UID와 공유 기본 deny가 양립하는지 확인했다. 실제 시스템 디렉터리 생성은 테스트 fixture로 대체한다. CLI 임시 root의 존재·소유권·권한은 spawn 전 재검사하며 targeted·receipt·결과 기록은 기존 state runtime에 남긴다.

Claude 결과는 기존 원자적 helper로 호출별 last-message.txt에 저장한다. DB 결과 확정을 DatabaseWaitTimeout으로 중단한 모의 관리 호출에서 다음 진입이 동일 invocation·session·원문을 복구하고 새 subprocess를 실행하지 않는지 검증했다. 공용 최신 결과 파일을 다른 내용으로 바꿔도 복구 결과는 유지된다.

최초 실행은 테스트에서 실제 `/tmp` 생성을 시도해 현재 sandbox의 쓰기 범위에 막혔다. 생성은 fixture 안으로 옮기고 production의 짧은 경로 요청과 native 변환을 mock으로 확인하도록 교정한 뒤 동일 명령이 47개 모두 통과했다(Ran 47 tests in 0.810s, OK). 기존 60개 성공을 현재 후보 검증으로 재사용하지 않았다.

```sh
FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_claude_policy tests.test_claude_preflight tests.test_claude_managed_runtime
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_claude_policy tests.test_claude_preflight tests.test_claude_managed_runtime
```

실제 Claude·계정·native OS 적용과 전체 verify는 이번 교정에서도 미실행이다. Manager의 전체 verify·Review가 남아 있다.

## Decision Log

- 지원 여부를 실제로 확인하지 않는 provider 단위 일괄 거부는 안전한 실패 동작이지만 R-023의 지원 환경 검증 완료 근거가 아니다. 동일한 보안 기준 안에서 확인 가능한 호출만 허용하므로 새 제품 목표나 권한 확대가 아니다.
- provider 효력·역할·session·설정 병합이 연결돼 complexity high, 잘못된 확인의 경계 밖 쓰기 영향으로 risk high다. 설정된 Claude 작업의 무인 실행을 복구하는 필수 작업으로 priority high다.
- 원격 실행 gate의 G-161과 공통 지침 주입의 G-147은 이 구현의 선행 조건이 아니다. 기존 성공 호출 계약을 유지해 독립적으로 검증·Merge하며, 전체 차단·재개 감사 G-189는 이 변경 뒤 수행한다.
- 2026-10-09 사용자 요청에 따른 최신 명세 정합화: R-023·R-055의 legacy 기본 checkout 제외를 반영해 과거 실행의 포괄적 엄격 판정 요구를 대체했다. Claude 지원 구현은 공식 정책·유효 설정·모의 호출로 검증하며 유료 실호출 시연을 시작·완료 조건으로 요구하지 않는다. 신규 호출의 쓰기 제한·자동 검토·실제 위반 보존과 선택 호출의 안전 중단은 유지한다. ID·기존 dependency·실행 이력을 보존하며 구현·검증 성공을 기록하지 않는다.
- 2026-10-11 사용자 요청에 따른 문서 보강: 기존 내장 기능 우선·공식 웹 문서와 모의 테스트 개발은 확정된 방향이다. managed 갱신 시 native 제한 유지 경로의 조사 미완료만으로 조직 관리 환경 한정이나 외부 격리가 필요하다고 단정하지 않는다. 실제 선택 호출의 정책 충돌·제한 적용 실패는 안전 중단하고 근거를 보존하며, 기존 기능으로 충족할 수 없는 요구와 조사한 기능·버전·제약·필요 변경을 구체적으로 제시할 때만 요구사항 변경 판단을 요청한다. 두 대안은 미선택이며 기존 ID·dependency·실행 이력과 남은 구현·검증 항목을 유지한다.

- 2026-10-16 수동 복구 정합화: 최신 R-023의 명시적 범위 결정에 따라 실행 중 managed 변경의 감시·적용 전 차단·지속 제한 보장과 실환경 완전성 점검을 현재 구현·검증에서 제외했다. 2026-10-11 기록은 당시 이력으로 보존하며 해당 조사 요구를 현재 완료 조건으로 적용하지 않는다. 기존 질문의 해당 보장을 새로 완화하거나 관측 전 쓰기를 승인하는 변경이 아니다. 시작 설정의 역할별 쓰기 제한·자동 승인 검토와 실제 관측 오류의 안전 중단은 유지한다. Git 상태 차이를 Agent 위반으로 판정하는 옛 요구도 최신 R-023·R-055로 대체하며, 기존 작업물·실패·검증 이력과 ID·메타데이터를 보존한다.
