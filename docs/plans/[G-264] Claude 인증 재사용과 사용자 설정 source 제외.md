---
id: G-264
title: Claude 인증 재사용과 사용자 설정 source 제외
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-262
  - G-191
  - G-265
  - G-266
  - G-267
  - G-268
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: high
risk: high
priority: high
---


## Purpose

Claude의 인증·profile 조회와 관리 모델 호출을 R-085의 같은 설정 source·수명 계약에 맞춘다. 공통 홈·인증 링크와 G-191의 managed source 지문·역할별 정책·prepared/실제 관측 구분은 재사용한다. 현재 user/project/local 일괄 제외로 남은 공통 홈·프로젝트 설정 소비와 별도 조회의 scope 차이만 교정한다. 개인 source 제외·native 쓰기 제한·auto 검토는 유지하며 실계정·OS·session 호환성 실검증은 완료 조건으로 추가하지 않는다.

## Acceptance Criteria

- 같은 공급자·상태 루트의 모든 역할·repository·worktree가 `automation-claude`를 직접 사용한다. 구현된 외부 home 무시·기본 사용자 `.credentials.json` 연결·기존 파일/링크·허용 인증 환경값·부모 환경 보존을 유지하며 다른 공급자/상태 루트와 섞이지 않는다.
- 개인 홈과 설정 디렉터리 밖의 개인 settings·Plugin·Hook·MCP·규칙·지침을 가져오지 않는다. 인증/profile·지침 기능 조회와 모델 실행이 선택된 공통 홈의 현재 설정·프로젝트·관리 정책 및 명시적 실행 입력을 같은 계약으로 사용한다. 일반 설정 상속을 개인 홈 전체 상속으로 바꾸지 않으며 R-023의 쓰기 제한·auto 검토와 확장 충돌 거부를 유지한다.
- Manager는 사용자 settings를 생성·덮어쓰거나 역할별 홈에 연결하지 않는다. 공통 홈 설정의 최초 부재·CLI 생성·수정·원자 교체·삭제·재생성은 다음 호출의 현재 상태로 소비·검사한다. 미소비 개인 설정은 보존하되 정책 지문·거부 조건으로 사용하지 않는다. 과거 홈·session·끊긴 링크를 이동·복원하지 않으며 실제 소비 설정의 읽기 실패·소유권 불명·최종 검사 뒤 변경·권한/확장 충돌은 보존·거부한다.
- `validate_provider_runtime()`의 인증 조회, `claude_model_efforts()`의 model/effort 조회, `provider_guidance.claude_arguments()`의 기능 조회와 관리 preflight·신규·재개·retry·fallback의 호출 환경·source 계약이 어긋나지 않는다. 미소비 개인 Hook을 실행하거나 그 설정만으로 거부하지 않는다. 모델 조회의 `--bare`가 본 호출과 동등한 설정 소비·정책 확인을 증명한다고 간주하지 않는다.
- 저장된 session ID·repository/worktree/role/작업 결합과 기존 최소 문맥 fallback을 유지한다. 공통 홈의 최근 session으로 대체하거나 과거 자료·작업/실패 이력을 이동·삭제·초기화하지 않는다. 호출별 임시 폴더·로그·결과·Model/Effort/Fast·지침·권한·사용량을 분리하고 공통 홈·개인 홈·다른 호출의 임시 경로는 도구 쓰기 범위에 넣지 않는다.
- 기본 인증 파일 갱신·원자 교체·CLI의 링크 교체 확인과 기존 credential-store 거부·전용 경로 안내·비밀값 비노출을 재사용한다. 개인 홈 전체 상속·자동 로그인·권한 확대·다른 provider 전환 없이 해당 호출의 실패를 보존하고 독립 Codex 작업을 유지한다. G-191의 정책·Reviewer 읽기 전용과 기존 native Windows 등 미지원 거부를 완화하지 않는다.
- 호출 구성·설정 소비/제외·인증 링크·권한 회귀는 임시 fixture·모의 호출로 확인한다. R-085 Non-Goals의 session/CLI·실계정·OS 호환성은 완료 조건으로 추가하지 않는다. 현재 `prepared` 구성 근거를 실제 OS 격리 성공이나 Review 재사용용 `verified` 증거로 바꾸지 않는다.

## Implementation

