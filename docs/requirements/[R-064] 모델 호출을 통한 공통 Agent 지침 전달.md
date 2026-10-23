# [R-064] 모델 호출을 통한 공통 Agent 지침 전달

## Summary

자동 Agent의 공통·역할 지침을 현재 선택된 최신 runtime 원본에서 매 호출 읽어 provider의 developer/system instruction으로 전달하고, 프로젝트 CLI 지침 및 작업별 prompt와 분리한다. 직접 대화형 작업은 관련 활성 Requirement를 재사용·갱신하되 ExecPlan을 선작성하지 않으며 Skill에는 문서 작성·검토 규칙만 둔다. 지침 파일 변경의 PR 문서 보고는 파일명·요약·이유·영향만 표시하고, 원본 버전·session·호출 감사와 기존 권한·검증·게시 경계를 유지한다.

## Context

프로젝트별 파일 복사 없이 자동 실행의 공통·역할별 지침을 전달하고 실제 사용한 버전을 재현할 수 있어야 한다. 프로젝트의 개발 지침 해석은 AI Agent CLI에 맡긴다.

## Requirement

### 공통·역할별 규칙과 프로젝트 규칙

- Skill과 Manager가 프로젝트 안에서 고정 위치로 사용하는 `.agents/`·`docs/`·`scripts/`와 명세·템플릿·계획·요구사항·verify·코드맵·Skill 경로는 예약 경로로 유지한다. 예약은 위치 고정이며 모든 파일의 생성 의무가 아니다. Loop Manager runtime에서만 읽는 `docs/AGENTS.*.md`는 프로젝트 예약 경로가 아니다. Loop Manager 자체 개발에만 필요한 새 루트 항목 제한·instruction 원본 파일 수정 안내는 이 저장소의 루트 `AGENTS.md`에 둔다. 각 프로젝트의 개발·구조 규칙은 예약 경로를 유지하는 범위에서 해당 프로젝트 지침을 따른다.
- 실행 지침의 원본은 Loop Manager runtime의 `docs/AGENTS.common.md`와 역할별 `docs/AGENTS.planner.md`·`docs/AGENTS.planner_check.md`·`docs/AGENTS.worker.md`·`docs/AGENTS.reviewer.md`다. 공통에는 모든 자동 역할에 적용되는 기준만 남기고, 특정 역할의 권한·금지사항·작성·판단·검토·검증 절차는 해당 역할 파일에 둔다. 파일들은 이어붙일 실행 지침이므로 자신의 구성·합성·snapshot·버전 관리에 대한 설명을 넣지 않는다. 초기 분리는 기존 루트 AGENTS와 역할 prompt의 규칙을 기준으로 필요한 내용만 옮기며 기준 revision·분리 내용·이유를 구현 PR에서 설명한다. 루트에는 Loop Manager 개발에 필요한 작업 지침만 두며 docs의 규칙·스펙과 코드·문서로 쉽게 확인할 수 있는 설명을 반복하지 않는다.
- 게시된 공통·역할별 파일이 이후 실행 원본이다. 각 프로젝트에 복사하지 않고 프로그램이 선택한 게시된 runtime worktree의 내용을 사용한다. source checkout의 로컬 편집이나 선택한 작업 worktree·등록 프로젝트의 동명 파일을 실행 지침의 원본으로 사용하지 않는다.
- Manager는 일반·유지보수·수동 제출 Planner에 common+planner, 읽기 전용 점검에 common+planner_check, Worker에 common+worker, Reviewer에 common+reviewer를 합쳐 전달한다. 다른 역할의 상세 지침을 한 호출에 함께 넣지 않는다. 합친 실행용 파일은 저장소 밖 Manager 상태 디렉터리에만 만들며 커밋하지 않는다. 실행용 파일은 현재 runtime 원본에서 생성하며 작업별 원본 snapshot은 저장하지 않는다. 새 호출의 선택은 R-082를 따르며 ExecPlan·코드 변경만의 점검 Planner는 제거한다. 기존 점검 원본·snapshot·호출 기록은 호환·감사를 위해 보존하고, 원본이나 profile이 있다는 사실을 새 호출 사유로 사용하지 않는다.
- 모든 지원 역할·provider의 신규·재개·복구 호출에 common+해당 역할 내용을 Codex의 추가 developer instruction 또는 Claude의 append system prompt로 전달한다. provider의 기본 지침과 기존 추가 지침, 프로젝트 규칙을 보존한다. 정적 공통·역할 규칙은 사용자 메시지에 복사하지 않으며 작업별 목표·snapshot·승인 사실·피드백·결과 형식만 prompt에 둔다.
- 공통·역할별 instruction은 Loop Manager 자동 실행의 역할·권한·승인·검증·게시 책임에 적용한다. 프로젝트의 언어·코딩 규칙·빌드 방법은 프로젝트 지침을 따른다. 프로젝트 지침이나 작업별 사용자 prompt만으로 자동 실행 권한을 넓히거나 필요한 승인을 생략하지 않는다. 제품 목표의 의미를 바꿔야 해결되는 충돌은 사용자 판단을 기다린다.

