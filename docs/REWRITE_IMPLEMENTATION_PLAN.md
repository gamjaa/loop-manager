# Loop Manager 재구현 계획

이번 직접 재구현의 임시 실행 기준이다. R/G 문서를 만들지 않으며 모든 완료 표시는 실제 구현·검증 근거와 대조한다. 전체 검토가 끝나기 전에는 유지하고 모든 완료 조건을 충족하면 삭제한다.

## 기준과 현재 단계

- 기준 develop: `086c89936433eac06511af035daada51afd77ff2`, 참고 main: `668928f2cbd9069bb24f6b5e5cf1fc8baf627bd5`.
- 작업 브랜치: `codex/rewrite-loop-manager`. [Draft PR #375](https://github.com/gamjaa/loop-manager/pull/375), base develop. main 수정·자동 Merge 없음.
- 사용자 추가 지시대로 구형 코드를 먼저 삭제한 뒤 나머지 구현을 진행 중이다.
- 구형 실행·호환·복구·유지보수·마이그레이션과 전용 테스트 약 624개 파일/28만 줄을 제거하고 공개 CLI를 새 구조로 연결했다. 자체 WSL/Windows 검증 도구·OS 종료/잠금·Provider 사용량 parser·문서 parser/PATH 준비를 필요한 범위에서 재사용했다.
- 체크포인트 `a58baf8e` 이후 최신 입력·공유 G·수동 작업·원격 인수·Archive·알림·문서 경계를 보완 중이다. 현재 변경은 추가 검증과 단계별 커밋이 필요하다.
- 실제 운영 DB·기존 원격 작업·PR은 검증 대상으로 쓰지 않는다. 작업 중 `loop_manager.py run` 직접 실행 금지. 실계정 Agent·Telegram 발송은 미실행이다.

## 기존 구현과 변경 범위

| 영역 | 교체·삭제 | 현재 방향 |
|---|---|---|
| 계획 | 전체 입력 SHA trigger, 유지보수 Planner, 직접 base 게시 | R Blob OID·실제 계획 판단, 주 R별 단일 계획 PR, 공유 G 재사용 |
| 상태 | 구형 스키마·마이그레이션·중복 phase·상세 감사 자료 | 11개 책임 테이블, WAL, G status 단일 원본, 작은 결과 |
| 실행 | binding/compatibility exports, 관리 대상 상한 | 공통 AI 한도·프로젝트 Worker 1·전역 Verify 1, 독립 단계 겹침 |
| Git | Rebase·Force/Reset 복구·과거 소유권 증명 | 격리 clone, Merge, 실제 쓰기/삭제 대상 보호, 미게시 파일 보존 |
| CLI | runtime 잠금 결합·legacy submit/recover/생략 옵션 | 직접 DB 조회/CAS, pause/resume, 수동 예약→Git→실제 결과 |
| 완료 | 장기 세션/로그/토큰/receipt 보존 | 최종 PR 집계·Archive·R 평가 근거를 먼저 확정하고 후속 정리 |
| Verify | 등록 프로젝트에 공통 분산·부분 검증 강제 | 프로젝트 명령·cwd·환경·필수 입력, 자체 저장소 검증 도구 분리 |
| 문서 | 폐기 명령·옵션·R/G 참조·유지보수 자료 | 새 README/OPERATIONS/VERIFY/PLANS·역할 지침·설정·복사 스크립트 |

## 단계와 선행 관계

| 단계 | 상태 | 선행 | 완료 조건 |
|---|---|---|---|
| S1 기준·전체 대응표 | DONE | 없음 | 최신 refs·문서·지침을 조사하고 코드 전에 계획 커밋 |
| S2 WAL·CLI | IN_PROGRESS | S1 | WAL/rollback/busy/CAS/요청·Outbox/조회, 신규 환경 수동 G 관측 검증 |
| S3 문서·Planning | IN_PROGRESS | S2 | R OID·rename/철회·공유 G·소속·단일 Draft/Ready PR·닫힘/후속 계획 검증 |
| S4 Git·PR·수동 준비 | IN_PROGRESS | S2 | Merge·게시 후 중단·원격 삭제·미게시/ignored 파일·수동 CAS/Git 경계 검증 |
| S5 Agent·Verify | IN_PROGRESS | S2 | 역할/Provider/전용 홈/세션/usage·필수 명령·종료·후보 지문, 연결 흐름 검증 |
| S6 Coordinator·runtime | IN_PROGRESS | S3–S5 | 한도/겹침/기아 방지/재시작/설정·프로젝트 요청/순차 인수·인계 검증 |
| S7 완료·정리·알림 | IN_PROGRESS | S3/S4/S6 | 실제 Merge·Archive 재개·최종 PR 집계·작은 DB 결과·세션/공간·Outbox 검증 |
| S8 구형 구성·문서 정리 | IN_PROGRESS | 사용자 요청으로 선행 정리 | 폐기 코드/테스트/문서/옵션 제거, 새 설치·실행·복구 안내 정합성 |
| S9 최종 검토·Verify·PR | TODO | S2–S8 | 아래 계약별 최종 대조, WSL 전체+Windows 핵심, 커밋·Push·PR 갱신 |

## 계약별 추적표

테스트 경로는 `tests/test_rewrite_*.py`이고 표에는 모듈 접미사를 쓴다. 현재 추가 수정 뒤 최종 실행 전인 항목은 완료로 표시하지 않는다.

| PRODUCT / ARCHITECTURE | 코드 | 검증 근거·남은 확인 | 상태 |
|---|---|---|---|
| Summary/Scope/Non-Goals, A1 책임 | entrypoint/coordinator, validation, 11개 DDL | 언어 독립 명령·자동 유지보수 제거·사용자 Merge; 최종 잔존 검색 | IN_PROGRESS |
| Invariants 문서·충돌 규칙, Workflow1, A2/A4 | catalog/observer, planning/proposals, 역할 지침 | catalog R OID·SHA-only skip·DAG·draft·rename·PRODUCT 실행 무효화; 모든 활성 R을 Agent에 전달 | IN_PROGRESS |
| Workflow2 단일 주 R·공유 G·완전 계획, A4 | planning/proposals | proposals Draft·재사용/no-diff·원격 소속; 후속 계획 branch/충돌 재평가 추가 확인 | IN_PROGRESS |
| Workflow2 사용자 Close/최신 편집, A4 | proposals, GitHub.close | proposals 최신 head/base Close 거부·Close 응답 유실 후 Agent 중복 없이 SATISFIED 확정 | IN_PROGRESS |
| Workflow3 base/계획/구현 분리·완료 불변 | observer, coordinator.workspace_definition | catalog terminal 불변·문서 오류/변경 generation·브랜치 draft/dependency 검사 | IN_PROGRESS |
| Workflow4 Worker→Verify→Reviewer→PR, A5/A7 | coordinator, runner, validation | coordinator 단계 겹침·늦은 결과·Checkpoint 게시 재개; 실제 publish까지 연결된 mock 흐름 보강 | IN_PROGRESS |
| Workflow5 manual start/complete, A3/A5 | direct_commands, Store.request, coordinator.requests | commands 예약 뒤 Git·동시 DB 쓰기·실패 경로 보존·출력 ack; 신규 DB/최신 문서 관측 추가 확인 | IN_PROGRESS |
| Workflow6 G Merge·R 전체 AC·Archive, A9 | record_pull, completion | proposals 실제 Merge가 renamed/reused R 평가 해제, completion Archive Push 응답 유실·별도 사용자 commit 거부 | IN_PROGRESS |
| 삭제/취소/ID 재사용·공유 목표, A2/A9/A10 | observer, record_pull, completion | catalog 철회·공유 G 보존·오류/rename/archive 구별·tombstone, proposals 원격 branch/전체 PR 이력 | IN_PROGRESS |
| 우선순위/동시실행, A5 | coordinator, Store.claim | coordinator Worker/Verify 겹침·전역/프로젝트 한도·살아 있는 orphan 자원·동급 신규 기아 방지 | IN_PROGRESS |
| Verify·Review 성공 근거, A7 | validation, processes, validation_receipts | agents 언어 독립 명령/입력 변경·실제 Job/자식 종료, coordinator 최신 R/G/PRODUCT·지침·후보 무효화 | IN_PROGRESS |
| PR 충돌/Draft/Ready, A8 | repository, proposals, coordinator | proposals 충돌 head/base 캐시·상태 별도 관측, 실제 Git 충돌 해결 후 Manager stage/commit; Ready 직전 재확인 | IN_PROGRESS |
| Git 안전/원격 삭제, A8 | Repository | git 실제 Merge·게시 후 재관측·원격 사용자 커밋·CRLF/ignored·미게시 보존·no-overwrite-ignore | IN_PROGRESS |
| 실패/중단/새 환경, A10 | coordinator, record_pull, Repository | coordinator 원격 PR 후보 인수 시 PASS 합성 없음·실행 중 process 중복 금지; branch-only ID 예약 | IN_PROGRESS |
| WAL·빠른 CLI·짧은 Tx, A3/A5 | Store, direct_commands | state 동시 claim·busy·rollback·늦은 결과·원자 요청/Outbox, commands 무효과 SELECT·Tx 밖 Git | IN_PROGRESS |
| pause/resume/unblock·프로젝트 변경, A5 | entrypoint, coordinator.settings_boundary/requests | pause 초기 판정·실제 unblock 결과, 설정/등록 해제 최종 결과 추가 연결 확인 | IN_PROGRESS |
| Provider 지침/홈/세션/Checkpoint, A6 | agents/runner, provider_home, processes | agents 역할 argv·Ponytail 1회·지침 hash·환경 비밀값 필터·실제 native ACL; 세션 소실 구별 | IN_PROGRESS |
| 자체 runtime 선택/drain/handoff, A11 | runtime/launcher/revision_runtime | cli 정확한 SHA·부모/자식 잠금·CLI 동시 쓰기·영수증 소비, completion 문서-only 무시·Git 실제 SHA clone/compile | IN_PROGRESS |
| 최소 데이터/후속 정리, A3/A9 | completion, Repository.defer_cleanup | completion 공유 session/auth·사용자 파일 보존·정리 실패 독립, 최종 usage 이후 토큰/세션 삭제 | IN_PROGRESS |
| 실제 사용량/PR 사용자 영역, A12 | agent_calls, usage_body, GitHub.update_usage | agents Provider cache/unknown, git Manager 영역 redaction·사용자 영역 보존; 최종 집계 갱신 연결 확인 | IN_PROGRESS |
| Telegram/CLI 결과/장기 무진전, A13 | outbox, Store, coordinator.stalled | outbox 전송 재개·UTF16 길이·중간 취소·미사용 취소, commands 출력 전 의도/후 ack, coordinator 실제 시도·24h 무진전 | IN_PROGRESS |
| 보안·실제 삭제/게시 경계, A13 | Repository, provider_home, agent_environment | 실제 Git/ACL·최소 환경·세션/auth 보존·PR redaction. 운영 데이터 미사용 | IN_PROGRESS |
| 검증 환경/비용, A14/AGENTS | scripts/verify.ps1, verification | WSL 전용 /tmp 복사·전체 timeout·정리, Windows 최소 native; 최종 두 단계 재실행 | IN_PROGRESS |
| 문서·설정·템플릿 | README, OPERATIONS, VERIFY, PLANS, config, copy scripts | 폐기 명령/옵션·maintenance 자료 제거; 링크/설치 복사·잔존 참조 최종 검사 | IN_PROGRESS |

## 기술 결정과 발견 사항

- 새 DB 스키마 버전 2만 지원하고 마이그레이션은 없다. `exec_plans.status`가 단계의 유일 원본이며 request/manual/generation은 실행 단계 복제가 아니다.
- R rename을 보존하려고 G sources의 원본 경로와 안정적인 R-ID 연결을 유지한다. 공유 G 재사용은 R의 작은 판단 근거에 ID 관계만 보관한다. 주 R 소속은 최초 계획 PR 이력에서 복원한다.
- 실제 문서/지침/검증 입력 변화만 성공을 무효화한다. 원격 SHA-only 전진은 Planner trigger가 아니다. PRODUCT/ARCH 변화는 실행 정의에 반영한다.
- CLI 출력·DB 사이 중단은 결과 유실보다 중복 가능성을 감수한다. 출력 확인 전까지 Outbox 의도를 유지하고 flush 후 CLI_FINAL로 확정한다.
- 실제 Git 회귀에서 충돌 해결 파일의 index가 U인 상태라 Manager commit이 막히는 문제를 발견했다. 표식/교정 확인 후 Manager가 stage·commit하도록 고쳤고 같은 실제 회귀가 통과했다.
- ignored 파일은 실행 위반이 아니며 Git의 실제 overwrite는 금지한다. 삭제 시 보존해야 할 ignored/미게시 파일이 있으면 공간 정리만 보류한다.
- 순차 Writer 인수를 기본으로 하고 살아 있는 이전 Agent/Verify는 자원 한도에 포함한다. 원격 branch 삭제는 PR 없는 게시 Checkpoint에도 자동 재생성을 허용하지 않는다.
- Planner Close 의도를 DB에 먼저 남겨 외부 Close 후 응답 유실을 재개한다. Archive는 게시 전후 원격을 재관측하고 코드/문서·별도 사용자 변경을 보호한다.
- BLOCKED 정책 질문 없음. 관측한 호출 오류는 보존한다. 실계정 Claude 미검증 자체는 개발 완료 차단 사유가 아니며 FUTURE_IDEAS에 남긴다.

## 검증 기록과 다음 작업

- 체크포인트 `a58baf8e` 공식 verify: WSL 118 성공, Windows 핵심 91 성공/9 skip. 두 단계 PASS. `.cache/verify/b02f037b8b344d4d8100ed916c103723/result.json`.
- 이후 집중 회귀: 71개 PASS (추가 수정 전). `.cache/rewrite-tests`를 TEMP/TMP/TMPDIR로 사용했다.
- 최신 실제 Git native 회귀 6개 PASS: Merge 충돌 해결, ignored overwrite 거부·보존, Checkpoint 게시 재개, 정확한 runtime SHA 준비. 격리 local bare remote만 사용했다.
- 최신 문서/CLI/Coordinator/Planning/Completion/Outbox 집중 54개 PASS (후속 계획·충돌 비교 기준 보완 전).
- TODO: 후속 계획/Planner 충돌·신규 DB 수동 준비·최종 PR 집계와 Worker→Verify→Review→실제 publish 함수 연결 회귀를 마친다.
- TODO: 문서 링크·필수 복사 파일·폐기 구성·미사용 코드와 모든 PRODUCT/ARCHITECTURE 계약을 최종 대조한다.
- TODO: WSL 전체+Windows 핵심을 최종 코드로 실행하고 결과를 구분해 기록한다. 이후 적절한 단위로 커밋·Push하고 PR 본문을 최종 범위로 갱신한다.
- TODO: 모든 조건이 실제 충족됐을 때만 목표 문서의 미구현 배너와 이 임시 계획을 정리하고 PR을 Ready로 전환한다. 미완료가 남으면 계획과 Draft를 유지한다.

- 2026-11-14 최신 연결 코드 공식 verify PASS: WSL 144 성공, Windows 핵심 117 성공/9 skip. 전체 60.6초. 보고서: .cache/verify/ffb76081a95d410da86d1acb78e86385/result.json. WSL 전용 /tmp 복사본 stopped/removed 결과는 최종 보고서에서 확인한다. 연결된 Worker→Verify→Review→Ready, 신규 DB 수동 G 인수, 후속 계획 PR, 최종 usage/CLI 결과 보존 회귀를 포함한다. 이후 코드 수정이 있으면 최종 verify를 다시 실행한다.
