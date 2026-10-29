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

## 공식 호출 구성 조사와 중간 구현

현재 구현은 설정 source 제외·역할별 native 요청 정책 생성기·독립 시작 확인 helper까지 반영한 중간 후보이며, 관리 Claude 호출의 provider 단위 안전 거부는 유지한다. 역할별 전체 쓰기 경계·유효 정책 확인·호출 증거 연결을 완료했다고 선언하지 않는다.

2026-10-13 Worker의 공식 문서·changelog v2.1.296 조사에서는 managed 설정이 실행별 sandbox 설정을 대체할 수 있고 서버 갱신을 ConfigChange hook으로 사전 차단할 수 없다는 제약을 기록했다. 이는 문서 조사이며 실호출 관측이 아니다. 당시 별도 OS 경계 또는 구현 보류 판단이 필요하다고 기록했지만, 2026-10-16에 반영된 최신 R-023은 실행 중 managed 변경 대응을 현재 범위에서 제외했다. 따라서 그 조사 제약을 현재 구현의 판단 대기나 외부 격리 도입 조건으로 사용하지 않는다. 사용자 결정을 새로 작성하지 않으며 기존 Decision Log의 경위를 보존한다.

2026-10-29 관리 rebase 뒤 a30388e1 기준 전체 diff와 현재 sources·PRODUCT·ARCHITECTURE를 다시 확인하고 공식 문서를 새로 조회했다.

