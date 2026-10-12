---
id: G-264
title: Claude 인증 재사용과 사용자 설정 source 제외
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-085] Codex Claude 인증 전용 자동 실행 환경.md
depends_on:
  - G-262
  - G-191
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

Claude의 관리 호출에 별도 인증 전용 저장소를 적용하고 사용자 설정 source를 제외한다. G-191의 확인된 내장 쓰기 제한·자동 승인 검토를 유지하면서 개인 설정을 가져오지 않고 기존 인증을 재사용한다. Codex 변경과 독립적으로 검증·Merge한다.

## Acceptance Criteria

- scheduler에 `CLAUDE_CONFIG_DIR`이 없어도 관리 상태 경로의 별도 저장소를 선택한다. 명시된 원본 경로 또는 기본 파일/OS 인증 저장소와 기존 지원 인증 환경값 중 공식 CLI가 지원하는 경로로 인증만 재사용한다. 인증 원문이나 인증과 개인 설정이 섞인 파일을 복제·연결하지 않고 새 로그인·비밀 복제 저장을 시작하지 않는다.
- 관리 저장소에는 Manager가 만든 Claude 사용자 설정 파일이나 개인 settings·Plugin·Hook·MCP·규칙·지침의 링크/복제본이 없다. 설정 디렉터리 밖의 사용자 source도 실제 호출에서 제외한다. 단순 `CLAUDE_CONFIG_DIR` 지정만으로 전체 사용자 설정 분리를 인정하지 않으며 프로젝트·관리 정책과 명시적 실행 설정은 유지한다.
- G-262의 같은 환경·설정 scope를 인증/profile·설정/권한 확인, instruction 확인, 신규·재개·retry·fallback에 전달한다. 개인 Hook이 있는 fixture에서 Hook을 실행하거나 그 설정을 이유로 preflight를 거부하지 않는다. 사용하지 않는 개인 설정 변경은 유효 지문을 바꾸지 않고 실제 프로젝트·관리·Manager 설정 변화는 계속 반영한다.
- Model/Effort/Fast·고정 공통/역할별 instruction·권한 입력은 명시적으로 전달한다. G-191의 지원 환경·유효 정책 확인, 프로젝트/관리 확장 충돌 거부·파일/명령 쓰기 범위·자동 승인 검토와 Reviewer 읽기 전용을 유지한다. 사용자 source 제외로 조직 정책이나 프로젝트 지침을 함께 생략하지 않는다.
- 기존 자동 실행 저장소에 개인 설정 연결이 있다면 확인된 Manager 소유 연결만 분리한다. 원본 인증·사용자 설정, session·작업물·로그를 보존하고 regular file·다른 target·소유권 부재·경쟁은 해당 호출을 거부한다. 기존 source 저장소의 session을 재개할 수 없으면 기존 문맥·이력을 보존하는 새 session 복구를 사용한다. 중단·재시작이나 환경 전환으로 작업·retry·교정 이력을 초기화하지 않는다.
- G-191이 지원하는 provider/OS 범위 안에서 인증 재사용 성공·실패와 미수행 근거를 구분한다. 미지원 native Windows 등 기존 거부 범위를 별도 home 도입만으로 허용하지 않는다. 실패 시 개인 home 전체 상속·설정 일괄 복제·다른 provider 전환·대화형 로그인·권한 확대로 우회하지 않고 독립 Codex 작업을 유지한다.
- 부모 환경·원본 저장소는 변경되지 않고 인증값이 prompt·argv·출력·로그·DB에 노출되지 않는다. 실제 CLI·계정 없이 수행한 fixture를 실계정 성공으로 기록하지 않는다.

## Implementation

G-262의 준비·전달 경계와 기존 `agent_profiles.py`, `provider_adapters.py`, `agent_write_isolation.py`, `provider_guidance.py` 및 G-191의 Claude 유효 정책 확인을 재사용한다. 기존 provider runtime의 repository/worktree/role/session 연결을 유지하며 새 인증 전용 저장소를 실제 CLI 조회·실행의 선택으로 결합한다. 공통 home·사용자 전체 디렉터리를 sandbox의 writable 경로에 추가하지 않는다.

설치 CLI와 공식 인증·설정 source/병합 계약에서 별도 디렉터리의 파일 인증, OS keychain identity 및 기존 인증 환경값을 확인한다. 인증 전용 연결이 가능한 자료만 재사용하며 인증과 사용자 설정이 섞인 저장소를 통째로 가져오지 않는다. 사용자 설정 source 제외와 프로젝트·관리 정책 유지가 동시에 성립하는 호출 입력을 적용한다. `--bare` 등 기존 사전 조회의 옵션도 실제 호출과 인증·설정 범위가 같은지 대조하고, 필수 프로젝트/관리 입력을 생략하는 우회로 사용하지 않는다.

