# Loop Manager 재구현 계획

이 문서는 이번 직접 재구현의 임시 실행 기준이다. 자동 Planner의 ExecPlan이 아니며 R/G 문서를 새로 만들지 않는다. 모든 완료 표시는 실제 코드와 검증 근거가 있어야 한다.

## 기준과 현재 상태

- 기준 develop: `086c89936433eac06511af035daada51afd77ff2` (2026-11-13 원격 fetch 확인).
- 참고 main: `668928f2cbd9069bb24f6b5e5cf1fc8baf627bd5`.
- 작업 브랜치: `codex/rewrite-loop-manager`; PR base: `develop`.
- 운영 DB·원격 작업·기존 PR·main은 변경하지 않는다. 기존 DB 마이그레이션은 만들지 않는다.
- 현재 단계: S1 조사·설계 추적 완료, S2 신규 저장소 구현 준비.
- 상태: `TODO`, `IN_PROGRESS`, `DONE`, `BLOCKED`.
- 완료되지 않은 구현을 현재 동작으로 설명하거나 목표 문서를 구현에 맞춰 축소하지 않는다.

## 기존 구현과 목표의 차이

| 영역 | 기존 구현 | 재구현 목표 |
|---|---|---|
| 계획 | PRODUCT 포함 전체 입력 지문, base 직접 게시, 유지보수 Planner | R Blob OID 변경·최종 충족 재평가, 주 R별 단일 계획 PR, draft G |
| 저장 | 다수 복구·이력 테이블, phase와 상태의 중복, 파일 receipt | 명세의 12개 책임 테이블, G status 단일 원본, 작은 DB 근거 |
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
| S3 문서 관측·Planning | TODO | S2 | ID/metadata/DAG/draft, R OID 감지, 공유 G/주 R, 계획 PR·닫힘·최종 R 평가 | rename/archive/삭제 구분, 입력 무변경 skip, R 충돌 문맥, 사용자 계획 편집 보존 |
| S4 Git·GitHub·수동 준비 | TODO | S2 | 격리 공간, 원격 관측, Merge/Push/PR/외부 의도, manual start | 실제 Git 소수 대표 경로와 mock API; 게시 후 중단·기존 PR·미게시 변경 보존 |
| S5 Agent·Verify·근거 | TODO | S2 | Provider 지침/홈/세션/Checkpoint/토큰, 프로젝트 명령, timeout/프로세스 종료, 후보 지문 | Provider mock, 지침 1회, 누락 사용량, 후보 변경 무효화, 실패·중단 PASS 거부 |
| S6 Coordinator·재시작·Runtime | TODO | S3/S4/S5 | 단계 선택, 자원 겹침, CLI 재관측, generation, 새 환경 복구, 안전 인계 | Worker/Verify/Review 겹침·pause·취소·늦은 반환·runtime drain·복구·경합 |
| S7 완료·Archive·정리·알림 | TODO | S3/S4/S6 | 실제 Merge 완료, R 전체 재확인, 철회·tombstone, 비차단 정리, Telegram | 실패 재시도·Outbox 중복 억제·미게시 파일 보존·작은 확정 기록 |
| S8 전환·구형 구성 제거·문서 | TODO | S2–S7 | 진입부 연결, 구형 패키지/테스트/설정 삭제, README/OPERATIONS/VERIFY/PLANS/지침/템플릿 | 폐기 참조·미사용 코드 검색, CLI help·문서 링크·새 설치 절차 정합성 |
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
| P Invariants 실행·Git·성공·완료 | 실제 Merge dependency, 입력 근거, 사용자 작업 보존, archive로 완료 합성 금지 | S2/S4/S6/S7 | 미실행 | TODO |
| P Workflow1 / A2/A4 | R Blob OID 계획 감지, 단순 SHA/PRODUCT/ARCHITECTURE/G 변경 독립 Planner 금지 | S3 | 미실행 | TODO |
| P Workflow2 / A4 | 주 R별 계획 PR 1개, 공유 G 중복 방지, draft G·질문·Draft PR | S3/S4/S5 | 미실행 | TODO |
| P Workflow2 / A4 | 최신 PR 사용자 변경 재관측 후 전부 불필요 판단, Close→R Archive 재개 | S3/S4/S7 | 미실행 | TODO |
| P Workflow2 / A4 | 사용자 Close 동일 입력 재생성 금지, R 실제 변경 후 재평가 | S3 | 미실행 | TODO |
| P Workflow3 / A4 | base/계획/구현 브랜치별 확정 범위, 실행중 변경 반영, 완료 G 소급변경 금지 | S3/S6 | 미실행 | TODO |
| P Workflow4 / A5/A7/A8 | Worker→필수 Verify→Reviewer→PR, 실패 같은 G 교정 | S5/S6 | 미실행 | TODO |
| P Workflow5 / A4/A5 | G별 manual start CAS→Git→확정, 중단/실패 재관측, complete 접수·자동 검증 | S2/S4/S6 | 미실행 | TODO |
| P Workflow6 / A9 | 실제 Merge G archive, R 최신 AC 최종 평가, 미충족 후속 계획 | S3/S7 | 미실행 | TODO |
| P 삭제·취소 / A2/A9 | rename/archive/실패를 철회로 오인 금지, 공유 목표·실행 반환·ID 재사용 방지 | S3/S6/S7 | 미실행 | TODO |
| P 우선순위·동시실행 / A5 | 재개 우선·동급 신규 기아 방지, 전역 AI·프로젝트 Worker1·전역 Verify1 | S6 | 미실행 | TODO |
| P Verify/Reviewer / A7 | 프로젝트 명령·환경·cwd·timeout, 대기시간 분리, 자식 종료, 안전 입력 열거 | S5/S6 | 미실행 | TODO |
| P 충돌·Draft / A8 | GitHub 충돌 캐시와 상태 관측 분리, 확인된 충돌만 Merge/Agent 교정 | S4/S6 | 미실행 | TODO |
| P 충돌·Draft / A8 | 같은 PR Draft→검증→Ready, 같은 후보 충돌해소·재검토 알림 통합 | S4/S6/S7 | 미실행 | TODO |
| P Git 안전 / A8 | no force/rebase/reset, ignored-only 허용, 실제 게시 target 확인, 원격 삭제 대기 | S4/S6 | 미실행 | TODO |
| P 실패·복구 / A10 | 실제 외부 효과 재관측, 오래된 generation 거부, 새 공간·원격 DB없는 복구 | S2/S4/S6 | 미실행 | TODO |
| P 운영 CLI / A3/A5 | 단일 WAL, 빠른 SELECT, 짧은 CAS, runtime 잠금과 CLI 분리, busy 명시 실패 | S2/S6 | 미실행 | TODO |
| P 운영 CLI / A5 | pause 최신 의도, 단계직전 요청 재관측, unblock 실제 재평가, 중복·상충 요청 | S2/S6 | 미실행 | TODO |
| P Telegram / A5/A13 | request ID·feedback 원자 결과+Outbox, 즉시 CLI 결과 중복금지, 지연 실패/거부 통지 | S2/S6/S7 | 미실행 | TODO |
| P Runtime / A11 | 실제 실행입력 변경만, 안전 경계 drain·checkpoint·인계, PR Merge 대기 없음 | S5/S6 | 미실행 | TODO |
| A1 책임·원본 | 문서=base, 작업물=Git, PR=GitHub, 실행=SQLite; 요청은 의도 | S2–S8 | 미실행 | TODO |
| A3 테이블·보존 | 신규 스키마, 제약·인덱스, WAL 재시작, 근거 DB행, 구형 호환 제거 | S2/S7/S8 | 미실행 | TODO |
| A3 외부 의도 | 필요한 미확정 효과만 journal, 트랜잭션 밖 I/O, 완료 job 정리 | S2/S4/S7 | 미실행 | TODO |
| A6 Agent | 공통/역할/Worker Ponytail 정확히1회, 프로젝트 AGENTS는 CLI, 지침 해시 | S5 | 미실행 | TODO |
| A6 Agent | Provider 전용 홈·인증·호출 분리, 세션 소실·오류 종류, checkpoint commit/push | S5/S6 | 미실행 | TODO |
| A9 정리 | 최종 집계/알림/AC 작은 근거 후 세션·로그·토큰·worktree 정리, 실패 비차단 | S7 | 미실행 | TODO |
| A10 환경 인수 | 원격 G/PR 이력·취소 ID 복구, 세션·PASS·과거 토큰 합성 금지, 순차 Writer | S3/S4/S6 | 미실행 | TODO |
| A12 사용량·PR | invocation dedup, 역할/provider/model·cache 의미·unknown, 사용자 본문 보존 | S2/S4/S5/S7 | 미실행 | TODO |
| A13 보안 | 최소 권한, 비밀 로그 비공개, 실제 변경/삭제 경계 검증, Outbox 실패 독립 | S4/S5/S7 | 미실행 | TODO |
| A14 / 사용자 검증요구 | mock/unit 위주, 실제 Git 대표 보존·중단·Merge, WSL전체+Windows최소 회귀 | S2–S9 | 미실행 | TODO |
| 사용자 문서 정리 | README/OPERATIONS/VERIFY/PLANS/지침/템플릿 일치, 폐기 R/G·명령 제거 | S8/S9 | 미실행 | TODO |