| 공식 근거 | 현재 구현에 적용할 계약 |
|---|---|
| [Sandbox 설정](https://code.claude.com/docs/en/sandboxing#configure-sandboxing)·[CLI 옵션](https://code.claude.com/docs/en/cli-reference) | 빈 배열은 상속된 배열을 지우지 않는다. `--setting-sources`의 빈 문자열은 user/project/local source를 제외하며, 제외 source의 sandbox filesystem·Edit 권한을 sandbox 구성에서도 제외하는 계약은 v2.1.246 이상이다. 시작 확인 helper는 v2.1.307 이상을 검사한다. 이는 excluded-source 병합과 nonempty objects/refs 정리 보정을 포함하는 기준이며 관리 실행 연결은 아직 미완료다. |
| [파일 권한](https://code.claude.com/docs/en/permissions#read-and-edit) | Edit·Write·NotebookEdit의 경로별 제한은 `Edit(path)`를 사용한다. 파일 권한의 `//절대경로`와 sandbox의 `/절대경로`를 구분하고 임의 subprocess에는 sandbox를 적용해야 한다. |
| [CLI의 restricted 구성](https://code.claude.com/docs/en/cli-reference) | v2.1.248 이상의 `--restricted`는 파일 도구를 working directories에 한정하고 managed·실행별 설정만 로드한다. 명령 도구는 기본 제거되지만 `--tools`에 개별 이름을 명시해 사용할 수 있으므로 파일 도구의 workspace 밖 쓰기 거부를 구성할 native 후보로 남긴다. 요청 정책 생성기에 적용하고 단위 검증했지만 관리 호출에는 아직 연결하지 않았다. Bash sandbox·Git·임시 경로 보호를 대체하지 않는다. |
| [설정 우선순위](https://code.claude.com/docs/en/settings#settings-precedence)·[permission mode](https://code.claude.com/docs/en/permission-modes) | 실행별 설정과 source 제외로 managed 설정까지 제거되지 않는다. 시작 정책 충돌은 별도 확인해야 하며 `auto`를 `acceptEdits`나 sandbox 명령 일괄 승인으로 대체하지 않는다. |

앞선 복구점은 공통 `claude_permission_arguments`에서 `--setting-sources`와 빈 문자열을 별도 인자로 전달한다. 신규·재개가 같은 helper를 사용하고 기존 `auto`·Fast 요청·Reviewer 파일 도구 금지·strict sandbox 요청을 유지한다. 이 source 선택만으로 managed 충돌·전체 쓰기 경계나 실제 정책 적용을 확인했다고 판단하지 않는다.

앞선 복구점 호출에서는 프로젝트 Python과 `LOOP_MANAGER_TARGETED_TEST_RUNNER`로 다음 세 unit case를 새로 실행해 모두 통과했다. `tests.test_provider_runtime.ProviderNeutralAgentRuntimeTest.test_claude_starts_resumes_and_applies_role_permissions`는 모의 신규 Worker·재개 Reviewer 호출의 빈 source 선택 인자와 자동 검토·sandbox 요청을 확인했다. `tests.test_agent_write_isolation.WriteIsolationTests.test_unsupported_provider_records_rejection_without_launch`와 `test_claude_preflight_rejection_precedes_log_and_process`는 기존 관리 호출 거부와 프로세스 미시작을 확인했다. 이전 후보의 성공을 재사용하지 않았다. 실제 Claude CLI·계정 호출과 전체 verify는 실행하지 않았으며 이 결과는 모의 호출·안전 거부 회귀만의 근거다.

2026-10-29 후속 호출은 Manager가 게시한 복구점 2f794f630c61991144dbc06056bef0bbb6ec9237에서 이어서 `agent_claude_policy.policy_arguments`를 구현했다. canonical source·다른 worktree·shared/worktree Git과 `.git` pointer를 `Edit`의 정확 경로·하위 경로 거부 및 sandbox `denyWrite`에 함께 넣는다. Reviewer는 모든 파일 편집 도구와 workspace의 명령 쓰기를 거부하면서 읽기·Bash 조사와 호출 소유 임시 쓰기 요청은 유지한다. restricted working directories는 workspace와 호출별 임시 경로로 한정하고, `auto`·`classifyAllShell=true`·strict sandbox·socket 및 namespace 확대 금지를 명시한다. 시스템 기본 per-user 임시 경로는 거부하며 Fast 요청도 보존한다.

시작 시 해석한 managed 정책을 입력받는 생성기는 상속된 추가 디렉터리·범위 밖 allowWrite·명령 승인 규칙·자동 검토 비활성화·격리 밖 예외·환경·확장·hook 충돌과 잘못된 JSON 타입을 거부한다. 빈 배열로 상속 배열을 지웠다고 판단하지 않는다. 경로 중첩·symlink alias·플랫폼별 해석이 달라질 수 있는 glob 문자가 들어간 root는 모호한 권한 대신 오류를 반환한다. native Windows는 POSIX 전용 경로 처리 전에 거부한다. 시작 source 수집·확인 helper는 다음 단계에서 구현했으나 관리 실행에 연결하기 전이므로 요청 설정과 시작 입력 관측·실제 적용 성공을 구분한다.

새 `tests/test_claude_policy.py`의 9개 unit case로 두 도구 경계의 보호 경로, Reviewer 읽기·쓰기 분리, 자동 검토·strict sandbox·기본 임시 경로 거부, 상속 충돌·타입·경로 중첩·symlink·Windows 거부를 검증했다. 테스트는 Agent·subprocess·보호 파일 쓰기 probe를 실행하지 않으며 설정 생성·거부 로직만 확인한다. Linux에서 실행했고 Windows case도 플랫폼 모의 결과이며 native Windows 실행 성공이 아니다. 기존 세 회귀의 앞선 복구점 성공과 현재 생성기의 단위 검증을 구분한다.

복구점 c99603ba78143ec20fea677c1ebd684793590355 이후에는 `agent_claude_preflight`에서 source 수집·CLI 시작 확인·재검사 단위를 구현했다. managed 파일과 이름순 JSON drop-in을 병합하고 remote·MDM·파일의 first-wins/merge 순위, 최고 source의 defaultMode, 하위 admin source의 env·filesystem 비활성화·sandbox 실행 파일 override를 구분한다. 개인·project·local 설정은 실행 입력에서 제외하며 끊어진 링크·잘못된 JSON·비일반 파일·읽기 실패를 부재로 처리하지 않는다. source 경로·resolved 경로·내용 지문과 비밀값 없는 환경 경로만 관측하고 원문·인증값은 결과에 넣지 않는다. macOS는 MDM domain 조회·해석 실패와 부재를 구분한다. WSL의 Windows 정책 상속은 아직 해석하지 않아 해당 환경의 시작 확인에서 구체적으로 거부한다.

이 helper는 보호 경로와 겹치지 않는 비공개 runtime·호출별 임시 경로를 준비하고 CLI v2.1.307 이상·필수 native 옵션·인증 상태와 공통 홈·읽기 전용 doctor 진단을 확인한다. 인증·doctor 조회에도 restricted·source 제외·MCP 제한을 전달하며 doctor 뒤 새 remote source와 정책 충돌을 다시 확인한다. `assert_current`는 역할·workspace·repository·임시 경로·소유 권한·설정 지문·CLI를 재확인하고 삭제된 경로를 재생성하지 않는다. 다른 worktree 목록의 정상 변화나 Git snapshot 비교는 접근 위반으로 사용하지 않는다. 반환 자료에는 문서 기반 요청 정책과 `runtime_enforcement=not_observed`, 실제 호출에서 확인해야 할 auto mode 지원을 명시한다. 모델·계정의 auto 지원 확인·invocation 증거 저장·관리 프로세스 실행 연결은 아직 미완료이며 이 helper 결과만으로 호출을 허용하지 않는다.

현재 최종 helper와 `tests/test_claude_preflight.py`의 18개 targeted unit case를 프로젝트 Python·Manager targeted runner로 실행해 통과했다. source 순위·배열 병합·숨겨진 cross-source 충돌·오류 보존·인증 홈·호출별 임시 격리·CLI/설정 재검사·비밀값 제외와 MDM/WSL의 모의 진단을 확인했다. 비일반 파일 회귀를 추가하는 과정에서 `fdopen`이 파일 종류 검사보다 먼저 실패하고 descriptor가 남을 수 있음을 확인해 검사 순서와 finally 닫기를 수정했다. 최초 실패 명령과 수정 뒤 동일 재실행은 다음과 같고, 동일 case 재실행 및 이후 최종 모듈 18개가 모두 통과했다.

```sh
FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_claude_preflight.ClaudePreflightTests.test_broken_links_bad_json_nonobjects_and_dropin_read_errors_are_not_absence
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_claude_preflight.ClaudePreflightTests.test_broken_links_bad_json_nonobjects_and_dropin_read_errors_are_not_absence
```

CLI·인증·doctor·MDM은 test double이며 실제 Claude·계정·OS 정책 적용·모델 호출과 전체 verify는 실행하지 않았다. 이 검증을 실제 격리 성공으로 기록하지 않는다. 다음 구현은 모델·계정 auto 지원의 실제 시작 메시지 확인, 신규·재개·fallback의 prepared 증거 영속화·실행 직전 재검사·실제 오류 보존·프로세스 receipt/targeted 전달 연결이다. 기존 provider 거부·필수 검증·미완료 Progress를 유지하고 시작 확인 helper 단위의 중간 checkpoint를 요청한다.

## Decision Log

- 지원 여부를 실제로 확인하지 않는 provider 단위 일괄 거부는 안전한 실패 동작이지만 R-023의 지원 환경 검증 완료 근거가 아니다. 동일한 보안 기준 안에서 확인 가능한 호출만 허용하므로 새 제품 목표나 권한 확대가 아니다.
- provider 효력·역할·session·설정 병합이 연결돼 complexity high, 잘못된 확인의 경계 밖 쓰기 영향으로 risk high다. 설정된 Claude 작업의 무인 실행을 복구하는 필수 작업으로 priority high다.
- 원격 실행 gate의 G-161과 공통 지침 주입의 G-147은 이 구현의 선행 조건이 아니다. 기존 성공 호출 계약을 유지해 독립적으로 검증·Merge하며, 전체 차단·재개 감사 G-189는 이 변경 뒤 수행한다.
- 2026-10-09 사용자 요청에 따른 최신 명세 정합화: R-023·R-055의 legacy 기본 checkout 제외를 반영해 과거 실행의 포괄적 엄격 판정 요구를 대체했다. Claude 지원 구현은 공식 정책·유효 설정·모의 호출로 검증하며 유료 실호출 시연을 시작·완료 조건으로 요구하지 않는다. 신규 호출의 쓰기 제한·자동 검토·실제 위반 보존과 선택 호출의 안전 중단은 유지한다. ID·기존 dependency·실행 이력을 보존하며 구현·검증 성공을 기록하지 않는다.
- 2026-10-11 사용자 요청에 따른 문서 보강: 기존 내장 기능 우선·공식 웹 문서와 모의 테스트 개발은 확정된 방향이다. managed 갱신 시 native 제한 유지 경로의 조사 미완료만으로 조직 관리 환경 한정이나 외부 격리가 필요하다고 단정하지 않는다. 실제 선택 호출의 정책 충돌·제한 적용 실패는 안전 중단하고 근거를 보존하며, 기존 기능으로 충족할 수 없는 요구와 조사한 기능·버전·제약·필요 변경을 구체적으로 제시할 때만 요구사항 변경 판단을 요청한다. 두 대안은 미선택이며 기존 ID·dependency·실행 이력과 남은 구현·검증 항목을 유지한다.

- 2026-10-16 수동 복구 정합화: 최신 R-023의 명시적 범위 결정에 따라 실행 중 managed 변경의 감시·적용 전 차단·지속 제한 보장과 실환경 완전성 점검을 현재 구현·검증에서 제외했다. 2026-10-11 기록은 당시 이력으로 보존하며 해당 조사 요구를 현재 완료 조건으로 적용하지 않는다. 기존 질문의 해당 보장을 새로 완화하거나 관측 전 쓰기를 승인하는 변경이 아니다. 시작 설정의 역할별 쓰기 제한·자동 승인 검토와 실제 관측 오류의 안전 중단은 유지한다. Git 상태 차이를 Agent 위반으로 판정하는 옛 요구도 최신 R-023·R-055로 대체하며, 기존 작업물·실패·검증 이력과 ID·메타데이터를 보존한다.