- [x] ARCHITECTURE가 연결한 공식 호출·설정 source 계약으로 개인 source 제외와 공통 홈·프로젝트 설정 소비가 함께 성립하는 일반 호출 구성을 확인한다. `agent_claude_policy.policy_arguments()`의 restricted·빈 setting-sources는 현재 구현이며 R-085의 요구를 대체하는 승인이 아니다. 역할별 유효 경계·managed 우선순위·배열 병합·확장 거부를 유지하며 임의 source 일괄 무시·보안 정책 완화로 해결하지 않는다. 실제 선택 호출의 미지원/정책 오류는 구체적으로 보존하고 실시간 managed 감시·새 infrastructure를 도입하지 않는다.
- [x] 확인한 `loop_manager_internal/agents/agent_profiles.py:validate_provider_runtime()`의 source 선택 인수가 없는 `claude auth status`, `claude_model_efforts()`의 `--bare --init-only`, `provider_guidance.py:claude_arguments()`의 help 조회를 관리 preflight/모델 호출과 대조해 필요한 차이만 연결한다. `provider_home.py`의 인증 준비·링크 검사와 `prepared_environment()`를 재사용하며 개인 설정·부모 환경은 변경하지 않는다. 조회별 필요한 scope를 명시하고 모든 조회에 모델 실행·쓰기 권한을 부여하지 않는다.
- [x] 현재 Claude의 지문 소유자인 `agent_claude_preflight.configuration()`·`assert_current()`에 필요한 공통 홈·프로젝트 source 소비와 실제 지문을 연결한다. G-191의 managed 병합·정책 생성·spawn 전 재검사는 재사용한다. Codex/legacy `_configuration_digest()`를 관리 Claude의 현재 소비 근거로 대용하거나 별도 설정 서비스·과거 runtime 제거 handler를 만들지 않는다.
- [x] `tests/test_provider_home.py`·`tests/test_provider_runtime.py`의 인증/profile 조회 fixture와 `tests/test_claude_preflight.py`·`tests/test_claude_managed_runtime.py`의 실제 source 판정을 연결한다. 현재 lifecycle 테스트의 Claude 분기는 인증 준비와 legacy digest만 검사하므로 관리 호출의 설정 소비 근거로 대체하지 않는다. 현재 `test_ignored_personal_project_settings_and_auth_values_do_not_enter_source_identity`의 개인/프로젝트 혼합 기대는 개인 source 불변과 소비하는 공통 홈·프로젝트 lifecycle로 구분해 교정하고 managed 변경 거부를 유지한다. `tests/test_claude_policy.py`의 source 인수 기대도 구현에 맞추되 파일/명령 보호·auto 검토·상속 충돌 assertion은 유지한다. PRODUCT·ARCHITECTURE·운영 안내에는 실제 구현한 계약만 통합한다.

## Validation

- Worker targeted unit은 위 다섯 테스트 파일에서 변경한 조회 scope·source lifecycle·정책 인수의 최소 case다. CLI·인증·process는 double, source 읽기·지문·원본 파일 보존 판정은 실제 helper를 사용한다. G-191의 역할별 정책·병합·OS 거부 행렬을 복제하지 않는다.
- 임시 개인 settings/Hook과 공통 홈·프로젝트·managed 자료를 구성해 조회별 argv·환경과 실제 소비 지문을 함께 검사한다. 개인 설정 변화는 지문 불변·Hook 무호출이어야 하고, 소비 source의 생성·원자 교체·삭제·재생성은 다음 관리 호출의 현재 판정에 반영돼야 한다. source 판정 자체를 성공 mock하거나 인수만 검사해 정책 동일성을 주장하지 않는다.
- 인증 조회→profile/지침 준비→모델 실행 직전 사이의 실제 소비 source 변경·읽기 실패는 안전하게 거부하고 원문·session·호출 이력을 보존한다. 정상 lifecycle과 미소비 source 변경만으로 실패하지 않아야 하며 신규·재개·fallback이 같은 계약을 사용한다. 권한 확대·managed 충돌·Reviewer 쓰기 거부·실제 관측 오류 처리는 유지하고 개인 Hook·Git·유료 Agent를 실행하지 않는다.
- 현재 managed runtime의 source drift·중단·DB 결과 복구와 인증 링크/부모 환경 회귀를 재사용한다. 공통 홈·과거 session·다른 호출의 임시 자료를 보존하고 실제 동시 실행·실계정 성공을 합성하지 않는다. unittest/protocol 1·launcher Python 3.11–3.14·필수 compile·Agent guard를 유지하며 OS 실검증·실시간 managed 갱신 감시를 추가 gate로 삼지 않는다.

## Progress

