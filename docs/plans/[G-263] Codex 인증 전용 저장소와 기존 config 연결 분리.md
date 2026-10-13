---
id: G-263
title: Codex 인증 전용 저장소와 기존 config 연결 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
depends_on:
  - G-262
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

Codex runtime의 config를 전용 원본의 현재 상태와 일치시키고 개인 설정 연결의 분리·경쟁·중단 안전성을 완결한다. 구현된 선택 state_dir·기본 인증 링크·Windows 전용 홈·session 구조를 재사용하며 원본 삭제/재생성도 신규·재개·재시도·fallback 준비에 적용한다.

## Acceptance Criteria

- R-085: DB/lock이 사용하는 선택 state_dir의 automation-codex를 전용 원본으로 사용한다. 외부 CODEX_HOME의 미설정/다른 값·Windows 대소문자 차이를 인증 source 선택에 쓰지 않고 기본 사용자 auth.json만 새 인증 연결에 사용한다. 부모 환경·다른 상태 루트·기존 전용 파일/정상 링크와 허용 인증 환경값을 보존한다.
- 새 관리 홈에 Manager가 생성한 빈 config나 개인 Plugin/Hook/MCP·규칙/profile의 링크/복제본은 없다. 전용 설정은 덮어쓰지 않고 POSIX 역할별 runtime과 Windows 전용 홈을 유지한다. 명시적 Model/Effort/Fast·instruction·sandbox/승인 입력은 config 없이도 전달된다.
- 매 호출 준비에서 전용 config의 최초 부재·생성·수정·원자 교체·삭제·재생성을 현재 원본에 맞춘다. 원본이 있으면 최신 내용을 사용하고 없으면 config 없는 runtime으로 호출한다. 확인된 Manager 잔여 config 연결과 끊긴 링크만 제거하며 과거 config 복원·빈 파일 생성·개인 config 재연결·원본 부재만의 호출 거부는 없다.
- 개인 config 분리와 전용 원본 부재의 잔여 정리는 runtime 소유권·경로·링크 종류/원본 관계·현재 상태와 분리 직전 재확인에 결합한다. 원본 읽기 실패는 부재가 아니다. regular file·다른 target/alias·소유권 불명·검사 뒤 교체는 보존·진단하고 해당 호출을 거부한다. source config/auth·전용 설정·session·작업물·로그를 변경/삭제하지 않는다.
- G-262의 동일 준비 환경을 인증·기능/설정 검사·신규·재개·retry·새 session fallback에 사용한다. 개인 Hook/설정의 부재·변경은 consumed 설정 지문을 바꾸지 않고 현재 전용 config·프로젝트/관리 입력 변화는 재검증한다. 반복 준비는 정상 연결을 불필요하게 재생성하지 않는다.
- 지원 Linux/macOS·기존 native Windows의 공식 CLI 인증 선택·파일/credential store·갱신/원자 교체·CLI의 링크 교체와 session 호환을 확인한다. 파일 링크만으로 경로 의존 credential store 공유를 주장하지 않는다. 실패는 실제 전용 경로의 조치를 안내하고 개인 홈 전체 상속·인증 복제/이동·자동 로그인·권한 확대·새 저장 방식으로 우회하지 않는다.
- 기존 role/session·runtime identity와 같은 상태 루트의 DB/cron 연결은 유지한다. 과거 홈의 재개 불가는 원문/session/실패를 보존한 기존 fallback으로 처리하고 초기화/비가역 이동을 하지 않는다. 비밀값 비노출·직접 exec/내장 도구 경계·보호 경로·자식 종료·Reviewer 읽기 전용과 자동 승인 검토는 유지한다.

## Implementation

