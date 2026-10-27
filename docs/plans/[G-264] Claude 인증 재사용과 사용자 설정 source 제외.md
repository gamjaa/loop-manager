---
id: G-264
title: Claude 인증 재사용과 사용자 설정 source 제외
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
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

직접 구현된 Claude 공통 홈·기본 인증 연결·호출별 임시 폴더를 재사용하고, 남은 개인 설정 source 제외와 실제 소비 설정의 지문을 조회·호출 경계에 연결한다. G-191의 내장 쓰기 제한·자동 승인 검토와 기존 session fallback을 유지한다. 실제 CLI/session·실계정·OS 호환성 검증은 최신 R-085 Non-Goals에 따라 이번 완료 조건에서 제외한다.

## Acceptance Criteria

- 같은 공급자·상태 루트의 모든 역할·repository·worktree가 `automation-claude`를 직접 사용한다. 구현된 외부 home 무시·기본 사용자 `.credentials.json` 연결·기존 파일/링크·허용 인증 환경값·부모 환경 보존을 유지하며 다른 공급자/상태 루트와 섞이지 않는다.
- 공통 홈 안팎의 개인 settings·Plugin·Hook·MCP·규칙·지침을 자동으로 가져오지 않는다. 설정 디렉터리 밖의 개인 source를 제외하는 호출 구성을 인증/profile·지침·정책 조회와 모델 실행에 일관되게 적용한다. 단순 홈 지정으로 전체 분리를 인정하지 않으며 공통 홈·프로젝트·관리 정책과 명시적 실행 입력은 유지한다.
- Manager는 사용자 settings를 생성·덮어쓰거나 역할별 홈에 연결하지 않는다. 공통 설정의 최초 부재·CLI 생성·수정·원자 교체·삭제·재생성은 현재 설정으로 검사한다. 과거 홈의 설정·인증·session과 끊긴 링크는 보존하고 새 입력으로 검사·복원·동기화하지 않는다. 실제 소비 설정의 읽기 실패·소유권 불명·최종 검사 뒤 변경·권한/확장 충돌은 보존·거부한다.
- G-262의 같은 준비 환경과 설정 scope를 신규·재개·retry·fallback까지 전달한다. 사용하지 않는 개인 설정 변화는 유효 지문을 바꾸지 않으며 실제 공통 홈·프로젝트·관리·Manager 입력 변화는 재검증한다. 개인 Hook을 실행하거나 미소비 개인 설정을 이유로 preflight를 거부하지 않는다.
- 저장된 session ID·repository/worktree/role/작업 결합과 기존 최소 문맥 fallback을 유지한다. 공통 홈의 최근 session으로 대체하거나 과거 자료·작업/실패 이력을 이동·삭제·초기화하지 않는다. 구현된 호출별 임시 폴더·로그·결과·Model/Effort/Fast·지침·권한·사용량 분리를 유지하고 공통 홈·개인 홈·다른 호출의 임시 경로는 도구 쓰기 범위에 넣지 않는다.
- 기본 인증 파일 갱신·원자 교체·CLI의 링크 교체 확인과 기존 credential-store 거부·전용 경로 안내·비밀값 비노출을 재사용한다. 개인 홈 전체 상속·자동 로그인·권한 확대·다른 provider 전환 없이 해당 호출의 실패를 보존하고 독립 Codex 작업을 유지한다. G-191의 정책·Reviewer 읽기 전용과 기존 미지원 native Windows 등 거부 범위를 공통 홈만으로 완화하지 않는다.
- 호출 구성·설정 소비/제외·인증 링크·권한 회귀는 임시 fixture·모의 호출로 확인한다. 기존 session 재개 호환성, Claude CLI 버전/OS/설정 source/session 호환성·실제 동시 모델 실행, 실계정·OS credential store 확인은 R-085의 후속 메모로만 남기며 실제 성공을 합성하지 않는다. 이 제외는 개인 source 제외 구현이나 G-191의 독립 목표를 완료로 처리하는 근거가 아니다.

## Implementation

- [ ] 확인한 `loop_manager_internal/agents/provider_home.py`·`agent_profiles.py`의 공통 홈·기본 인증 연결과 G-262의 준비 환경을 재사용한다. `provider_adapters.py`·`provider_guidance.py`의 조회/실행 입력에 개인 source 제외를 연결하고 부모 환경·사용자 원본을 변경하지 않는다. G-191의 실행별 정책과 이 설정 source 경계를 결합하되 격리 구현을 중복하지 않는다.
- [ ] ARCHITECTURE가 연결한 Claude 공식 설정 source/우선순위 계약에서 개인 source 제외와 공통 홈·프로젝트·관리 정책 유지에 필요한 호출 구성을 조사한다. 설치 CLI·계정·실시간 효과 시연을 필수 gate로 두지 않는다. `--bare` 등 조회 입력을 모델 호출과 대조하고 필수 프로젝트/관리 입력을 생략하는 우회로 쓰지 않는다.
- [ ] `agent_write_isolation.py`의 `_configuration_digest`를 실제 선택한 공통 설정/source와 맞춘다. 준비 중 설정 생성/교체는 현재 내용과 정책을 검토하고 최종 검사 뒤 경쟁은 거부한다. 기존 인증 링크 전후 확인·미지원 거부·최소 문맥 fallback과 호출별 TMPDIR/로그/결과 분리를 재사용한다. 사용하지 않는 과거 runtime 파일의 제거 handler를 추가하지 않는다.
- [ ] `tests/test_provider_home.py`, `tests/test_provider_runtime.py`, `tests/test_agent_write_isolation.py`, `tests/test_native_platform_agents.py`의 fixture를 확장해 개인 source가 호출 입력/지문에 들어가지 않고 실제 정책 판정은 유지되는지 확인한다. PRODUCT·ARCHITECTURE·`docs/OPERATIONS.md`에는 구현한 제외·설정 수명과 실제 미지원 범위를 통합하며 공유 인증 갱신의 기본 파일 영향 안내를 유지한다.

