# G-158 archive Requirement 일회성 감사

기준 원격 revision: `4be5702ee7bf133667bd3c58745659b527018237`. 사용자가 승인한 수동 감사이며 cron 감사 실행이나 구현 완료로 꾸미지 않는다.

archive Requirement **50개**, Acceptance Criteria **668개**를 고정 입력에 포함했다. 모든 과거 관련 계획의 Merge 여부와 현재 코드·테스트·후속 명세를 대조했다. 상세 기준 원문·판단·파일 blob/hash·심벌은 [requirement-audit.json](requirement-audit.json)에 있다.

## 복구 판단

기존 archive ExecPlan은 모두 실제 MERGED 이력이 있으므로 파일을 active로 되돌리거나 실행 상태를 초기화하지 않는다. 과거 계획의 일부 구현을 Requirement 전체 완료로 추정하지 않으며, 미완료이면서 이후 정책에 대체되지 않은 R-063 범위만 R-071/R-072/R-073로 재작성한다. 이 새 Requirement는 이번 PR에서 구현하지 않는다.

R-063은 전체 문서의 그대로 복원이 안전하지 않은 혼합 결과(`uncertain`)다. 마지막 구현 PR의 archive 조항은 R-068/G-157로 교체됐으므로 archive에 보존한다. 확인된 잔여 목표는 활성 R-068의 일회성 감사·누락 작업 연결 요구와 이번 사용자 결정에 근거한 새 Requirement로 보존한다. 새 Requirement에 과거 archive-only 경로를 active source로 넣지 않는다.

R-049 설정 생략 거부는 후속 G-131 AC3의 optional fallback으로 바뀌었다. README·코드·후속 계획이 일치하므로 strict 설정 정책과 과거 G-130~G-132를 복원하지 않는다. R-018 feedback/session 잔여 범위는 활성 R-033/G-066에 있어 중복 계획하지 않는다. R-061/R-064의 권한·공통 지침 목표도 해당 활성 계획에 남긴다.

| 새 Requirement | 유지할 잔여 범위 | 제외/보존 조건 |
|---|---|---|
| R-071 | 새 계획 ID·경로 교정, barrier 추가, 결과 형식 정규화와 사실 설명 호출 축소 | R-068 archive 정책·기존 의미 판단·소유권·검증 보호 |
| R-072 | 역할별 입력 동일성이 증명된 후보 성공 결과 재사용 | 실제 입력 변경에는 재검증, 최신 게시·승인 gate 유지 |
| R-073 | 호출 목적·실행 여부·사용량 범위 보존과 기간별 조회 | 부분 수집 구분, 수집 실패의 작업 성공 보존 |

## 전수 결과

`fulfilled`는 고정 revision의 코드·테스트 계약 대조 결과다. 이 Agent가 전체 테스트를 새로 실행했다는 뜻은 아니다. `superseded`는 뒤의 제품/계획 규칙으로 바뀐 부분을 다시 실행하지 않는 판단이며 유효한 공통 보호 조건은 유지한다. `uncertain`은 문서 전체 복원/전체 완료를 확정하지 않은 상태다.

