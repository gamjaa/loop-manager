---
id: G-306
title: Worker checkpoint의 제한 실행과 종료 검사
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-093] 원격 작업 브랜치로 새 환경에서 이어하기.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
depends_on:
  - G-307
complexity: high
risk: high
priority: high
---

## Purpose

Worker는 의미 있는 작업 단위의 checkpoint를 요청하고 Manager가 안전한 종료 후 로컬 checkpoint commit·원격 게시를 전담하도록 실제 실행·종료 계약을 연결한다. R-093의 직접 구현인 commit별 게시 함수를 재사용하며, provider의 shared Git 쓰기 금지와 Worker 종료 검사가 이 허용된 작업을 막는 부분만 보완한다. checkpoint는 구현 완료나 검증·Review 승인이 아니다.

현재 `agent_codex_sandbox.prepare_launch()`는 선택 worktree의 Git directory와 common directory를 보호한다. 한편 현재 Worker prompt는 직접 commit을 허용하지만 sandbox는 shared Git 쓰기를 보호하고, 정상 종료 시 `validate_worker_workspace_scope()`는 frontmatter 복원 callback이 있으면 HEAD 변경 자체를 거부한다. `WORKER_OUTCOME`도 완료 후보와 사용자 판단 대기만 구분한다. 이 세 경계를 정합화하며 sandbox 보호를 일반 Git 쓰기 허용으로 바꾸지 않는다.

## Acceptance Criteria