### 로컬 직접 작업과 자동 실행의 경계

- ExecPlan 생성·계획 작성은 Loop Manager가 호출하는 Planner를 포함한 매니저의 자동 계획 흐름이 담당한다. 사용자가 직접 수행하거나 대화형 Agent에게 위임한 로컬 작업은 구현 전에 ExecPlan을 작성하지 않는다. Skill에도 직접 작업의 ExecPlan 선작성 의무를 두지 않는다. 저장소가 등록돼 있다는 이유로 직접 작업을 자동 Worker 작업으로 취급하지 않는다.
- 새로운 기능이나 정책 변경은 직접 작업과 자동 작업 모두 Requirement에서 시작한다. 대화형 Agent는 사용자 요청의 관련 활성 Requirement를 먼저 작성하거나 갱신하며 별도의 문서 작성 요청을 요구하지 않는다. 기존 활성 Requirement가 요청·Acceptance Criteria를 충분히 담으면 재사용하고 중복 파일을 만들지 않는다. 보관된 Requirement는 읽기만 하며 재사용·수정·삭제·활성 위치 복원을 하지 않는다. 보관 문서와 관련된 새 변경은 새로운 고유 ID·경로의 Requirement로 작성한다. 미결정 정책은 사용자 판단으로 해소하며 자동 Planner·Worker가 새 제품 목표를 임의로 추가할 권한으로 확대하지 않는다.
- 프로젝트로 복제하는 Specification Skill에는 PRODUCT·ARCHITECTURE·Requirement·FUTURE_IDEAS 작성·검토와 사용자가 알아야 할 문서 취급 기준만 둔다. Requirement 작성·갱신과 보관 문서의 읽기 전용 범위는 유지하며 계획 생성·역할별 실행·SQLite·runtime·게시·archive 판정 같은 Manager 동작은 설명하지 않는다. 이 저장소 루트 AGENTS는 Skill을 참조하며 이미 설명된 문서 작성 규칙을 반복하지 않는다.
- 직접 작업에 Loop Manager의 SQLite 상태 전이·Worker와 Reviewer의 역할별 절차·ExecPlan 완료 판정을 요구하지 않는다. 자동 작업에는 기존 ExecPlan·dependency·targeted test·Manager 전체 verify·Review·사용자 Merge 조건을 그대로 적용한다. 직접 작업의 결과만으로 자동 작업의 상태나 검증 성공을 대신 기록하지 않는다.
- 로컬에서 구현한 결과와 활성 Requirement가 `origin/<base-branch>`에 반영되면, Loop Manager는 기존 입력 변경·작업 선택 정책에 따라 실제 코드·테스트와 요구사항의 정합성·미충족 작업을 재점검한다. 이미 충족된 구현은 중복 계획하지 않고 직접 작업에 소급해 ExecPlan을 요구하지 않는다. 남은 작업에 필요한 ExecPlan만 생성하거나 허용된 기존 계획을 개정한다. 미커밋 변경·로컬 전용 commit은 자동 입력으로 사용하지 않으며, Requirement의 유지·archive 조건과 동일 입력에서의 반복 호출 억제는 유지한다.

### 현재 runtime 적용과 실패