- [x] 2026-10-11 planning 기준 `6fc245469a6bd3cced81dbf64fc049130982bbc8`에서 Claude 환경 필터·인증/profile 조회·실행 env 재생성·설정 지문과 G-191을 대조했다. 관리 home 준비 및 사용자 source 제외 연결이 없고 현행 managed isolation은 Claude를 거부한다. 구현·테스트·전체 verify는 실행하지 않았다.
- [x] 2026-10-13 `041e6ee8`에서 직접 반영된 `263cd43`의 상태 루트·기본 인증 링크·동일 사전 조회 환경과 `tests/test_provider_home.py`를 확인했다. 사용자 설정 source 전체 제외의 실제 효과, 지원 credential-store/session 계약과 G-191 이후 실제 호출 연결은 남아 있다. 구현·테스트·verify를 실행하거나 직접 작업의 검증 성공을 만들지 않았다.

위 credential-store/session 호환성 표현은 당시 조사 기록이며 최신 R-085 Non-Goals에 따라 완료 조건에서 제외한다. 원격 `d51829e329dc2ea6bda81f63f049d6caffeea1b8`에는 G-191의 source 제외·managed 지문·준비/실행 연결과 모의 회귀가 반영돼 있다. 이미 충족한 연결은 재작업하지 않고 일괄 제외와 남은 조회별 scope·설정 수명 정합성만 아래에 남긴다. 이번 Planner는 테스트를 실행하지 않았다.

- [x] 개인 source 제외를 유지하는 공통 홈·프로젝트 설정 소비/수명과 인증/profile/지침 조회 정합화·최소 회귀
- [x] 2026-11-01 관리 rebase 뒤 `origin/main`·시작 HEAD `b1a4b161d3bb546b26e1b32d864d10ab6fd587d1`과 현재 sources·dependency 경로를 다시 확인했다. base 대비 기존 구현 diff는 없었고 이번 호출에서 아래 교정과 최소 회귀를 구현했다.
- [x] 공식 CLI·settings·permissions 계약을 대조해 user/project/local을 모두 무시하는 restricted 대신 일반 source 선택과 native working-directory 검사·범위 밖 prompt 거부를 사용했다. 역할별 sandbox·auto 검토·보호 경로 deny·Reviewer 읽기 전용은 유지한다. 실제 읽는 공통 홈·project·local·managed source를 지문에 포함하고 읽기/소유권·외부 alias·확장/권한 충돌 및 조회 사이·spawn 전 변경을 거부한다. model/effort는 bare를 제거한 init-only이며 인증·지침 help와 같은 source 선택, 빈 tool·전체 tool deny·strict 빈 MCP·hook 비활성화를 사용한다.
- [x] 기존 개인/프로젝트 일괄 무시 회귀를 개인 source 지문 불변과 실제 소비 source lifecycle 회귀로 교체했다. 나머지 managed 병합·권한·보호 파일·관측 실패 검증은 유지했다. linked worktree의 local root와 native fallback, 공통 홈 생성·수정·원자 교체·삭제·재생성, fallback 다음 호출의 새 설정 소비·기존 session 보존을 fixture/모의 CLI로 확인했다. PRODUCT·ARCHITECTURE·OPERATIONS를 현재 구현과 맞췄다.
- [x] 현재 후보의 targeted runner 기록: 정책/preflight/managed runtime 50개는 최초 진단 문구 차이 2개 실패를 공통 재검사의 구체 진단 보존으로 교정한 뒤 동일 명령으로 50개 통과했다. 인증/조회 9개는 새 fixture의 부모 directory 누락 오류 1개를 수정하고 동일 명령으로 9개 통과했다. 이후 local root·조회 사이 검사와 fallback 회귀를 보강하고 preflight class 20개, 조회·관리 신규/재개/fallback·지침/바인딩/profile drift 6개를 최소 재실행해 모두 통과했다. 이전 후보의 검증을 재사용하지 않았다.
- [x] 2026-11-01 결과 보고 교정: 최종 TARGETED_TESTS에서 과거 진단을 명령별 FAILED_COMMAND/RERUN 쌍 없이 요약해 미해결로 판정된 경로를 읽기 전용으로 확인했다. 기존 실행 기록은 보존하고 이번 호출의 targeted runner로 당시 관련 3개 case와 source lifecycle·개인 source 불변·local root·fallback·파일/명령 정책 5개 case를 다시 실행해 8개 모두 통과했다. 최종 보고는 이번 호출의 실제 결과로 작성한다. 구현 코드·Manager 판정·상태는 변경하지 않았다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-13 mutable snapshot `5fb268cdf4cd026adedd8e23a2a66c68d3d8c12a0d6a57f2453cceed1f83e57c`의 ID·경로·dependency·metadata·기존 기록을 보존했다. 최신 R-085의 선택 상태 루트·기본 인증 원본·전용 설정 보존에 맞게 상충 조건을 교정하고 직접 구현 범위와 남은 source/인증·session 판단을 구분했다. 별도 home 도입을 Claude 격리 지원이나 공식 인증 재사용 성공으로 승격하지 않는다.
- 공통 홈·호출별 임시 폴더·저장 session ID 연결은 직접 구현을 재사용한다. 이전 runtime 설정의 분리/삭제·OS keychain 조사·실제 session 재개 호환성을 이번 작업으로 다시 계획하지 않는다. 새로운 인증 저장 방식·OS 지원·권한 확대가 필요한 경우에는 해당 경계를 구현하지 않고 구체적 사용자 판단에 남긴다.