- [ ] `loop_manager_internal/agents/provider_home.py`, `agent_profiles.py`의 state_dir·기본 인증 연결과 G-262의 준비 경계를 재사용한다. 인증/설정 검사부터 실제 호출·fallback까지 같은 source를 전달하고 개인 확장 설정을 소비하지 않는 지문·사전 거부 경계를 유지한다.
- [ ] `loop_manager_internal/agents/agent_codex_sandbox.py`의 prepare_launch·_read_only_link를 대조해 config 전용 상태 정합화를 구현한다. 현재 target 부재 때 잔여 runtime 링크를 거부하는 경로를, 확인된 Manager 연결의 안전한 제거와 소유권 미확인의 보존/거부로 구분한다. auth의 부재/연결 보호를 config 정책으로 일괄 완화하지 않는다.
- [ ] config 원본의 존재/내용·연결 target과 private runtime 소유 증거를 검사하고 unlink/호출 전 교체를 다시 확인한다. 끊긴 링크도 원본 관계가 증명될 때만 링크 자체를 제거한다. 원본 config/auth·session·작업/로그는 보존하고 중단 뒤 현재 상태부터 멱등 준비한다.
- [ ] 설치 CLI/공식 계약에서 지원 OS별 인증 store·home/keychain identity·native Windows 연결과 session 복구를 확인한다. 미확인 방식은 이유를 보존해 거부하고 새로운 인증/비밀 저장 방식이 필요하면 Architecture Change Policy의 사용자 판단에 남긴다.
- [ ] `tests/test_provider_home.py`의 구현된 상태 루트·개인 config 분리·session identity·설정 지문 fixture와 `tests/test_codex_native_invocation.py`, `test_agent_write_isolation.py`, `test_native_platform_agents.py`, `test_provider_runtime.py`를 재사용한다. PRODUCT·ARCHITECTURE·`docs/OPERATIONS.md`에는 실제 config 수명·OS/인증 실패 조치만 통합하고 공유 인증 파일 갱신의 기본 인증 영향 안내를 유지한다.

## Validation

- 임시 전용 원본과 같은 runtime의 연속 모의 호출로 config 최초 부재→생성→수정/원자 교체→삭제→재생성을 확인한다. runtime 내용/연결·consumed 지문·명시 호출 입력이 현재 원본을 따르고 부재 때 config가 없으며 반복 준비가 상태를 바꾸지 않아야 한다. 신규·재개·retry·session-unavailable fallback을 연결한다.
- 기존 개인 링크, 알려진 전용 잔여/끊긴 링크는 검증한 링크만 제거한다. regular file·다른 target/alias·원본 읽기 불가·소유권 부재·검사 뒤 교체는 원문을 보존하고 호출을 거부한다. 각 중단 후 재준비와 source config/auth·session·로그·부모 환경 불변을 확인한다.
- `tests/test_provider_home.py` 및 관련 최소 native/provider/isolation unit에서 외부 home 무시·전용 config 보존·개인 Hook/Plugin 제외·프로젝트/관리 충돌·source 변경 지문·비밀값 비노출을 확인한다. 실제 사용자 config/Hook·Agent·계정은 사용하지 않고 CLI/credential-store 관측은 mock한다.
- 지원 OS/CLI별 공식 근거·실제 fixture/미수행 범위를 구분한다. 이용 가능한 실제 임시 링크/private-directory 효과만 OS 근거로 인정하고 mock Windows/macOS 또는 파일 공유를 실계정/credential-store 성공으로 기록하지 않는다. 부분/전체 verify 회귀는 session fallback·Fast 기본 off·권한/Reviewer 경계·사용량·자식 종료·독립 provider 지속과 원본 보존이다.

## Progress

원격 직접 구현의 전용 홈·기본 인증 링크·Windows 홈 선택·개인 config 분리와 관련 회귀를 정적으로 확인했다. 현재 전용 config 삭제 뒤 _read_only_link는 잔여 링크가 있으면 거부하며 최신 R-085의 config 없는 호출 조건은 남아 있다. 이 ExecPlan의 구현·테스트는 실행하지 않았다.

- [ ] 전용 config 수명 정합화·연결 분리 경쟁/중단 보완
- [ ] 지원 인증/session 근거와 targeted 검증
- [ ] 실제 동작/지원 안내 정합성

## Decision Log

- R-085의 원본 부재는 config 없는 정상 호출이다. 소유 관계가 증명된 잔여 연결은 제거하지만 regular file·다른 원본·읽기 실패를 임의 삭제하지 않는 기존 보호는 유지한다. config 수명을 auth에 일반화하지 않는다.
- 직접 구현된 state_dir·기본 인증 연결과 Windows 전용 홈을 재사용하고 원본 삭제/재생성까지 같은 ID의 잔여 config 안전성 목표로 반영한다. 새로운 home/session 재설계·인증 저장 방식은 추가하지 않는다.
- complexity high: OS별 인증/session identity와 원본 존재/연결의 소유 증명·교체 경쟁·중단을 보존하며 실제 호출 입력에 일치시킬 판단이 남아 있다. risk high: 잘못된 분리는 설정/인증/session 가용성을 잃게 하거나 개인 확장 설정을 실행시킨다. 원본·session·연결 근거를 유지한 안전 거부/기존 fallback으로 수동 복구하며 원본 수명·경쟁·중단·비밀값 및 미확인 방식 거부를 검증한다.
- priority high: 별도 cron 환경값·개인 설정 편집 없이 안전한 Codex 자동 호출을 완결하는 필수 작업이다. 모의 근거를 실계정 성공으로 기록하거나 직접 구현에 소급 PASS를 만들지 않는다.