- 모든 역할·provider의 신규·재개·재시도·fallback 호출은 현재 실행이 선택한 최신 runtime의 공통·역할 원본을 읽는다. 진행 중인 ExecPlan과 독립 planning·유지보수·수동 제출 작업도 이전 runtime 지침에 고정하지 않는다. runtime 교체는 R-023의 안전한 호출 반환·재시작 경계를 따른다. 이미 실행 중인 호출의 지침을 도중에 바꾸지 않는다.
- 작업별 원본 묶음 snapshot은 새로 저장하거나 실행 입력으로 읽지 않는다. 과거 JSON·Markdown snapshot과 DB 기록은 감사 자료로 보존하며 누락·손상도 현재 runtime 지침 선택에 영향을 주지 않는다. 전달에 필요한 합친 파일만 저장소 밖 실행용으로 생성한다.
- 실제 전달 내용의 해시를 호출 기록에 남긴다. session의 작업 귀속은 지침 버전과 분리하며 같은 작업의 세션 재개에도 현재 runtime 지침을 전달한다. 과거 호출의 지침 해시·성공·실패·세션 이력을 최신 값으로 소급 변경하지 않는다.
- 모든 역할 원본을 포함한 현재 내용 해시를 planning 입력과 관련 Agent 성공 결과의 입력 동일성 판단에 사용한다. 진행 중인 작업에도 지침 변경을 반영하며 다른 지침으로 얻은 성공을 같은 입력의 성공으로 재사용하지 않는다. 내용이 같으면 runtime SHA나 위치 변경만으로 재계획하지 않는다.
- 작업 worktree의 동명 파일 편집·diff 검토는 실행 지침을 바꾸지 않는다. 게시 후 새 runtime이 선택되면 기존 작업의 다음 호출부터 개선된 지침을 사용한다. 공통·역할·Ponytail 지침만 변경된 경우도 runtime 갱신 입력이다.
- 지침 기록 없는 기존 세션은 기존 작업물·세션·실패 이력을 보존하고 지원되는 최소 문맥 복구로 이어간다. 현재 runtime의 원본 누락·빈 내용·읽기·인코딩 오류, 알 수 없는 역할, 실행용 파일 변조·저장 실패, 전달 기능 미지원·설정 충돌은 해당 모델 실행 전에 거부한다. 과거 snapshot으로 대체하거나 권한을 확대하지 않는다.

### 공통·역할별 파일 변경

- 작업 worktree의 공통·역할별 `docs/AGENTS.*.md`는 현재 역할·목표의 일반 파일 수정 권한을 따른다. 파일 이름만으로 추가 수정 제한이나 guidance 제안·사전 승인·승인 대기 중단을 요구하지 않는다. Planner·Reviewer의 일반 쓰기 제한과 실행 중 runtime worktree 자체의 편집 금지는 유지하며, 작업 중 역할 파일 수정으로 현재 실행 권한을 확대하지 않는다.
- 공통·역할별 파일이 변경된 PR의 문서 보고는 R-087에 따라 파일명·변경 요약·이유·영향만 표시한다. 문서 발췌·전체 patch·diff 링크·검증용 hash는 PR 본문에 넣지 않는다. Manager는 실제 최종 diff·hash와 보고를 대조해 누락을 보완하고 Reviewer는 일반 변경과 함께 검토한다. PR Merge·새 runtime 선택 전의 수정본은 현재 실행 지침으로 사용하지 않는다.

### 설치

- 준비 도구는 Specification Skill·템플릿·기본 제품 및 설계 문서와 작업 폴더만 준비하며 Agent CLI 지침 파일을 다루지 않는다. 기존 파일·폴더의 덮어쓰기 보호는 유지한다.


## Acceptance Criteria

- 등록·계획 입력·호출·검증·게시에는 Agent CLI의 루트 지침 파일을 관리하는 별도 계약이 없다. R-066의 지침 독립성을 따르며 기존 감사와 일반 사용자 결정은 보존한다.
- Skill과 Manager의 프로젝트 예약 경로는 공통 지침과 명세 작성 Skill에 명시한다. runtime 전용 지침과 `docs/plans/manual`은 예약 경로에서 제외한다.