- R-085의 “CLI가 공통 홈에 설정을 생성·수정·원자적으로 교체한 뒤에는 다음 호출이 그 홈의 현재 상태를 사용한다.”와 “설정 상속은 공통 홈·프로젝트·관리 설정을 사용하는 CLI 기본 동작을 따른다.”를 현재 변경 목표로 유지한다. ARCHITECTURE의 일괄 제외와 해당 테스트의 존재는 이 목표를 철회하는 사용자 결정이 아니다. 기존 명세의 구현 교정을 새 정책 질문으로 바꾸지 않으며 ID·경로·dependency·분류·과거 조사 기록을 보존한다. G-191의 완료 구현을 소급 계획하거나 자동 검증/완료를 합성하지 않는다. G-292의 기록 전달 회귀는 source 정책 변경 없이 독립 검증할 수 있다.
- complexity high: 개인 source 제외·공통 홈/프로젝트의 현재 설정 소비·managed 우선순위와 역할별 native 쓰기/auto 정책이 동시에 성립하는 호출 구성을 정하고 인증/profile/모델 실행·지문 재검사에 일치시킬 기술 판단이 남아 있다. 기존 준비·정책 생성·관측 경계를 재사용하며 실제 불가능한 지원 조건을 확인하면 원인과 영향을 보존한다.
- risk high: 잘못된 source 선택은 개인 Hook 실행·조직 정책 누락·쓰기 범위 확대를 만들 수 있다. 실제 소비 source의 사전 판정·최종 재검사와 기존 안전 거부로 완화하며 원문·session·진단·실패 이력을 보존한다. 외부 Hook의 부작용을 코드 rollback으로 복구한다고 가정하지 않는다.
- priority high: 개인 설정을 수정하지 않고 안전한 Claude 무인 호출의 조회·실행 계약을 맞추는 필수 작업이다. R-023/R-085의 기존 목표를 유지하고 실환경 미확인 때문에 추가 승인·infrastructure·완료 gate를 만들지 않는다.

## Implementation Notes

공식 근거는 [CLI reference](https://code.claude.com/docs/en/cli-reference)의 setting-sources·restricted·permission-prompts·init-only와 [settings precedence](https://code.claude.com/docs/en/settings#settings-precedence), [local root](https://code.claude.com/docs/en/settings#where-claude-code-keeps-the-local-file-in-a-git-repository), [working directories](https://code.claude.com/docs/en/permissions#working-directories), [범위 밖 파일 쓰기](https://code.claude.com/docs/en/permissions#symlinks)다. source를 무시하는 옵션을 홈 재사용의 근거로 삼지 않고 공통 홈·프로젝트를 실제 설정 입력으로 검증한다. managed 우선순위와 native 배열 병합을 유지하며 알려지지 않은 key·확장과 보안 충돌은 거부한다. 파일 source 원문·인증 비밀은 저장하지 않고 경로/내용 지문만 남긴다. 병합 설정 지문은 reason의 `effective_settings_digest`로 구분한다.

전체 verify·integration·실제 Agent CLI·계정·OS 격리·session 호환성 검증은 Worker가 실행하지 않았다. 기존 verify-inputs의 `loop_manager_internal/**`·`tests/**`가 이번 코드와 테스트를 포함한다. 목록 밖 파일의 비소비 identity는 별도로 선언하지 않으며 Manager의 전체 verify·Review 경계를 유지한다. Requirement·frontmatter·사용자 결정·과거 기록·인증 자료·session은 변경하지 않았다.