## Validation

- Worker는 변경한 source 판정의 새 unit case와 `python3 -m unittest tests.test_provider_home.ProviderHomeTests.test_common_home_config_lifecycle_uses_current_settings_without_runtime_links tests.test_provider_runtime.ProviderNeutralAgentRuntimeTest.test_policy_failure_stops_retry_and_session_fallback`를 실행한다. 실제 CLI/인증/격리 관측은 mock하며 개인 Hook·Plugin·유료 Agent를 실행하지 않는다.
- 임시 공통 홈과 그 밖의 개인 Hook/settings fixture로 source 제외 입력·소비 지문·충돌 판정을 함께 검사한다. 개인 설정 변화는 지문을 바꾸지 않고 프로젝트·관리 정책 변화는 검사를 요구해야 한다. argv만 확인하거나 source 판정 자체를 mock 통과로 대신하지 않는다.
- 설정 최초 부재→CLI 생성→수정/원자 교체→삭제→재생성을 조회·신규·재개·retry·fallback의 모의 호출에 연결한다. 현재 설정 사용·과거 홈 불변·호출별 임시 경로/결과/역할 입력 분리와 최종 검사 후 변경/읽기 실패의 안전 거부를 확인한다. 현재 동시 resume fixture를 실계정 재개나 동시 모델 실행의 근거로 사용하지 않는다.
- 부분/전체 verify의 고유 회귀는 G-191의 정책/거부·Reviewer 읽기 전용, profile·guidance·timeout·사용량·process 종료·인증 링크·부모 환경/원문 보존과 독립 Codex 지속이다. 기존 미지원 거부를 삭제하거나 실환경 호환성 미수행을 이번 변경의 실패 gate로 추가하지 않는다.

## Progress

- [x] 2026-10-11 planning 기준 `6fc245469a6bd3cced81dbf64fc049130982bbc8`에서 Claude 환경 필터·인증/profile 조회·실행 env 재생성·설정 지문과 G-191을 대조했다. 관리 home 준비 및 사용자 source 제외 연결이 없고 현행 managed isolation은 Claude를 거부한다. 구현·테스트·전체 verify는 실행하지 않았다.
- [x] 2026-10-13 `041e6ee8`에서 직접 반영된 `263cd43`의 상태 루트·기본 인증 링크·동일 사전 조회 환경과 `tests/test_provider_home.py`를 확인했다. 사용자 설정 source 전체 제외의 실제 효과, 지원 credential-store/session 계약과 G-191 이후 실제 호출 연결은 남아 있다. 구현·테스트·verify를 실행하거나 직접 작업의 검증 성공을 만들지 않았다.

최신 직접 구현의 공통 홈·설정 지문 수명과 서로 다른 worktree의 모의 동시 resume·임시 경로/결과 분리를 정적으로 확인했다. 위 과거 조사 기록의 credential-store/session 호환성 표현은 당시 범위이며 최신 R-085 Non-Goals에 따라 이번 완료 조건에서 제외한다. 개인 source 전체 제외와 G-191 이후 정책 연결은 여전히 남아 있다. 테스트는 실행하지 않았다.

- [ ] 남은 개인 source 제외·현재 설정 소비/보존 연결 및 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-13 mutable snapshot `5fb268cdf4cd026adedd8e23a2a66c68d3d8c12a0d6a57f2453cceed1f83e57c`의 ID·경로·dependency·metadata·기존 기록을 보존했다. 최신 R-085의 선택 상태 루트·기본 인증 원본·전용 설정 보존에 맞게 상충 조건을 교정하고 직접 구현 범위와 남은 source/인증·session 판단을 구분했다. 별도 home 도입을 Claude 격리 지원이나 공식 인증 재사용 성공으로 승격하지 않는다.
- 공통 홈·호출별 임시 폴더·저장 session ID 연결은 직접 구현을 재사용한다. 이전 runtime 설정의 분리/삭제·OS keychain 조사·실제 session 재개 호환성을 이번 작업으로 다시 계획하지 않는다. 새로운 인증 저장 방식·OS 지원·권한 확대가 필요한 경우에는 해당 경계를 구현하지 않고 구체적 사용자 판단에 남긴다.
- complexity high: 개인 source 제외와 프로젝트/관리 정책 유지가 동시에 성립하는 호출 구성·소비 지문을 조회/실행/재검사에 일치시킬 판단이 남아 있다. G-191의 쓰기 경계와 이 환경 분리의 책임은 구분하며 모의 근거와 실제 지원 상태도 분리한다.
- risk high: 잘못된 source 제외는 개인 Hook을 실행하거나 조직 정책을 누락할 수 있다. 원본·session·진단을 보존한 안전 거부와 기존 fallback으로 복구하며 source 병합·정책 충돌·인증 실패·중단 보존을 검사한다. 외부 Hook의 부작용을 코드 rollback으로 되돌린다고 가정하지 않는다.
- priority high: 개인 설정을 수정하지 않고 안전한 Claude 무인 호출을 가능하게 하는 필수 작업이다. 기존 dependency·ID·경로·metadata와 과거 기록을 보존하고 실제 선택 호출의 인증/정책 실패는 계속 거부한다. R-085 Non-Goals를 높은 effort나 추가 gate로 대신 결정하지 않는다.
