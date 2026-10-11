---
id: G-263
title: Codex 인증 전용 저장소와 기존 config 연결 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-085] Codex Claude 인증 전용 자동 실행 환경.md
depends_on:
  - G-262
complexity: high
risk: high
priority: high
---

## Purpose

Codex의 관리 home에서 개인 설정 상속을 제거하고 기존 인증만 재사용한다. Linux·macOS의 개인 config 연결과 native Windows의 사용자 home 전체 재사용을 바꾸되 기존 session·작업·로그 및 source 인증을 보존한다.

## Acceptance Criteria

- scheduler에 `CODEX_HOME`이 없어도 Manager 상태 경로 아래의 별도 저장소를 선택하고 기본 사용자 저장소/지원 인증 저장소의 기존 인증을 재사용한다. 명시된 원본 `CODEX_HOME`도 인증 원본으로만 사용한다. 부모 환경과 원본 저장소를 변경하지 않는다.
- 새 관리 home에는 Manager가 생성한 `config.toml`이나 개인 설정 링크가 없다. 개인 Plugin·Hook·MCP·규칙·지침·profile을 복제·연결하지 않는다. 개인 설정이 있는 fixture와 설정 파일이 전혀 없는 fixture 모두 명시적인 Model/Effort/Fast·instruction·sandbox·승인 입력으로 호출할 수 있다.
- G-262의 준비된 환경에서 인증 검사·기능 확인·설정 검사·신규·재개·fallback을 수행한다. 사용하지 않는 개인 Hook/MCP·profile 때문에 preflight를 거부하지 않고 개인 설정만의 변경은 유효 설정 지문을 바꾸지 않는다. 프로젝트·관리 설정의 실제 충돌과 호출 직전 정책 변경은 계속 거부한다.
- 기존 config 연결은 해당 repository/worktree/role runtime의 소유권·경로·링크 종류·원본 target과 현재 상태를 확인한 경우에만 링크 자체를 분리한다. 원본 config/auth, 인증 연결, 기존 session·작업 파일·로그는 보존한다. regular file·다른 target·alias·소유권 미확인 또는 검사 뒤 교체는 임의 삭제·덮어쓰기 없이 해당 호출을 거부한다. 중단 후에는 현재 상태를 재검사하며 이미 분리된 정상 home을 반복 변경하지 않는다.
- Linux·macOS와 기존 지원 native Windows에서 공식 CLI의 인증 선택·저장소 의미에 맞는 성공/실패 경로를 확인한다. 파일 인증은 기존 인증 전용 연결을 재사용하고 인증 원문을 복사·이동·삭제하거나 새 인증 저장 방식을 도입하지 않는다. 연결/OS 인증 재사용을 확인할 수 없는 환경은 안전 실패하며 개인 home 전체 사용·로그인 시작·권한 확대를 fallback으로 사용하지 않는다.
- 기존 role/session 위치는 가능한 한 유지한다. 이전 Windows home 등의 session을 관리 home에서 재개할 수 없으면 원본 session과 저장 문맥·실패를 보존한 기존 새 session 복구를 사용한다. 실제 source나 session을 비가역 이동·초기화하지 않는다.
- 인증값은 prompt·argv·로그·DB에 노출되지 않는다. Codex 직접 exec, 내장 도구 사전 확인, 보호 경로·호출 소유권·프로세스 종료 및 Reviewer 읽기 전용·자동 승인 검토를 유지한다.

## Implementation

`agents/agent_codex_sandbox.py`의 기존 private runtime·identity·인증 연결·보호 경로 helper와 G-262의 준비 경계를 재사용한다. home 선택과 인증 재사용은 sandbox probe 전에 완료하고 이후 검사·실행에 같은 환경을 전달한다. `auth.json`과 `config.toml`을 함께 연결하는 현재 loop와 Windows 개인 home 선택을 제거한다. 인증 원본 선택과 실제 소비하는 설정 source는 분리하고 Manager는 빈 config도 생성하지 않는다.

설치 CLI와 공식 인증/설정 계약에서 파일·OS 저장소의 선택, 원본 home과 keychain identity, native Windows 연결 제한을 확인한다. 지원되지 않는 방식·권한을 새로 도입하지 않으며 인증값을 추출해 임시 파일이나 argv에 저장하지 않는다. 공식 지원만으로 가능한 경로를 구현하고 불가능/미확인은 이유를 보존해 해당 호출을 거부한다. 실제로 새로운 인증·비밀 저장 방식을 요구하는 경우 Architecture Change Policy의 사용자 판단을 먼저 남긴다.