소유권과 인증/설정 분리를 증명하지 못하면 해당 호출의 이유와 과거 상태를 보존한다. 새 인증 방식·OS 지원·비밀 저장 방식이나 권한 확대가 필요한 경우 Architecture Change Policy의 구체적 사용자 판단을 남기고 그 부분을 구현하지 않는다. G-191의 provider 쓰기 경계·자동 검토 검증을 재구현하지 않는다. 기존 session 복구와 중단 보존을 연결하고 PRODUCT·ARCHITECTURE 및 `docs/OPERATIONS.md`에 실제 지원 범위·인증 실패 조치를 반영한다.

## Validation

- Worker는 `tests/test_provider_runtime.py`, `tests/test_agent_write_isolation.py`, `tests/test_native_platform_agents.py`와 G-191의 관련 fixture에서 변경 경계의 최소 unit만 실행한다. 기본/명시 인증 원본·인증 환경값, config 부재, 사용자 디렉터리 안팎의 개인 Hook/settings, 프로젝트/관리 source와 충돌을 실제 선택·지문·정책 판정으로 검사한다. 인증 조회·CLI·keychain은 mock하며 실제 사용자 Hook·Plugin·유료 Agent를 실행하지 않는다.
- source 선택·병합의 공식 근거와 fixture에서 실제 사용/제외한 source를 함께 기록한다. argv 문자열 확인만으로 사용자 설정이 제외됐다고 주장하지 않는다. 지원 방식의 성공/실패, OS 인증 identity 불일치·연결 불가·설정 분리 불확실·미지원 환경이 넓은 home/로그인/권한 fallback을 만들지 않는지 확인한다.
- 신규·재개·session-unavailable fallback에서 같은 저장소와 설정 범위, 원본/session/작업물/로그 보존, 개인 설정 변경의 지문 불변·실제 소비 설정 변화의 재검증을 확인한다. 기존 연결의 소유 불명·교체·부분 분리 후 재시작과 비밀값 비노출을 대조한다. mock OS/인증 결과와 실제 실행 근거를 구분해 미수행 범위를 명시한다.
- Manager 전체 verify는 G-191의 Claude 정책·거부·Reviewer 읽기 전용, profile·guidance·timeout·사용량·process 종료, 부모 환경 불변 및 독립 Codex 지속을 유지한다. 기존 version 1 unittest 수집·compile·Agent guard를 유지한다. Reviewer는 개인 설정/혼합 인증 파일 상속, 조직/프로젝트 정책 누락·지원 범위 확대와 과거 실행 성공 합성을 blocking issue로 확인한다.

## Progress

- [x] 2026-10-11 planning 기준 `6fc245469a6bd3cced81dbf64fc049130982bbc8`에서 Claude 환경 필터·인증/profile 조회·실행 env 재생성·설정 지문과 G-191을 대조했다. 관리 home 준비 및 사용자 source 제외 연결이 없고 현행 managed isolation은 Claude를 거부한다. 구현·테스트·전체 verify는 실행하지 않았다.
- [ ] 인증 재사용·사용자 source 제외·보존 복구 및 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity high: CLI 설정 source와 OS별 인증 identity를 개인 설정 상속 없이 유지하고 기존 session·권한·관리 정책과 함께 검증할 판단이 남아 있다. G-191의 쓰기 경계와 이 환경 분리의 책임은 구분한다.
- risk high: 잘못된 source 제외는 개인 Hook을 실행에 포함하거나 조직 정책을 누락하고 인증·session 재개를 깨뜨릴 수 있다. 원본·session·진단을 보존한 안전 거부와 기존 문맥 fallback을 복구 수단으로 유지하며 source 병합·정책 충돌·인증 실패·중단 보존을 AC·Validation에서 확인한다. 외부 Hook의 부작용을 코드 rollback으로 되돌린다고 가정하지 않는다.
- priority high: 개인 설정을 수정하지 않고 안전한 Claude 무인 호출을 가능하게 하는 필수 작업이다. G-191 Merge와 공통 환경 연결 뒤 진행하며, 개발 CLI/계정 부재는 모의 검증의 한계로 기록한다. 실제 선택 호출의 인증/정책 확인 실패는 그대로 거부하고 R-085의 비목표인 새 계정 관리·OS 지원 확대를 추가하지 않는다.