| Requirement | 결과 | 판단과 후속 근거 |
|---|---|---|
| R-001 | superseded | READY-first 조건은 이후 입력별 Planner 선택으로 교체됐다. dependency·직렬 실행·priority와 compact catalog는 유지된다. 후속: R-024,R-037,R-049;G-130,G-132 |
| R-002 | fulfilled | PR head 기준 정리·미게시 작업 보존·cleanup pending의 비차단 경계가 구현돼 있다. 후속: R-022 ignored-only 정리 적용 |
| R-003 | fulfilled | Worker targeted 검증과 Manager full verify·읽기 전용 Review의 책임이 분리돼 있다. 후속: G-124,G-145; R-058 전체 runner는 별도 활성 입력 |
| R-004 | fulfilled | 변경을 설명하는 commit 제목과 Worker 결과 계약·immutable source 링크가 검증된다. 후속: documents/pr_description.py |
| R-005 | superseded | 마지막 Worker PR의 필수 archive 방식은 R-068과 G-157의 의미 판단·Manager planning 이동으로 교체됐다. bootstrap 과거 계획을 재실행하지 않는다. 후속: R-068;G-157 |
| R-006 | fulfilled | 시각 접두사·계획 식별·실패 복귀 로그와 lock 중복 실행 옵션이 구현돼 있다. run wrapper의 과거 pull 예외는 현재 원격 runtime 정책으로 대체됐다. 후속: R-010;runtime/process_lock.py |
| R-007 | superseded | 정확한 AGENTS 승인 절차는 보존한다. 미래 Planner 쓰기 권한은 활성 R-061, 공통 지침은 R-064/G-147, 사전 승인·결정 경계는 R-066/R-069가 변경한다. 기존 broad common/project 권한을 복원하지 않는다. 후속: R-061,R-064,R-066,R-069;G-147 |
| R-008 | superseded | verify 지문 반복 보호는 유지한다. Review 지문에 의한 최종 차단은 R-044/G-098의 외부 실행 교정 한도로 교체됐다. 후속: R-044;G-098 |
| R-009 | superseded | Review level 승격은 남아 있다. 최초 분류와 profile 선택의 변경은 활성 R-052를 따르며 구형 고정 Model 규칙을 되살리지 않는다. 후속: R-052;G-041 |
| R-010 | superseded | 원격 exact-SHA runtime과 기본 checkout 보존은 유지한다. 비차단 로컬 동기화·협업 ref scope는 R-056/R-060과 후속 구현으로 구체화됐다. 후속: R-056,R-060;G-135;manual G-160,G-162 |
| R-011 | fulfilled | 충돌 확정 뒤 같은 PR의 identity·lease·worktree를 검증하고 재검증·게시하는 경로가 있다. 후속: R-021,R-048,R-060의 현재 안전 조건 적용 |
| R-012 | superseded | 유지보수 DAG·barrier·사후 재검증은 유지한다. 기본 5·즉시 최초 평가·모든 PLAN_BLOCKED 대기는 R-042/R-054 및 단계별 정책으로 교체됐다. 후속: R-042,R-054;G-082~G-087,G-138 |
| R-013 | superseded | 계획별·프로젝트별 안전 차단 및 독립 작업 선택은 유지한다. 차단이 있으면 일반/유지보수 Planner를 일괄 금지하는 부분은 R-024/R-049/R-054/R-055로 변경됐다. 후속: R-024,R-049,R-054,R-055 |
| R-014 | superseded | 실제 MERGED 후 내용 보존 정리·원격 경쟁·pending 재시도는 구현돼 있다. SQLite 현재 path 갱신과 최초 게시 링크 보존은 R-017/G-036의 분리된 cache 정책을 적용한다. 후속: R-017;G-036 |
| R-015 | superseded | 안전한 차단 작업물 checkpoint는 구현돼 있다. PR 없는 branch만 게시하는 제한·프로젝트 차단시 무조건 local-only는 R-060의 같은 PR/진단 ref 보존으로 교체됐다. 후속: R-046,R-060;G-107;manual G-160 |
| R-016 | superseded | Planner 전용 bytecode 비활성화와 ignored 순변경 차단은 R-022의 ignored-only 허용 정책으로 명시적으로 교체됐다. 후속: R-022;G-049 |
| R-017 | fulfilled | 최신 원격 선언 cache를 원자적으로 반영하고 실행·session·PR·최초 게시 감사와 분리한다. 실제 사용자 삭제의 확대 복구는 활성 R-055 목표다. 후속: R-055;G-117 |
| R-018 | superseded | provider/Model에 결합된 session 재개·교체와 기본 usage 기록은 구현돼 있다. feedback delta의 미완료 범위는 활성 R-033/G-066에서 보존돼 있어 과거 G-039~G-041을 재실행하지 않는다. 확장 usage는 R-063 잔여 Requirement로 분리한다. 후속: R-033,R-063;G-066,G-134 |
| R-019 | superseded | archive Worker 교정은 R-068, Review 지문 차단은 R-044, 과거 replay 증거 강제는 R-048로 교체됐다. transient/state-reconcile의 독립 복구와 안전 거부는 남아 있다. 후속: R-044,R-048,R-055,R-068 |
| R-020 | fulfilled | 정확한 runtime 등록 identity의 Manager 귀속과 persisted 범위 복구·다른 공유 변경 거부 경계가 있다. 후속: R-060;G-137;manual G-160 |
| R-021 | fulfilled | MERGEABLE/UNKNOWN은 충돌 preflight로 오차단하지 않으며 ignored 경로 충돌과 실제 권위 상태 불일치는 계속 거부한다. 후속: R-022,R-060 적용 |
| R-022 | fulfilled | ignored-only 상태는 비권위 상태로 처리하며 index·ignore rule·operation·ref 변경과 tracked 경로 충돌의 안전 경계를 유지한다. 후속: R-059 cache 검증은 별도 활성 목표 |
| R-024 | superseded | mutable READY snapshot·원자적 게시·취소 ID 보존은 구현돼 있다. 일반 Planner/no-runnable 선택은 후속 R-037/R-049의 입력 분류로 변경됐다. 후속: R-037,R-049;G-130,G-132 |
| R-025 | superseded | 현재 SPEC_BLOCKED의 근거·개별 차단·중복 금지 경로는 구현돼 있다. 미래 모호한 계획의 draft 처리·Worker 문서 권한은 활성 R-061 목표로 유지한다. 후속: R-061 |
| R-026 | fulfilled | timeout 설정·process tree 종료·불확실 종료 안전 차단·한도 부족 사용자 판단 경계가 구현돼 있다. 후속: verification/runner.py;G-145 pending cleanup refactor |
| R-027 | superseded | 공통 품질·self-only 전수 평가·원문 분리된 승인 draft의 구현은 남아 있다. 이후 tiered 평가와 R-061/R-064의 문서/지침 권한을 적용하며 오래된 평가·AGENTS 변경 계획을 복원하지 않는다. 후속: R-042,R-061,R-064;G-078,G-079,G-115,G-119~G-129 |
| R-028 | fulfilled | 루트 고정 경로와 실제 진입/도구 근거 기준은 유지하며 내부 모듈 분리는 G-119~G-127에 반영돼 있다. 후속: G-119~G-129;maintenance/code_index.py |
| R-029 | fulfilled | 같은 외부 실행 실패의 진전·연속 횟수·원인과 기존 BLOCKED 중복 제거를 상태와 outbox로 관리한다. 후속: G-041,G-045 후속 경계 유지 |
| R-030 | fulfilled | Git checkout 줄바꿈 변환을 내용 변경과 구분하며 실제 READY 변경·revision·ownership 보호를 유지한다. 후속: manual G-162의 CRLF fixture 교정 |
| R-031 | fulfilled | 안정 상태의 실제 대기 원인과 완료 gate를 구분하고 같은 사건을 중복 알리지 않는다. 후속: R-057 최종 확인의 추가 조건은 활성 입력 |
| R-032 | fulfilled | 식별 가능한 실패 구간 우선·제한된 fallback·원문 보존·성공 출력 미전달 경계가 있다. 후속: R-033 session delta의 추가 범위는 G-066 |
| R-034 | fulfilled | 알린 차단의 유효 해제와 실제 해당 작업 시작을 구분하고 미전송 조각 취소·한 번의 재개 사건을 유지한다. 후속: G-142는 알림 schema refactor로 유지 |
| R-035 | fulfilled | Reviewer는 검증 성공 뒤 제한된 예외 사유만 받으며 성공 원문 전달·테스트 재실행을 요구하지 않는다. 후속: R-061 관련 diff 검토는 활성 입력 |
| R-036 | fulfilled | 최초 full verify 전에 최신 base를 반영하고 후보 변경 뒤 검증 token을 무효화한다. 후속: R-063 input-equivalence 예외는 아직 미구현;R-072로 분리 |
| R-037 | superseded | 코드 변경에도 항상 일반 Planner를 쓰는 조건은 R-049의 읽기 전용 점검·일회 인계로 교체됐다. source/archive-only 입력 구분은 유지한다. 후속: R-049;G-130~G-132 |
| R-038 | fulfilled | 길이별 순서 보존 분할·확인된 조각 cursor·실패 재개·차단 해제시 미전송 조각 취소가 구현돼 있다. 후속: R-034 재개 사건과 결합 |
| R-039 | superseded | 안전한 원격 후속 commit 채택 경계는 유지한다. 원격 분기와 base 동시 전진·기존 PR 처리 제한은 R-046/R-060의 협업 복구로 확대됐다. 후속: R-046,R-048,R-060;G-106,G-110 |
| R-040 | fulfilled | 비종료 dependency closure에 필요한 MERGED ID만 검증하고 무관한 완료 본문은 Planner에 전달하지 않는다. 후속: G-070/G-054 관계는 과거 회귀 입력 |
| R-041 | superseded | 원격 사용자 계획의 identity·원자적 수용·이력 보존은 구현돼 있다. ID 공백/삭제+새 ID의 일괄 거부는 이후 R-055의 삭제 정리 목표로 교체돼 옛 규칙을 다시 강화하지 않는다. 후속: R-055;G-117 |
| R-042 | superseded | 파일별 원본/digest·재개·관계 근거는 유지한다. 이후 PRODUCT의 static fast/targeted/full 모듈 평가와 기본20을 적용하며 종전 파일/관계/종합 단계를 새 기본으로 복원하지 않는다. 후속: PRODUCT 주기적 리팩터링;R-063;manual G-154 |
| R-043 | fulfilled | planning projection·오류·workspace·session과 독립 차단의 상태 소유를 분리하며 원인 해소 전 planning 오류를 지우지 않는다. 후속: R-055 broader planning_error nonblocking은 활성 목표 |
| R-044 | fulfilled | 외부 실행별 Review 교정 한도·기존 phase 보존·legacy 반복 지문 재평가와 별도 안전 차단을 구현한다. 후속: G-098;verify 실패 한도와 분리 |
| R-045 | superseded | 과거 commit별 replay/승인 receipt의 필수 증명은 R-048에 의해 교체됐다. 최종 ownership·target ancestry·범위·원격 lease는 계속 검증한다. 후속: R-048;G-108~G-110 |
| R-046 | superseded | ref 관측·실행 슬롯 보존·원인별 해제는 구현돼 있다. replay 필수·원격 후속 only 경계는 R-048/R-060으로 변경됐다. 후속: R-048,R-060;G-137,G-140;manual G-160 |
| R-047 | superseded | 동일 소유 branch의 중간 checkpoint·정확한 lease·검증 미완료 상태는 유지한다. 과거 연속 replay evidence 강제는 R-048로 교체됐다. 후속: R-048;G-109 |
| R-048 | fulfilled | 과거 receipt 부재 대신 현재 소유 Git 상태로 완료를 수용하고 진짜 ownership/범위/원격 불일치와 별도 차단을 유지한다. 후속: G-108~G-110;manual G-162 회귀 |
| R-049 | superseded | 일반/읽기 전용 점검의 입력·권한·인계는 G-130~G-132로 구현됐다. 설정 생략 거부는 후속 G-131 AC가 명시적으로 optional planner_check fallback으로 바꿨고 코드·README도 일치하므로 strict 재구현을 제외한다. 후속: G-131 AC3;R-057,R-061 |
| R-051 | fulfilled | 보존된 planning 결과·staged/untracked 작업·관련 문서 변경·원격 base 반영의 재개 경계가 구현돼 있다. 후속: G-118,G-128,G-129;현재 rebase adoption 정책 유지 |
| R-060 | fulfilled | 선택된 ref/worktree 중심 scope·협업 변경 수용·원격 checkpoint/진단 보존 경계가 후속 수정을 포함해 구현돼 있다. runtime 관측과 실제 잔여 독립 차단은 혼동하지 않는다. 후속: G-135,G-137,G-140;manual G-160,G-162 |
| R-063 | uncertain | 문서 전체 복원은 R-068이 대체한 마지막 구현 PR archive 규칙을 되살리므로 하지 않는다. 그 외 확인된 미구현 목표는 R-071/R-072/R-073에 재작성한다. G-134는 일부 invocation 보존 결함만 해결했다. 후속: R-068;G-134;R-071/R-072/R-073 |