## 기술 결정·미해결 사항

- DONE: 신규 DB만 구현하며 기존 운영 DB를 초기화하거나 자동 마이그레이션하지 않는다.
- DONE: 사용자 지정 직접 재구현이 Specification Skill의 일반 문서 전용 범위보다 우선한다. R/G를 만들지 않는다.
- DONE: 별도 루트 파일·외부 의존성은 추가하지 않는다. 임시 계획은 `docs/`에 둔다.
- DONE: 원격 관리 작업물·사용자 checkout에 대한 실제 쓰기는 테스트 대상으로 사용하지 않는다.
- TODO: Provider/프로세스 유틸리티의 구형 의존성과 안전한 재사용 범위 조사.
- BLOCKED: 없음. 중요한 제품 정책 누락을 발견하면 근거·선택지·영향을 이 절에 남기고 해당 범위만 질문한다.

## 검증 기록

- S1: `git fetch origin develop main`, 깨끗한 detached HEAD 확인 후 최신 develop에서 전용 브랜치 생성. 코드 변경 없음.
- 전체 Verify·운영 실행: 미실행. 작업 중 직접 `loop_manager.py run` 금지.

## 다음 작업

S2의 새 DDL과 짧은 상태 변경/비동기 요청 계약을 구현하고, 격리 DB에서 초기화·WAL·재시작·CAS·경합·알림 원자성을 검증한다. S3–S7 전체 계약 구현 전에는 새 진입부로 전환하지 않는다.
