# Loop Manager 재구현 계획

이 문서는 이번 직접 재구현의 임시 실행 기준이다. 자동 Planner의 ExecPlan이 아니며 R/G 문서를 새로 만들지 않는다. 모든 완료 표시는 실제 코드와 검증 근거가 있어야 한다.

## 기준과 현재 상태

- 기준 develop: `086c89936433eac06511af035daada51afd77ff2` (2026-11-13 원격 fetch 확인).
- 참고 main: `668928f2cbd9069bb24f6b5e5cf1fc8baf627bd5`.
- 작업 브랜치: `codex/rewrite-loop-manager`; PR base: `develop`.
- 진행 PR: [Draft #375](https://github.com/gamjaa/loop-manager/pull/375). 전체 재구현 미완료 상태와 검증 실패를 명시했다.
- 운영 DB·원격 작업·기존 PR·main은 변경하지 않는다. 기존 DB 마이그레이션은 만들지 않는다.
- 이전 단계: S2–S7의 신규 경계와 Coordinator 프로토타입을 작성·집중 검증했다. 기존 CLI 전환, 완전한 원격 복구·runtime bootstrap, 구형 구성·문서 정리는 미완료다. 전체 재구현 완료 상태가 아니다.
- 현재 단계: 사용자 요청에 따라 S8의 구형 코드 정리를 먼저 수행하고 공개 CLI를 새 구조로 전환했다. 최신 명세 반영·원격 인수·완료/알림 통합을 이어서 구현한다.
- 상태: `TODO`, `IN_PROGRESS`, `DONE`, `BLOCKED`.
- 완료되지 않은 구현을 현재 동작으로 설명하거나 목표 문서를 구현에 맞춰 축소하지 않는다.

## 기존 구현과 목표의 차이

| 영역 | 기존 구현 | 재구현 목표 |
|---|---|---|
| 계획 | PRODUCT 포함 전체 입력 지문, base 직접 게시, 유지보수 Planner | R Blob OID 변경·최종 충족 재평가, 주 R별 단일 계획 PR, draft G |
| 저장 | 다수 복구·이력 테이블, phase와 상태의 중복, 파일 receipt | 명세의 11개 책임 테이블, G status 단일 원본, 작은 DB 근거 |
| CLI | bootstrap과 실행 잠금에 결합, legacy 제출·생략 옵션 | 짧은 WAL 직접 조회/CAS, pause/resume, G별 수동 접수·원자 결과 알림 |
| 실행 | 광범위 binding/compatibility exports, 관리 대상 한도·유지보수 | 명시적 함수 경계, 공통 AI 한도·프로젝트 Worker 1·전역 Verify 1 |
| Git | Rebase와 소유권/snapshot 증명 중심 | 분리 작업 공간, Merge, 실제 게시·삭제 경계 보호, ignored-only 허용 |
| 완료 | 장기 로그·receipt·호출 이력 보존 | PR/Archive 원격 근거, 필요한 최소 DB 기록, 비차단 후속 정리 |
| Verify | 공통 분산 엔진과 부분 검증 확대 | 프로젝트 정의 필수 명령, 후보·설정 근거, 프로젝트 내부 병렬화 |
| 운영 문서 | 폐기 R/G 참조, 구형 명령/설정 | 새 CLI·스키마·복구·템플릿과 일치 |

## 단계·의존성·완료 조건

| 단계 | 상태 | 선행 | 구현 범위 | 완료 조건과 검증 |
|---|---|---|---|---|
| S1 기준·전체 대응표 | DONE | 없음 | 최신 refs, 지침·PRODUCT·ARCHITECTURE·PLANS 조사, 영향 경계 | 기준 SHA·전체 추적표·이 계획을 코드 변경 전에 보존 |
| S2 WAL 저장소·CLI 계약 | IN_PROGRESS | S1 | 신규 DDL, 상태/CAS/요청/Outbox, 순수 조회, 설정, pause/resume | 초기화·재시작·busy·rollback·중복 요청·오래된 결과·원자 알림 unit 회귀 |
| S3 문서 관측·Planning | IN_PROGRESS | S2 | ID/metadata/DAG/draft, R OID 감지, 공유 G/주 R, 계획 PR·닫힘·최종 R 평가 | rename/archive/삭제 구분, 입력 무변경 skip, R 충돌 문맥, 사용자 계획 편집 보존 |
| S4 Git·GitHub·수동 준비 | IN_PROGRESS | S2 | 격리 공간, 원격 관측, Merge/Push/PR/외부 의도, manual start | 실제 Git 소수 대표 경로와 mock API; 게시 후 중단·기존 PR·미게시 변경 보존 |
| S5 Agent·Verify·근거 | IN_PROGRESS | S2 | Provider 지침/홈/세션/Checkpoint/토큰, 프로젝트 명령, timeout/프로세스 종료, 후보 지문 | Provider mock, 지침 1회, 누락 사용량, 후보 변경 무효화, 실패·중단 PASS 거부 |
| S6 Coordinator·재시작·Runtime | IN_PROGRESS | S3/S4/S5 | 단계 선택, 자원 겹침, CLI 재관측, generation, 새 환경 복구, 안전 인계 | Worker/Verify/Review 겹침·pause·취소·늦은 반환·runtime drain·복구·경합 |
| S7 완료·Archive·정리·알림 | IN_PROGRESS | S3/S4/S6 | 실제 Merge 완료, R 전체 재확인, 철회·tombstone, 비차단 정리, Telegram | 실패 재시도·Outbox 중복 억제·미게시 파일 보존·작은 확정 기록 |
| S8 전환·구형 구성 제거·문서 | IN_PROGRESS | 사용자 요청으로 정리 우선 | 진입부 연결, 구형 패키지/테스트/설정 삭제, README/OPERATIONS/VERIFY/PLANS/지침/템플릿 | 폐기 참조·미사용 코드 검색, CLI help·문서 링크·새 설치 절차 정합성 |
| S9 최종 검토·Verify·PR | TODO | S8 | 계약별 대조, WSL 전체+Windows 핵심, 커밋·Push·develop PR | 추적표 전부 검증, 미완료는 Draft PR+계획 보존; 전부 완료 때만 계획 삭제 |

S3/S4/S5의 책임은 독립적으로 구성하며 공유 DB 계약 변경을 먼저 확정한다. 자동 Manager/Agent를 실행해 이 작업을 위임하지 않는다. `loop_manager.py run`은 직접 실행하지 않는다.

## 모듈·데이터 변경 범위

- 교체: `loop_manager.py`의 compatibility export/legacy CLI, `application`의 binding 기반 실행 조립, `storage`의 구형 스키마·마이그레이션·중복 phase, `planning`의 직접 base 게시, `maintenance` 전부, `recovery`의 Rebase/snapshot 증명 중심 경로.
- 통합: 문서 관측/파싱/계획 감지, Git/PR 외부 효과, 실행 단계·요청 상태, 알림 사건 생성 책임을 각각 한 구현으로 모은다.
- 재사용 후보: 기존 Provider 호출 인자·사용량 해석, OS 프로세스 종료/잠금, PATH bootstrap, WSL 검증 복사·timeout·정리, 프로젝트 파일 복사/설치. 실제 의존성을 확인한 뒤 유효한 부분만 남긴다.
- 새 DB: `projects`, `pause_scopes`, `exec_plans`, `requirement_states`, `exec_plan_runs`, `agent_calls`, `validation_receipts`, `managed_prs`, `external_jobs`, `notification_outbox`, `exec_plan_tombstones`. 명세 표는 11개 테이블이며 별도 테이블을 임의 추가하지 않는다.
- `exec_plans.status`만 단계 원본이다. run에는 generation/input/manual/request/retry만 둔다.
- 모든 호출자·fixture·설정 예제·setup/copy/verify 스크립트·역할 지침·공개 CLI를 전환할 때 함께 확인한다.
- 운영 DB는 읽거나 수정하지 않는다. 테스트 DB와 임시 Git만 사용한다.

## 설계별 구현·테스트·검증 추적표

경로와 결과는 실제 구현/실행 후 채운다. `미실행`은 성공을 뜻하지 않는다.

| 기준 | 필수 계약·시나리오 | 단계 | 코드·테스트·검증 | 상태 |
|---|---|---|---|---|
| P Summary/Scope/Goals/Non-Goals | 언어 독립, 외부 스케줄러, 정책·최종 Merge 사용자 통제, 유지보수 자동 생성 제거 | S3/S5/S6/S8 | 미실행 | TODO |
| P Invariants 문서·R 우선순위 / A2 | base 확정 G만 실행, 높은 R-ID의 실제 충돌 규칙만 우선, 비충돌 조건 유지 | S3/S5 | 미실행 | TODO |
| P Invariants 실행·Git·성공·완료 | 실제 Merge dependency, 입력 근거, 사용자 작업 보존, archive로 완료 합성 금지 | S2/S4/S6/S7 | `storage/state.py`, `git/repository.py`; `test_rewrite_state`, `test_rewrite_git`, `test_rewrite_coordinator` — focused 통과, Archive 통합 미완료 | IN_PROGRESS |
| P Workflow1 / A2/A4 | R Blob OID 계획 감지, 단순 SHA/PRODUCT/ARCHITECTURE/G 변경 독립 Planner 금지 | S3 | `catalog/observer.py`; `test_rewrite_catalog` — R OID/draft/DAG/삭제 경계 통과; 공유 목표 영향 평가 미완료 | IN_PROGRESS |
| P Workflow2 / A4 | 주 R별 계획 PR 1개, 공유 G 중복 방지, draft G·질문·Draft PR | S3/S4/S5 | `planning/proposals.py`; `test_rewrite_proposals` — Draft 단일 PR·사용자 head 재관측·Close 동일 입력 억제 통과; Close→Archive 통합 미완료 | IN_PROGRESS |
| P Workflow2 / A4 | 최신 PR 사용자 변경 재관측 후 전부 불필요 판단, Close→R Archive 재개 | S3/S4/S7 | `planning/proposals.py`; `test_rewrite_proposals` — Draft 단일 PR·사용자 head 재관측·Close 동일 입력 억제 통과; Close→Archive 통합 미완료 | IN_PROGRESS |
| P Workflow2 / A4 | 사용자 Close 동일 입력 재생성 금지, R 실제 변경 후 재평가 | S3 | `planning/proposals.py`; `test_rewrite_proposals` — Draft 단일 PR·사용자 head 재관측·Close 동일 입력 억제 통과; Close→Archive 통합 미완료 | IN_PROGRESS |
| P Workflow3 / A4 | base/계획/구현 브랜치별 확정 범위, 실행중 변경 반영, 완료 G 소급변경 금지 | S3/S6 | `catalog/observer.py`; 문서 변경 generation 무효화·terminal G 보존 통과; 변경 명세의 실제 Worker 입력 반영 미완료 | IN_PROGRESS |
| P Workflow4 / A5/A7/A8 | Worker→필수 Verify→Reviewer→PR, 실패 같은 G 교정 | S5/S6 | `application/coordinator.py`, `execution/validation.py`; Worker/Verify/Review 배정 mock 회귀 통과; 공개 진입부 미연결 | IN_PROGRESS |
| P Workflow5 / A4/A5 | G별 manual start CAS→Git→확정, 중단/실패 재관측, complete 접수·자동 검증 | S2/S4/S6 | `application/direct_commands.py`, `storage/state.py`; `test_rewrite_commands` — CAS→Git→확정·실패 보존 통과; runtime 준비 경합 통합 미완료 | IN_PROGRESS |
| P Workflow6 / A9 | 실제 Merge G archive, R 최신 AC 최종 평가, 미충족 후속 계획 | S3/S7 | `application/completion.py`; `test_rewrite_completion` — 소형 정리·보존 회귀 통과; 전체 Archive 외부 효과 재개 미완료 | IN_PROGRESS |
| P 삭제·취소 / A2/A9 | rename/archive/실패를 철회로 오인 금지, 공유 목표·실행 반환·ID 재사용 방지 | S3/S6/S7 | `catalog/observer.py`, `storage/state.py`; G 삭제/tombstone·늦은 결과 거부 통과; R 철회·공유 G 목표 재평가 미완료 | IN_PROGRESS |
| P 우선순위·동시실행 / A5 | 재개 우선·동급 신규 기아 방지, 전역 AI·프로젝트 Worker1·전역 Verify1 | S6 | `application/coordinator.py`; `test_rewrite_coordinator` — Worker/Verify 겹침 통과; 재시작을 포함한 동급 신규 작업 기아 방지 미완료 | IN_PROGRESS |
| P Verify/Reviewer / A7 | 프로젝트 명령·환경·cwd·timeout, 대기시간 분리, 자식 종료, 안전 입력 열거 | S5/S6 | `execution/processes.py`, `execution/validation.py`; `test_rewrite_agents` — 언어 독립 명령·입력 변경·Windows/Linux 자식 종료 통과; 문서 OID 근거 보완 필요 | IN_PROGRESS |
| P 충돌·Draft / A8 | GitHub 충돌 캐시와 상태 관측 분리, 확인된 충돌만 Merge/Agent 교정 | S4/S6 | `git/repository.py`, `planning/proposals.py`; 충돌 head/base 캐시·실제 Merge 보존 회귀 통과; 전체 Draft/Ready/알림 통합 미완료 | IN_PROGRESS |
| P 충돌·Draft / A8 | 같은 PR Draft→검증→Ready, 같은 후보 충돌해소·재검토 알림 통합 | S4/S6/S7 | `git/repository.py`, `planning/proposals.py`; 충돌 head/base 캐시·실제 Merge 보존 회귀 통과; 전체 Draft/Ready/알림 통합 미완료 | IN_PROGRESS |
| P Git 안전 / A8 | no force/rebase/reset, ignored-only 허용, 실제 게시 target 확인, 원격 삭제 대기 | S4/S6 | `git/repository.py`; `test_rewrite_git.GitPreservationTests` WSL 통과 — 원격 사용자 Merge·게시 재확인·미게시/ignored/CRLF 보존 | IN_PROGRESS |
| P 실패·복구 / A10 | 실제 외부 효과 재관측, 오래된 generation 거부, 새 공간·원격 DB없는 복구 | S2/S4/S6 | `application/coordinator.py`; 늦은 결과·생존 프로세스 중복 금지·Worker 게시 직전 중단 재개 mock 통과; 원격 최초 인수 전체 미완료 | IN_PROGRESS |
| P 운영 CLI / A3/A5 | 단일 WAL, 빠른 SELECT, 짧은 CAS, runtime 잠금과 CLI 분리, busy 명시 실패 | S2/S6 | `storage/state.py`, `application/direct_commands.py`; WAL/busy/CAS/무효과 SELECT 통과; 공개 진입부 미연결 | IN_PROGRESS |
| P 운영 CLI / A5 | pause 최신 의도, 단계직전 요청 재관측, unblock 실제 재평가, 중복·상충 요청 | S2/S6 | `application/coordinator.py`; pause·unblock 실제 결과 대기 통과; 프로젝트 설정/등록 해제 최신 요청 처리 미완료 | IN_PROGRESS |
| P Telegram / A5/A13 | request ID·feedback 원자 결과+Outbox, 즉시 CLI 결과 중복금지, 지연 실패/거부 통지 | S2/S6/S7 | `notifications/outbox.py`; `test_rewrite_outbox`, `test_rewrite_state` — 원자 결과/부분 전송 재개/dedup 통과; CLI·PR 사건 통합 미완료 | IN_PROGRESS |
| P Runtime / A11 | 실제 실행입력 변경만, 안전 경계 drain·checkpoint·인계, PR Merge 대기 없음 | S5/S6 | `runtime/revision_runtime.py`, Coordinator boundary hook; 문서 전용 변경 무시·인계 영수증 소비·진행 Worker 반환 후 Verify 연기 통과; bootstrap/child 실연결 미완료 | IN_PROGRESS |
| A1 책임·원본 | 문서=base, 작업물=Git, PR=GitHub, 실행=SQLite; 요청은 의도 | S2–S8 | 미실행 | TODO |
| A3 테이블·보존 | 신규 스키마, 제약·인덱스, WAL 재시작, 근거 DB행, 구형 호환 제거 | S2/S7/S8 | `storage/state.py`; 11개 테이블·WAL 초기화/재시작·구형 DB 거부 통과; 구형 구현 제거 미완료 | IN_PROGRESS |
| A3 외부 의도 | 필요한 미확정 효과만 journal, 트랜잭션 밖 I/O, 완료 job 정리 | S2/S4/S7 | `git/repository.py`, `planning/proposals.py`, `application/completion.py`; Push 의도/확인 정리 통과; Archive 모든 중단 지점 검증 미완료 | IN_PROGRESS |
| A6 Agent | 공통/역할/Worker Ponytail 정확히1회, 프로젝트 AGENTS는 CLI, 지침 해시 | S5 | `agents/runner.py`; `test_rewrite_agents` — 지침/Ponytail 1회·Provider argv/캐시·환경 비밀값 필터·사용량 unknown 통과; 기존 역할 protocol 전환 미완료 | IN_PROGRESS |
| A6 Agent | Provider 전용 홈·인증·호출 분리, 세션 소실·오류 종류, checkpoint commit/push | S5/S6 | `agents/runner.py`; `test_rewrite_agents` — 지침/Ponytail 1회·Provider argv/캐시·환경 비밀값 필터·사용량 unknown 통과; 기존 역할 protocol 전환 미완료 | IN_PROGRESS |
| A9 정리 | 최종 집계/알림/AC 작은 근거 후 세션·로그·토큰·worktree 정리, 실패 비차단 | S7 | `application/completion.py`; 사용자 파일/공유 session/auth 보존·정리 실패 비차단 단위 회귀 통과; 최종 집계·게시·정리 통합 미완료 | IN_PROGRESS |
| A10 환경 인수 | 원격 G/PR 이력·취소 ID 복구, 세션·PASS·과거 토큰 합성 금지, 순차 Writer | S3/S4/S6 | `planning/proposals.py`, `git/repository.py`의 원격 관측 기반 코드 작성; 취소 ID/주 R/브랜치/PR 최초 인수 전체 회귀 미완료 | IN_PROGRESS |
| A12 사용량·PR | invocation dedup, 역할/provider/model·cache 의미·unknown, 사용자 본문 보존 | S2/S4/S5/S7 | `agents/runner.py`, `planning/proposals.py`; Provider cache 의미/unknown·사용자 PR 영역 보존 통과; 종료 시 최종 집계 확인 미완료 | IN_PROGRESS |
| A13 보안 | 최소 권한, 비밀 로그 비공개, 실제 변경/삭제 경계 검증, Outbox 실패 독립 | S4/S5/S7 | `git/repository.py`, `agents/runner.py`, `notifications/outbox.py`; 비밀 필터·Manager 영역 redaction·사용자 영역 보존·전송 재시도 통과; 운영 실실행 없음 | IN_PROGRESS |
| A14 / 사용자 검증요구 | mock/unit 위주, 실제 Git 대표 보존·중단·Merge, WSL전체+Windows최소 회귀 | S2–S9 | 신규 focused WSL/Windows 회귀 및 공식 verify 시도. 아래 검증 기록 참조. 전체 verify FAIL, Windows 공식 핵심 미실행 | IN_PROGRESS |
| 사용자 문서 정리 | README/OPERATIONS/VERIFY/PLANS/지침/템플릿 일치, 폐기 R/G·명령 제거 | S8/S9 | 미실행 | TODO |

## 기술 결정·미해결 사항

- DONE: 신규 DB만 구현하며 기존 운영 DB를 초기화하거나 자동 마이그레이션하지 않는다.
- DONE: 사용자 지정 직접 재구현이 Specification Skill의 일반 문서 전용 범위보다 우선한다. R/G를 만들지 않는다.
- DONE: 별도 루트 파일·외부 의존성은 추가하지 않는다. 임시 계획은 `docs/`에 둔다.
- DONE: 원격 관리 작업물·사용자 checkout에 대한 실제 쓰기는 테스트 대상으로 사용하지 않는다.
- DONE: 재사용 경계 확인 — frontmatter parser, process lock, Windows Job, Linux 종료 확인, Provider 사용량 parser/환경 필터, 비밀값 redactor와 공식 WSL verify를 재사용한다. 신규 코드는 기존 진입부의 compatibility export에 연결하지 않았다.
- DONE: Agent CLI 호출 형식은 [Codex 공식 exec 문서](https://learn.chatgpt.com/docs/developer-commands#codex-exec)와 [Claude 공식 CLI 문서](https://code.claude.com/docs/en/cli-reference)를 확인했다. 실제 Agent 호출 성공으로 기록하지 않는다.
- BLOCKED: 없음. 중요한 제품 정책 누락을 발견하면 근거·선택지·영향을 이 절에 남기고 해당 범위만 질문한다.

## 검증 기록

- S1: `git fetch origin develop main`, 깨끗한 detached HEAD 확인 후 최신 develop에서 전용 브랜치 생성. 코드 변경 없음.
- Windows 집중 회귀: `tests.test_rewrite_state`, `test_rewrite_catalog`, `test_rewrite_commands`, `test_rewrite_git.GitBoundaryTests`, `test_rewrite_agents` 총 35개 통과. SQLite WAL/짧은 busy/동시 claim/오래된 결과/요청·Outbox rollback, R OID·draft·DAG·실제 Merge dependency, CLI 무효과 조회, Provider mock·캐시 의미, 실제 Windows Job timeout을 확인했다.
- 초기 native 실행은 샌드박스 TEMP 접근 오류로 실패했고, 작업공간의 `.cache/rewrite-tests`를 TEMP/TMP/TMPDIR로 지정해 재검증했다. 초기화 연결의 명시적 close 누락은 수정 후 ResourceWarning 오류 모드에서도 통과했다.
- 새 코드: `storage/state.py`, `catalog/observer.py`, `application/direct_commands.py`, `git/repository.py`, `agents/runner.py`, `config/settings.py`, `execution/processes.py`, `execution/validation.py`. 기존 공개 진입부와 호출자는 그대로 유지한다.
- 공식 verify 1차: WSL success 5699 / failure 8 / error 6 / skip 8, Windows native NOT_RUN, 전체 FAIL. `.cache/verify/f9a2d995e5f4402ea14dd8f4eb24bebb/result.json` 및 `...-wsl/summary.json`. WSL 전용 `/tmp/loop-manager-verify-nf6c92di`의 stopped/removed=true 확인.
- 이번 변경의 fixture 오류: `tests.test_*` 모듈에서 helper를 import한 4곳을 `tests/support/rewrite_fixtures.py`로 옮겨 해결했다. fixture 무효과 import/교차 import 규칙 2개 회귀 통과.
- 기존 실패 재현: 변경 전 develop `086c8993`을 별도 `/tmp/loop-manager-baseline-*` 복사본에 초기화하고 `test_provider_home`, `test_provider_runtime`, `test_stable_work_reason`의 해당 case, `test_scoped_blocking`의 해당 case를 실행했다. 57 tests, failures 9 / errors 9 (unittest subtest 집계). 동일 Claude WSL managed 정책 guard와 기존 알림/대기 사유 불일치를 재현했다. `.cache/rewrite-wsl/baseline.log`. 이를 이번 변경의 성공 근거로 사용하지 않는다.
- 최종 Windows 집중 회귀: 신규 unit/mock/SQLite/Job 회귀 55개 통과. 실제 Git의 경로·CRLF·ignored·사용자 변경 보존 대표 회귀 1개도 native에서 별도로 통과(합계 56개). native Git의 첫 sandbox 실행은 MSYS `NtCreateDirectoryObject` 접근 오류로 실패해, 동일 격리 회귀를 sandbox 밖에서 재실행했다. 공식 Windows 핵심 회귀 통과로 표기하지 않는다.
- 공식 verify 2차(최종 코드): WSL success 5704 / failure 7 / error 6 / skip 8, Windows native NOT_RUN, 전체 FAIL. 신규 9개 테스트 모듈은 모두 passed=true. 남은 실패 모듈은 `test_provider_home`, `test_provider_runtime`, `test_stable_work_reason`, `test_scoped_blocking`이며 기준 develop에서도 재현했다. `.cache/verify/33a9835f1971422e8d83c37211508c39/result.json` 및 `...-wsl/summary.json`, `runner.jsonl` 참조. WSL `/tmp/loop-manager-verify-v6awwg9k` stopped/removed=true 확인.
- 최종 코드 점검: `git diff --check` 통과. 코드 기준 커밋 `0f75db62`이며 이후 변경은 이 검증 결과 기록뿐이다. 전체 목표 완료·필수 Verify 통과 상태가 아니다.
- 운영 실행: 미실행. 작업 중 직접 `loop_manager.py run` 금지.

## 남은 구현과 확인할 문제

- DONE: 공개 CLI·bootstrap·runtime child를 새 저장소/Coordinator로 전환했다. 직접 조회/상태 변경은 원격 runtime과 무관하게 DB에 접근하고, run은 별도 bootstrap/실행 잠금과 정확한 원격 SHA로 연결한다. 실제 Agent run은 실행하지 않는다.
- TODO: 원격 base 변경의 실제 최신 Requirement 전문과 OID를 Worker/Verify/Review 입력에 일관되게 반영하고 관련 없는 변경의 성공 재사용을 구분한다.
- TODO: R 철회·공유 G·의존 G의 남은 목표 재평가, 주 R 소속의 원격 복원, 닫힌 구현 PR/원격 삭제/취소 ID 최초 인수 전체 시나리오를 완성한다.
- TODO: 프로젝트 설정·등록 해제 요청의 최종 결과, 같은 G의 수동 준비 프로세스와 runtime 복구의 잠금 조율, 반복 checkpoint와 같은 우선순위 신규 G의 기아 방지를 검증한다.
- IN_PROGRESS: runtime 선택→child→drain→인계→재개를 연결했다. 부모/자식 잠금 분리, CLI 동시 쓰기, 정확한 SHA와 인계 영수증 소비 mock 회귀를 추가했다. 최종 실제 Git 후보 복사와 bootstrap 오류 경계 검증을 이어간다.
- TODO: Archive의 모든 중단 지점·최신 사용자 변경/완료 근거 대조, base 게시 변경 범위, 최종 PR 사용량 확정 후 세션/근거/실행 데이터 정리를 통합 검증한다.
- TODO: CLI 최종 결과와 PR/차단 알림의 중복 통합, 무효화된 payload 취소, 실제 장기 무진전 판정을 완성한다.
- TODO: 기존 역할 지침의 결과 protocol을 새 구조화 JSON과 일치시키고 README/OPERATIONS/VERIFY/PLANS·템플릿·설정·설치 스크립트를 전환한다. 구형 구현/스키마/테스트 제거는 이 전환 뒤 수행한다.
- TODO: 기존 전체 verify 실패를 새 구조 전환과 함께 해결하고 WSL 전체+Windows 핵심을 모두 통과시킨다. 현 상태로 전체 재구현 완료 또는 운영 투입 가능으로 표시하지 않는다.

## 다음 작업

이 파일과 Git 상태를 먼저 확인한 뒤 위 TODO를 이어간다. 우선 최신 명세 입력의 실제 전달과 원격 최초 복구·R 철회/공유 G를 완성하고 runtime bootstrap/child와 연결한다. 구형 코드 정리 우선 요청에 따라 새 진입부로 전환했으며 미완료 계약은 아래 TODO로 계속 추적한다. S8의 구형 구성·문서 정리와 S9의 필수 Verify가 끝나기 전까지 이 파일을 유지하고 Draft PR로 진행 기록을 보존한다.

## 구형 구성 정리 결과

- DONE: 구형 Manager·자동 유지보수·Rebase/scope 증명·중복 DB/마이그레이션·binding/compatibility exports와 그 전용 테스트·fixture 598개 파일(277,731줄)을 먼저 삭제했다. 경로는 Git diff로 추적하고 운영 데이터·사용자 checkout·원격 브랜치는 삭제하지 않았다.
- DONE: Provider Usage/환경 필터/비공개 홈, 프로세스 종료·WAL CLI·문서 parser와 자체 verify의 실제 호출 경계를 남겼다. 사용하지 않는 자체 부분 Verify 영향 분석·코드맵·Manager adapter·targeted wrapper도 제거했다.
- DONE: 공통/Planner/Worker/Reviewer 지침을 새 JSON 결과·Merge 중심 역할로 전환했다. 기존 planner_check 역할은 제거했다.
- DONE: 새 CLI 및 parent→child→handoff 회귀 4개와 기존 신규 Agent 회귀 7개, 총 11개가 native에서 통과했다. 삭제 후 공식 verify는 WSL 전체 118 success, Windows 핵심 91 success / 9 skip으로 두 단계 PASS. `.cache/verify/b02f037b8b344d4d8100ed916c103723/result.json`과 양쪽 summary 참조. WSL 전체+Windows 핵심 조합이며 Windows 전체 verify 통과로 표시하지 않는다.
- TODO: 삭제로 영향을 받는 운영 문서·PLANS·설정 예제를 정리하고 신규 계약의 전체 통합 검증을 완료한다.