## Manager 경계 확인

운영 DB의 일관된 backup에서 기존 Manager `RequirementRetirementPort`로 50개 입력을 pin하고 8개씩 결과를 검증·저장했다. 동일 입력 재조회에 pending 0개·blob 재읽기 0회를 확인했다. 생산 DB의 사건을 임의로 재생성하거나 요구사항 판정을 모델 없는 키워드 분류로 만들지 않았다. 같은 판단을 기존 Manager API로 생산 DB에도 등록했다. 전역 process lock과 등록 전 DB backup을 사용했고, 50개 결과 및 재시작 뒤 동일 입력 pending 0개·blob 재읽기 0회를 확인했다. 계획·프로젝트·유지보수 상태는 바뀌지 않았으며 별도 운영 receipt에 등록 시각·전후 상태를 보존한다.

현재 G-142/G-144/G-145는 G-158에 의존하며 기존 plan·session·branch·worktree·feedback·차단을 유지한다. 조기 archive 강제는 R-068/G-157에 비춰 재평가할 대상이다. G-145의 독립 Windows fcntl/cleanup 피드백과 그 밖의 scope/ownership 차단을 이 감사로 일괄 해제하지 않는다.

## 검증과 남은 조건

문서·계획 전용 diff이며 실행 코드는 변경하지 않는다. 새 targeted unit test가 필요한 코드 변경이 없어 unit test는 실행하지 않는다. 실제 parser로 전체 active/archive ExecPlan의 형식·source·고유 ID·dependency DAG를 검증하고 감사 대상/criteria/hash/근거 파일 전수와 동일 입력 재사용을 확인한다. 전체 verify와 Reviewer 검토는 Manager의 외부 정상 절차가 담당하며 실제 통과 기록 전에는 성공으로 표시하지 않는다. PR은 사용자 Merge 전 MERGED가 아니다.

## 사용자 문서 범위 결정

미충족 잔여 목표는 활성 R-071·R-072·R-073에 보존한다. 새 ExecPlan은 작성하지 않으며 이후 Planner가 활성 Requirement를 기준으로 계획한다. G-158에는 감사 결과·복구 준비·실제 확인한 Progress만 기록한다. 감사 JSON은 원문·입력·기준별 근거를 고정한 증거이며 활성 Requirement나 새 계획 입력을 대신하지 않는다.
