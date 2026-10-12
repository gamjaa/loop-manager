---
id: G-262
title: provider 실행 환경과 설정 관측의 단일 전달
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: high
priority: high
---

## Purpose

인증·profile 검사와 실제 호출이 서로 다른 provider home을 사용하는 경계를 정리한다. 준비한 실행 환경과 실제 설정 source를 명시적으로 전달하고, 인증 전용 저장소의 provider별 구현은 G-263/G-264에 남긴다. 이 공통 연결만으로 R-085 전체 충족을 선언하지 않는다.

## Acceptance Criteria

- 관리 호출은 설정·권한 검사와 해당 환경을 사용하는 CLI 사전 조회 전에 실행 환경을 준비한다. 인증 확인, Model/Effort 조회, instruction 기능 확인, isolation 설정 지문·호출 직전 확인과 실제 실행에 동일한 선택 저장소·설정 범위를 전달한다. 등록·scheduler·수동 및 복구 진입에서도 실제 사용할 환경의 준비를 누락하거나 개인 home에서의 인증 성공을 관리 환경의 성공으로 대용하지 않는다.
- 역할·repository·canonical worktree와 기존 session의 연결을 유지한다. 신규·재개·제한 재시도·session-unavailable fallback은 같은 선택 규칙을 사용하고, 준비 실패·소유권 불확실·정책 확인 실패는 실제 호출 전에 거부한다. 실패를 더 넓은 환경이나 다른 provider로 우회하지 않는다.
- 설정 관측은 자식이 실제 소비하는 source와 Manager의 명시적 실행 입력을 사용한다. 인증 원본 home을 개인 설정의 추가 fallback으로 조회하지 않는다. 개인 설정을 소비하지 않는 fixture에서 그 설정만 수정해도 유효 설정 지문은 같고, 소비하는 프로젝트·관리 설정 또는 Manager 실행 입력 변경은 필요한 재확인에 반영된다.
- 이전 환경의 증거나 입력이 불완전한 경우 과거 격리·설정 동일성을 합성하지 않는다. 기존 재검증과 최소 문맥의 새 session 복구를 사용하며 작업물·원래 session·고정 지침·실패·사용량·retry 이력을 보존한다. 환경 전환만으로 Worker level이나 모든 독립 차단을 초기화하지 않는다.
- R-086의 기존 pre_review_identity 관측 port와 G-183의 최종 승인 비교에 같은 비밀값 없는 실제 Reviewer instruction/provider/model/effort·환경/정책 identity를 제공한다. invocation_id가 주어지면 그 실제 호출의 준비/실행 증거를 읽고 현재 설정으로 과거 identity를 합성하지 않는다. 현재 사전 입력·실제 호출·사후 입력이 같다는 근거가 없으면 재사용 불가를 반환해 기존 fallback을 유지한다. 부분 runner·선행 상태 전이·최종 승인 저장을 다시 구현하지 않는다.
- 전달 과정에서 부모 환경·호출자 mapping을 변경하지 않고 인증값을 prompt·argv·로그·DB에 기록하지 않는다. 기존 비밀값 필터, Fast 기본 off, bytecode 정책, 역할별 쓰기 제한·자동 승인 검토 및 Reviewer 읽기 전용 계약을 유지한다.

## Implementation

`agents/agent_profiles.py`의 환경 필터·인증/profile 조회, `agent_invocation.py`의 요청, `invocation_service.py`·`provider_adapters.py`의 호출과 `agent_write_isolation.py`의 설정 관측·재확인 경계를 연결한다. 기존 provider launch와 지침 기능 확인에 준비된 환경을 전달하고, 검사 함수가 전역 `os.environ`을 다시 읽어 다른 home을 선택하는 경로를 정리한다. 기존 named effects·요청 구조를 재사용하며 범용 환경 framework나 새로운 설정 파일을 추가하지 않는다.

현재 `agent_codex_sandbox.py`의 repository/worktree/role identity와 등록·실행의 상태 경계를 대조해 사전 검사에 필요한 선택 문맥을 전달한다. 환경 준비는 경로·소유권 확인을 먼저 수행하고 실제 인증·model 조회보다 앞에 둔다. G-263/G-264가 인증 재사용·설정 제외·기존 연결 분리를 담당하며, 이 작업은 현재 provider 구현에도 적용할 수 있는 전달·관측 계약을 독립적으로 검증한다. Claude의 현행 안전 거부나 G-191의 지원 정책을 공통 연결로 해제하지 않는다.

기존 invocation/isolation 저장과 session fallback·고정 guidance를 재사용한다. 설정 지문은 실제 소비 범위의 identity를 보존하고 비밀 원문이나 인증값의 별도 cache를 만들지 않는다. 환경 변화가 기존 증거에 영향을 주면 기존 무효화·재검증을 사용한다. 현재 호출 준비와 invocation-bound 관측을 공통 API로 전달해 R-086의 이미 마련된 port에 연결한다. G-263/G-264의 source 선택과 동일한 관측을 소비하며 provider 지원·정책 거부를 이 연결만으로 해제하지 않는다. PRODUCT·ARCHITECTURE에는 실제 구현한 전달·관측 계약을 반영하고 provider별 미구현 목표는 구분한다.

## Validation