기존 개인 config 연결의 분리는 관리 경로·원본 target에 대한 증명과 분리 직전 재확인을 결합한다. 소유권은 디렉터리 이름이나 symlink 존재만으로 추정하지 않는다. 연결 분리와 호출 결과 확정은 원자 작업으로 가정하지 않고 남은 상태를 읽어 멱등 재개한다. session-unavailable 복구·isolation 증거·guidance 이력은 기존 경계를 소비하며 새 복구 엔진·Git 쓰기·비호환 migration을 추가하지 않는다. 실제 변경한 PRODUCT·ARCHITECTURE와 `docs/OPERATIONS.md`의 OS별 인증 안내를 같은 PR에서 갱신한다.

## Validation

- Worker는 `tests/test_codex_native_invocation.py`, `tests/test_agent_write_isolation.py`, `tests/test_native_platform_agents.py`, `tests/test_provider_runtime.py`의 관련 최소 unit을 사용한다. 기본/명시 home, config 부재, 개인 Hook·Plugin/MCP·rules fixture, 프로젝트·관리 충돌, consumed 설정 변경과 비밀값 비노출을 실제 환경 선택·파일·지문 판정으로 확인한다. provider/인증 조회는 mock하고 개인 Hook이나 실제 Agent를 실행하지 않는다.
- 기존 config 링크 분리의 원본 bytes·session·작업물·로그 불변, 이미 분리됨, regular file·target 변경·alias·소유권 부재·분리 직전 교체와 각 중단 지점을 확인한다. 실제 OS에서 이용 가능한 임시 fixture의 링크·private-directory 효과만 검증하고 mock Windows/macOS 결과를 실제 OS 성공으로 기록하지 않는다.
- 지원 provider/OS별 인증 방식·CLI 버전·모의/실제 근거와 미수행 범위를 구분한다. 실제 CLI·계정이 없으면 공식 계약과 fixture의 성공/실패 근거를 남기며 실계정 성공을 주장하지 않는다. Windows 인증 연결 권한 실패와 OS 저장소 identity 불일치에서 더 넓은 home·로그인·권한으로 재실행하지 않는지 확인한다.
- Manager 전체 verify는 기존 session fallback·작업물·실패 보존, 직접 exec·tool sandbox·Reviewer 읽기 전용·Fast 기본 off·사용량·중단과 독립 provider 지속을 유지한다. 기존 version 1 unittest 수집·compile과 실제 Agent guard를 유지한다. Reviewer는 원본 삭제/복제·설정 상속·소유권 미확인 분리 및 mock 성공 합성을 blocking issue로 확인한다.

## Progress

- [x] 2026-10-11 planning 기준 `6fc245469a6bd3cced81dbf64fc049130982bbc8`의 `prepare_launch`에서 POSIX config/auth 연결과 Windows source home 선택을 확인했다. 현재 native-platform fixture도 Windows 원본 home 사용을 기대한다. 기존 fallback·설정 지문·보호 경로 회귀를 읽었으며 구현·테스트·전체 verify는 실행하지 않았다.
- [ ] 인증 전용 Codex home·기존 연결 분리 및 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity high: OS별 인증 선택과 기존 runtime/session identity, 연결의 소유 증명·경쟁·중단에도 원본을 보존하는 판단이 남아 있다. 새로운 인증 저장 방식이나 권한 정책은 구현자 판단으로 추가하지 않는다.
- risk high: 잘못된 연결 분리는 개인 설정·인증 또는 session의 가용성을 해치고, 잘못된 home 선택은 개인 확장 설정을 실행에 포함할 수 있다. 원본·session·진단을 그대로 보존하고 검증한 링크만 분리하며 불확실성을 선거부한다. 원본과 연결 증거를 통한 수동 복구·기존 session fallback을 유지하고 원본 손실을 코드 rollback으로 복구한다고 가정하지 않는다. AC·Validation에서 교체 경쟁·중단·원본 보존·실패 시 우회 금지를 확인한다.
- priority high: 별도 scheduler 설정이나 개인 config 편집 없이 안전한 Codex 자동 호출을 만드는 필수 작업이다. 현재 구현된 Fast 기본 off·G-199의 scope 교정을 다시 계획하지 않는다. 개발 fixture와 실제 인증 성공은 구분하며 이번 계획 작성으로 R-085를 완료 처리하지 않는다.