- 같은 등록 프로젝트에서 직접 작업은 관련 활성 Requirement 작성·갱신 또는 충분한 기존 활성 문서의 재사용으로 시작하고 ExecPlan을 만들지 않는다. 보관 Requirement는 읽기만 하며 새 변경은 신규 Requirement로 작성한다. 새 ExecPlan은 크기와 관계없이 매니저의 자동 계획 흐름에서 작성하며 직접 작업에 자동 역할·SQLite 상태·ExecPlan 완료 절차를 요구하지 않는다.
- runtime의 common에는 모든 역할의 공통 기준만, 역할 파일에는 해당 역할의 규칙만 남는다. 초기 분리의 기준 revision·내용·이유를 확인할 수 있고 이후 원본은 게시된 공통·역할별 파일이다. 작업별 사용자 prompt에서 원본을 재생성하지 않는다.
- Specification Skill은 네 종류의 제품 명세 작성·검토와 보관 Requirement 취급 기준만 설명하며 Manager 내부 동작은 포함하지 않는다. 이 저장소 루트 AGENTS는 Skill을 참조하며 작성 절차·문서 역할을 중복하지 않는다. 별도 문서 작성 요청이 없다는 이유로 대화형 Agent의 사용자 요청에 따른 Requirement 작성을 거부하지 않는다.
- 이 저장소 루트 AGENTS에는 Loop Manager 개발에 필요한 작업 지침만 남긴다. Loop Manager 전용 새 루트 항목 제한·instruction 원본 파일 수정 안내는 다른 프로젝트에 주입하지 않으며 Manager 예약 경로·필수 문서·verify 경로의 보존은 공통 지침에 유지한다. 문서·코드에 있는 규칙·스펙·구조·실행 설명을 중복하지 않으며 필수 문서 조건은 등록·계획 입력 검사에서 유지한다.
- 공통·역할별 instruction에는 해당 역할의 행동 기준만 있고 지침 파일 자체의 구성·합성·snapshot·버전 관리 설명은 없다. 실행용 파일 생성과 현재 runtime 원본 선택은 기존 Manager 코드와 제품·설계 명세에서 유지된다.
- 직접 구현과 활성 Requirement를 원격 base에 반영하면 기존 Planner 선택 정책에 따른 재점검이 이루어진다. 충족된 구현의 중복 계획·소급 ExecPlan 요구·자동 검증 성공 합성 없이 남은 작업만 계획하며, 로컬 전용 변경과 기존 archive·반복 호출 억제 조건을 보존한다.
- 모든 지원 provider·역할의 신규·재개·복구 호출이 common+해당 역할을 developer 또는 system instruction으로 전달하며 기본 지침·기존 추가 지침·프로젝트 규칙과 실행 경계를 보존한다. 작업별 사용자 prompt에는 정적 공통·역할 규칙이 중복되지 않는다.
- 같은 작업의 Worker·Reviewer·재시도·복구·새 세션은 매 호출 현재 runtime의 역할별 지침을 사용한다. 직접 commit을 허용했던 과거 지침을 가진 작업도 최신 Manager 전용 checkpoint 규칙을 전달받으며 호출별 해시와 기존 감사가 보존된다.
- 작업별 원본 snapshot은 생성·재사용하지 않는다. 과거 snapshot의 누락·손상과 무관하게 현재 runtime 원본으로 호출하고, 현재 원본 오류·알 수 없는 역할·실행용 파일 변조는 실행 전에 거부한다.
- 공통 또는 역할 파일만의 내용 변경도 관련 planning·성공 결과 재사용 판단에 반영되지만 동일 내용의 재실행·runtime 갱신은 불필요한 반복 재계획을 만들지 않는다.
- 버전 기록 없는 기존 세션의 복구는 원래 작업·세션·실패 이력을 보존한다. 현재 runtime 원본·실행용 파일 오류와 전달 기능 미지원은 모델 프로세스 시작 전에 확인되고 해당 호출의 진단으로 남는다.
- 작업 worktree의 공통·역할별 파일은 허용된 역할·목표의 일반 권한으로 수정·검토·게시할 수 있고, 파일 이름만으로 guidance 승인 대기나 특별한 범위 위반을 만들지 않는다. 후보 편집본은 실행 입력이 아니며 새 runtime 선택 후 다음 호출에서 적용된다.
- 공통·역할별 파일 변경 PR의 문서 보고에는 파일명·변경 요약·이유·영향만 있고 발췌·patch·diff 링크·hash는 없다. 실제 최종 diff·hash와 보고의 일치를 검증하며 PR head·rebase 변경 후의 누락이나 오래된 보고도 보완된다.
- targeted test·Manager 전체 verify·읽기 전용 Review의 책임, dependency·게시·사용자 Merge 조건은 유지된다.

## Product Impact

- 프로젝트별 지침과 Loop Manager 공통·역할별 실행 규칙을 따로 관리할 수 있다.
- Requirement는 직접 작업과 자동 작업의 공통 변경 기준으로 유지하고, ExecPlan은 Loop Manager 자동 구현의 실행 계획으로 사용한다.
- 공통·역할 규칙은 프로젝트별 파일 배포 없이 기존 작업의 다음 호출에도 반영되고, 과거 호출의 실제 전달 해시는 보존된다.
- 공통·역할별 파일 변경은 일반 작업·PR 검토로 처리한다.

## Non-Goals

- 기존 프로젝트의 공통 본문을 자동 판별·제거하거나 기본 checkout에 commit·push하지 않는다.
- 공통 파일 링크의 자동 추적, 원격 URL 지침 다운로드, 사용자 전역 설정 변경을 요구하지 않는다.
- 일반 역할 권한·검증·Git 소유권을 완화하거나 R-061의 다른 정책을 이 변경만으로 구현하지 않는다.
- provider transport, sandbox 또는 Model 선택 정책을 변경하지 않는다.
- 기존 session·승인·실패 이력을 삭제하거나 전체 대화를 재구성하지 않는다.
- 문서 작성만으로 운영 SQLite·기존 ExecPlan 상태·프로젝트 등록을 변경하지 않는다.

## Open Questions

- 없음