- R-093/R-023: Worker가 checkpoint 생성만 요청하고 Manager가 실제 local commit·원격 게시를 수행·확인하는 제한 실행 경로가 있다. Worker는 직접 Git add·commit·fetch·rebase·push·PR을 실행하지 않는다. 기존 Manager의 commit 실행 경계를 이용해 요청된 효과만 수행하며 source·다른 worktree·임의 ref/config·인증 저장소를 Agent 쓰기 범위에 추가하지 않는다. Worker의 fetch·rebase·push·PR 조작과 Reviewer 쓰기 금지는 유지한다.
- 구현 중 checkpoint와 `ready_for_validation`을 명시적으로 구분한다. checkpoint만 반환한 turn에 완료 PR 요약·성공한 targeted test·Worker 성공 token을 합성하지 않는다. 실제 완료 후보의 기존 결과 필드·targeted 결과 수집·문서 보고·verify·Review 조건은 유지한다.
- Worker 종료 후 동일 repository·작업·branch·worktree와 요청의 시작 HEAD·후보, 보호 문서·frontmatter 복원 및 index를 확인한 후에만 Manager가 허용된 변경을 staging·commit한다. 이미 생성된 기록된 Manager checkpoint와 과거 허용 checkpoint는 실제 parent/tree/message·intent로 검사하며 정상 HEAD 전진만으로 거부하지 않는다. 다른 branch 이동·보호 문서 변경·잘못된 최종 선언·미확정 operation은 계속 거부한다.
- 선택된 ExecPlan frontmatter의 부분 복원은 원문·본문·mode와 호출의 실제 변경 근거로 판단한다. 이미 commit된 금지 변경을 파일만 복원해 게시 가능한 이력으로 간주하지 않는다. 해당 이력은 G-307의 게시 전 검사에 남기고 로컬 작업을 보존한다. 다른 보호 문서의 파일/index 복원은 기존 담당 작업의 범위를 유지한다.
- 실제 commit 직후 다음 Worker 변경 전에 G-307의 게시 또는 보존된 대기를 확정한다. 게시가 해결되지 않았으면 새 Worker turn을 시작하지 않는다. 생성/결과 저장/게시 사이 중단에는 같은 요청·commit·session으로 재개하고 commit·모델 호출을 중복하지 않는다.
- 공통·Worker 지침, 결과 protocol, 실제 provider 구성과 PRODUCT·ARCHITECTURE가 같은 경계를 설명한다. 기존 작업의 고정 instruction snapshot은 소급 교체하지 않으며 과거 결과도 당시 계약으로 보존한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/execution/worker_outcome.py`의 엄격한 결과 파싱과 `execution_worker_handler.py:WorkerHandler.run()`에 완료와 구별되는 checkpoint 요청/반환을 연결한다. 기존 `application.result_normalization`과 session·targeted 결과 수집을 재사용한다. 요청은 선택 작업·시작 HEAD·확인할 후보·commit 제목에 결합하고 임의 shell 명령이나 외부 경로를 실행하는 범용 채널로 만들지 않는다.
- [ ] 확인한 `execution/execution_verify_handler.py`의 `_commit_owned()` 사용 경계와 기존 소유 commit/checkpoint 기록을 재사용한다. Worker 종료·보호 문서 복원 뒤 허용 파일·index·선언을 확인하고 Manager가 허용 경로만 staging·commit해 즉시 기존 `publish_worker_checkpoints`로 넘긴다. 다른 staged/unstaged 자료가 전체 `git add -A`로 섞이지 않게 한다. 성공 결과 저장 전 중단은 실제 parent/tree/message·기존 intent로 확인한다. 새 상주 서비스·provider Plugin/Hook·권한 확대를 도입하지 않는다.
- [ ] 확인한 `agents/scope_adapter.py:validate_worker_workspace_scope()`의 무조건적인 `after.head != before.head` 거부를 Worker 직접 Git 쓰기 금지, 기록된 Manager commit·원격 통합 결과 및 frontmatter 복원 가능 여부로 분리한다. `scope_validation.py`·`execution/plan_validation.py`의 역할별 최종 결과·선언 검사를 유지하고 Manager가 기록한 commit과 미확인 HEAD 이동을 구별한다. 과거 Git snapshot 접근 위반 검사를 되살리지 않는다.
- [ ] 확인한 `agents/agent_prompts.py`·`docs/AGENTS.worker.md`·`docs/AGENTS.common.md`에 새 checkpoint 결과와 기존 Git 책임을 같은 의미로 반영한다. 정적 규칙은 고정 instruction에 두고 사용자 prompt에는 요청·게시 결과 등 새 작업 문맥만 남긴다. 최소 prompt 전반의 축약과 전달 cursor는 기존 담당 작업에 맡긴다.
- [ ] 확인한 `agents/agent_codex_sandbox.py`·`agent_write_isolation.py`의 Git/인증 보호와 실제 호출의 자동 승인 검토를 유지한다. 제한 실행을 기존 Manager 효과로 수행해 내장 sandbox를 완화하지 않는지 확인한다. Claude의 일반 지원 구성은 G-191을 중복 구현하지 않고 기존 미지원 거부·문서/mock 검증 경계를 따른다. PRODUCT·ARCHITECTURE의 Worker 권한·checkpoint 절은 실제 구현과 함께 통합한다.

## Validation

- Worker는 확인한 `tests/test_worker_outcome.py`·`tests/test_worker_result_contract.py`·`tests/test_execution_worker_phase.py`·`tests/test_scope_services.py`의 변경 관련 최소 unit case를 실행한다. checkpoint 요청과 완료/판단 대기 충돌, 중복·손상·다른 작업 identity, legacy 완료 결과를 검사하며 기존 실제 판단·SQLite·파일 fixture를 재사용한다.
- 정상 checkpoint 요청 → Worker 종료 → 보호 문서·index/허용 범위 확인 → Manager 로컬 commit → 게시 → 같은 session 재개를 검사한다. Worker의 직접 add/commit/push 실행은 0건이어야 한다. 게시 대기·종료 불명에서는 다음 Worker 호출 수가 0이고, checkpoint만으로 Worker/verify/Review 성공 근거가 생기지 않아야 한다.
- 허용 commit의 HEAD 변화는 수용하고, branch 변경·보호 문서·frontmatter 오류·실제 범위 밖 변경은 계속 거부한다. commit 전/후·결과 저장 전·게시 전 중단에서 기존 commit·작업물·실패·session이 보존되고 생성이 반복되지 않아야 한다. 이미 commit된 보호 문서를 파일만 복원한 경우도 G-307의 거부를 유지한다.
- `tests/test_agent_write_isolation.py`와 기존 provider mock에서 source·다른 worktree·shared Git·인증 저장소 보호와 Reviewer 읽기 전용이 유지되는지 확인한다. 실제 모델·계정 호출은 실행하지 않는다. mock으로 증명할 수 없는 commit/index/ref 효과는 기존 격리 Git fixture의 최소 Manager 통합 회귀로 확인하고 Worker targeted 실행과 구분한다.

## Progress

- [x] 5903c5aa의 R-093 직접 구현, 실제 sandbox·Worker 종료 검사·결과 protocol과 관련 테스트를 정적으로 대조했다. 구현·테스트 실행은 하지 않았다.
- [ ] 제한 checkpoint 요청·로컬 실행·종료 검사 연결
- [ ] 게시 대기·중단 재개와 최소 회귀 확인

## Decision Log

- 2026-10-22 정책 변경: 제한 실행은 Worker의 Git metadata 쓰기 허용이 아니라 **Worker가 요청한 Manager 전담 commit 효과**다. 신규·중간·최종 checkpoint에 동일한 종료·범위·index 검사를 적용한다. 과거 Worker 직접 commit의 호환 검사는 G-307에 남기고 당시 Progress는 보존한다.

- R-093은 로컬 checkpoint를 허용하지만 임의 shared Git 쓰기를 허용하지 않는다. 기존 Manager 소유 commit 효과에 좁은 Worker 요청을 연결해 두 조건을 함께 지킨다. `writable_roots`에 common Git directory를 추가하거나 sandbox 밖 재실행을 승인하는 방식은 이 계획의 구현이 아니다.
- commit별 원격 게시·lease·미완료 intent는 직접 구현을 재사용하고 G-307이 보완한다. 그 작업을 먼저 Merge해 제한 실행 경로가 게시 안전 조건을 우회하지 않게 한다. 선택 frontmatter/보호 문서 복원 전체와 최소 prompt 기능은 재구현하지 않는다.
- complexity high는 결과 protocol·실제 writer 종료·로컬 효과·session 재개를 하나의 제한된 요청 수명으로 연결하는 판단 때문이다. risk high는 잘못된 요청 실행·HEAD 수용이 소유권 밖 commit이나 미검증 결과의 진행으로 이어질 수 있기 때문이다. 요청/후보 identity, 종료 확인, 범위 거부와 실제 commit의 멱등 복구로 완화한다. 최초 level은 5이며 Model·Effort는 Manager 설정을 따른다.