- Worker는 `tests/test_provider_runtime.py`, `tests/test_agent_invocation.py`, `tests/test_agent_write_isolation.py`와 기존 guidance fixture의 변경 경계만 최소 targeted unit으로 실행한다. 사전 조회부터 실제 adapter 실행까지 env/source를 포착하고, 부모 환경과 다른 prepared home을 주입해 전역 home 재조회·mapping 변경·비밀값 노출을 탐지한다. CLI·계정은 test double로 대체하며 실제 선택·지문·저장 판정은 실행한다.
- 신규·재개·retry·fallback, 준비/인증/정책 실패, 개인 설정만 변경·실제 프로젝트/관리 설정 변경, 예전 증거의 입력 부재를 대조한다. 기존 최소 문맥 fallback fixture에서 원래 session·작업 파일·실패 기록과 역할 instruction이 보존되는지 확인한다. 모의 성공을 실제 인증·실계정 실행 성공으로 기록하지 않는다.
- Manager 전체 verify는 등록·scheduler·수동·복구의 같은 환경 전달, provider 오류의 독립 범위, process handoff·timeout·사용량·guidance와 기존 범위 검사를 유지한다. 선택 adapter는 `scripts/verify.json`의 version 1 unittest/launcher 환경이며 수집·필수 compile·guard를 변경하지 않는다. Reviewer는 사전 검사와 실제 실행의 환경 불일치 및 과거 성공 합성을 blocking issue로 확인한다.

## Progress

- [x] R-086 기존 관측 port를 공통 API에 연결했다. 실제 adapter·invocation 저장을 거친 동일 입력 사이클과 production 조립의 repo/plan/worktree·호출 ID 전달을 검증했다. Agent/CLI는 test double이며 실계정 실행 성공을 뜻하지 않는다.

- [x] 2026-10-11 delta `214c978cb296a765020d88378f5526050d241f44` → `6fc245469a6bd3cced81dbf64fc049130982bbc8`에서 R-085와 인증/profile·invocation·launch·설정 지문·fallback 원문 및 관련 fixture를 정적으로 대조했다. 인증/profile 조회와 설정 지문은 부모 환경을 사용하고 Codex의 별도 home은 이후 launch에서 선택된다. 구현·테스트·전체 verify는 실행하지 않았다.
- [x] 2026-10-12 사용자 직접 구현: 준비된 Codex 환경 전달, 실제 환경의 인증/profile·격리·지침 재확인, invocation 입력 저장 및 현재/실제 호출 관측 API를 구현했다. CLI/process는 test double이며 실제 모델·실계정 실행을 완료로 기록하지 않는다. 관련 targeted unit 117개와 native 호출·runtime 격리·중단 복구 25개가 통과했다.
- [x] 2026-10-12 기반 구현 b75568b에서 Manager 전체 verify: 215개 모듈·3,579개 테스트(성공 3,574·skip 5), 필수 compile 포함 통과. Linux/Python 3.12이며 실계정 모델 호출은 검증하지 않았다.
- [x] 2026-10-12 통합 구현 9dbdd1f의 전체 verify: 218개 모듈·3,622개 테스트(성공 3,617·skip 5), 필수 compile 포함 통과. production 관측·단일 선행 Reviewer 승인 사이클 및 targeted 실패 무통지 회귀를 포함하며 실제 모델·성능 실측 완료를 뜻하지 않는다.
- [ ] 읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-12 사용자 후속 구현: G-262 기반 위에 R-086 production 관측 port를 연결했다. 선행 승인 뒤 실제 전체 verify와 현재/실제 Reviewer 입력 비교를 수행하고 최종 게시 gate에서도 같은 관측을 사용한다. 관측 부재·실패의 fallback은 유지하며 G-263/G-264 완료를 선행 승인 증거로 가정하지 않는다.

- 2026-10-12 사용자 요청으로 G-262 공통 기반을 현재 main에서 별도 구현했다. 기존 dependency·ID·metadata·수행 기록을 보존하며 자동 계획 실행의 선행 gate를 해제하지 않는다. 현재 승인 비교 소비자는 후속 부분 verify PR에서 관측 API에 연결한다. G-263/G-264의 인증·개인 설정 source 변경이나 Claude 안전 거부 해제는 포함하지 않는다.
- 2026-10-12 사용자 요청의 문서 정합화: R-086의 준비된 환경 관측 소비자를 연결 대상으로 명시했다. 초기 1차 구현에서는 port 부재 때문에 부분 경로 사용 시에도 기존 경로로 fallback하는 상태를 미완료로 유지했다. 같은 관측을 별도 계획/캐시로 중복하지 않고 인증 전용 source 구현은 G-263/G-264에 남긴다. ID·기존 dependency·metadata·과거 수행 기록을 보존한다.

- complexity medium: 기존 요청·named effects·검사/실행 경계에 동일한 문맥을 전달하는 책임이 명확하다. provider별 인증 방식·저장소 전환은 후속 작업으로 분리한다.
- risk high: 다른 환경에서 확인한 정책·인증을 실제 호출의 근거로 채택하면 개인 확장 설정이나 오래된 증거로 실행할 수 있다. 준비 실패의 선거부, 원래 증거·session 보존과 현재 환경 재검증이 복구 수단이며 env 불일치·설정 변경·비밀값 비노출을 AC·Validation에서 확인한다. 무인 실행에 필요한 분리 기반으로 priority high다.
- 인증 원본과 비밀값 저장 방식은 유지하고 실행 환경의 명시적 전달만 정리한다. 새 인증 방식·비밀 복제·권한 확대가 필요하면 이 범위를 구현하지 않고 Architecture Change Policy에 따른 구체적 사용자 판단을 남긴다.
- 비종료 catalog의 G-191은 Claude 쓰기 제한·자동 승인 검토, G-183은 최종 Review 재사용, G-189는 전체 차단 감사다. 이 전달 기반을 중복 구현으로 확장하지 않으며 이번 결과는 delta 검토이고 프로젝트 최종 전문 확인이 아니다.
