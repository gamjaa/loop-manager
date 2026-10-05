# [R-069] User Decision 경계와 입력 양식

## Context

현재 AGENTS 변경 승인 파서는 User Decision 제목부터 다음 guidance 제목 또는 Decision Log 끝까지를 결정 본문으로 검증한다. 결정 뒤에 일반 메모를 추가하면 메모까지 결정 형식에 포함돼 파싱이 실패할 수 있다. 사용자가 결정 식별자인 Revision이나 문법을 직접 구성하지 않고 Decision과 필요한 설명·대체 patch만 작성할 수 있도록, 결정을 요청할 때 완전한 양식을 미리 제공해야 한다. 적용할 제안 diff도 결정란에서 바로 확인하고 편집할 수 있어야 하며, 사용자는 직접 편집하거나 모델의 도움을 받아 작성할 수 있어야 한다.

## Requirement

### 결정 본문의 경계

`### Agent Guidance User Decision: <proposal-id>`의 본문은 코드 블록 밖의 독립된 줄에 있는 `<!-- END USER DECISION -->` 직전까지만 읽는다. 마커 줄의 앞뒤 공백은 허용하고, 코드 블록이나 diff 내용 안의 같은 문자열은 종료 마커로 해석하지 않는다.

마커 뒤에는 일반 문단·목록·Markdown 제목 등 메모를 허용한다. 메모는 결정 필드로 해석하지 않으며, 결정 identity와 확정 patch에 포함하지 않는다. 메모 추가·수정만으로 승인 대기를 해제하거나 기존 승인을 변경하지 않는다. 유효한 결정과 메모를 함께 편집한 경우에도 메모 때문에 결정 파싱이나 정상 재개가 실패하지 않아야 한다. Proposal과 다른 ExecPlan 본문의 변경을 결정 편집으로 인정하지 않는 기존 보호는 유지한다.

종료 마커는 새 양식의 기본값이지만 필수 입력은 아니다. 마커 없는 기존 형식은 다음 guidance 제목, 다음 최상위 `##` 절 또는 EOF를 기존 경계로 사용한다. EOF 직전 결정이 완전하면 마지막 개행이나 마커가 없어도 정상 처리한다. 마커 없는 결정 뒤의 일반 메모를 자동으로 추측해 잘라내지는 않는다.

마커 뒤에서도 실제 guidance 제목은 다음 Proposal·User Decision으로 검증한다. 일반 메모로 취급해 누락·중복·ID 불일치를 숨길 수 없어야 한다.

### 입력 양식과 사용자 편집

Worker가 새로운 AGENTS 변경 결정을 요청할 때, 선택된 ExecPlan의 Decision Log에 기존 Proposal과 같은 ID의 다음 양식을 작성한 뒤 중단한다. 작성에 필요한 설명은 필드 옆의 짧은 주석으로 제공하고, 별도 사용자 작성 안내문이나 표를 붙이지 않는다. 알림은 편집할 파일과 User Decision 위치를 안내한다.

````markdown
### Agent Guidance User Decision: guidance-example
Decision: `pending` <!-- pending: 대기, approve: 승인, reject: 거절, replace: 대체 -->
Comment: `` <!-- 선택: 백틱 안에 결정 이유나 참고 사항 입력 -->
Patch: <!-- 제안 diff; 수정본을 적용하려면 Decision을 replace로 변경 -->
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1,1 +1,2 @@
 기존 줄
+추가할 정확한 줄
```
<!-- END USER DECISION -->
````

Decision·Comment·Patch 옆의 HTML 주석은 작성 안내이며 필드 값이나 patch로 읽지 않는다. 주석이 없는 기존 형식도 허용하고, 코드 블록·diff 안의 주석 문자열은 내용으로 보존한다. 실제 필드 값에는 `여기에 작성` 같은 placeholder를 넣지 않는다. 별도 작성 안내나 선택 메모 영역을 생성하지 않고, Comment에 입력 위치를 보여주는 빈 백틱을 제공한다. Comment는 백틱 안에 작성하는 선택적인 한 줄 설명이며 생략하거나 비워 둘 수 있다. Comment는 결정 identity·확정 patch·재승인 여부에 영향을 주지 않는다.

새 양식은 `pending`과 빈 Comment를 사용하고, Patch 코드 블록에 해당 Proposal의 정확한 전체 diff를 미리 채운다. 예시 diff를 그대로 복사하거나 사용자가 채워야 할 빈 블록을 만들지 않는다. Revision을 생성하거나 요구하지 않으며, 일반 pending 양식에는 Baseline을 추가하지 않는다. 사전 승인에서는 Decision 다음의 Baseline 필드 옆 주석으로 실제 기준 commit의 전체 SHA를 입력해야 한다는 조건을 안내하며, 같은 배치의 기준 일치와 원격 게시 검증을 유지한다.

Patch는 모든 Decision에서 표시된 채 둘 수 있으며, 사용자가 직접 또는 모델의 도움으로 수정할 수 있다. Loop Manager는 다음 기준으로 확정 patch를 선택하며, 실제 적용은 기존 Worker 절차를 따른다.

- `pending`: Patch는 검토용이다. 사용자가 diff를 편집해도 결정을 기다리며 적용하지 않는다.
- `approve`: Proposal의 원안을 적용한다. 미리 채운 Patch는 지우지 않아도 된다. Patch가 원안과 다르면 수정본을 무시하거나 임의로 적용하지 않고 `replace`로 확정해야 한다고 안내한다.
- `replace`: User Decision의 Patch에 있는 정확한 전체 diff를 적용한다. 비어 있으면 거부하며, 원안과 같은 diff도 허용한다.
- `reject`: 표시된 Patch를 유지해도 대상 파일을 변경하지 않는다.

빈 Patch를 사용하는 기존 pending·approve·reject 형식도 계속 지원한다. 미리 채운 원안과 기존 빈 블록은 같은 Decision에서 동일한 승인 의미로 정규화해 처리한다. 표시 여부만으로 승인 identity를 바꾸거나 재승인을 요구하지 않으며, 적용 대상으로 확정된 diff는 승인 기록에 결합한다. 값이 있는 Patch는 Decision과 관계없이 기존 대상·Scope·unified diff 형식을 검증한다.

기존 결정이나 사용자가 편집한 Patch·Comment·메모를 덮어쓰거나 같은 요청의 양식을 중복하지 않는다. 재요청·재시작에서 원안 diff를 다시 채워 편집본을 지우지 않는다. 누락되거나 잘못 작성된 사용자 결정을 자동 보정해 승인으로 인정하지 않는다.

### Revision 제거와 재승인

새 결정은 Revision 없이 pending·approve·reject·replace 모두 유효해야 한다. 승인 요청의 식별과 승인 당시 기준·제안·확정 patch의 결합은 프로그램이 관리한다. 기존 Revision이 있는 문서와 SQLite 승인 기록은 그대로 읽고 보존하며, 기존 Revision을 수정하거나 지워야 새 형식을 사용할 수 있도록 요구하지 않는다. 새 형식에 기존 Revision의 필수 입력 조건을 적용하지 않는다.

무효화된 승인에는 사용자가 Revision 값을 바꾸는 대신, 프로그램이 기존 제안과 승인 이력을 보존하고 새 고유 요청 ID의 Proposal·pending User Decision 쌍을 준비해 재승인을 요청한다. 새 요청의 기준과 patch를 확인한 뒤 작성한 결정만 유효하며, 이전 요청의 승인·Comment 변경·단순 재시작으로 차단을 해제하지 않는다. 같은 재승인 요청은 중단·재시작해도 다시 생성하거나 ID를 바꾸지 않는다. 무효화된 과거 쌍은 감사 기록으로 보존하고 새 요청의 적용 배치에 포함하지 않는다.

### 직접 편집과 모델의 작성 도움

사용자는 Decision·Comment·필요한 Patch·사전 승인 Baseline을 직접 작성하거나, 특정 제안에 대한 자신의 결정을 모델에게 전달해 문서 작성을 요청할 수 있다. 사용자의 명시적 요청을 받아 양식을 작성하는 모델의 도움은 허용하며, 모델이 편집했다는 이유만으로 유효한 결정을 거부하지 않는다. 모델은 사용자가 정한 결정과 범위만 기록하고, 포괄적 허락을 임의의 approve·replace 결정으로 바꾸지 않는다.

Loop Manager의 자동 Planner·Worker·Reviewer가 실행 중 자기 변경을 스스로 승인하거나 기존 사용자 결정을 바꾸는 것은 계속 금지한다. 사용자 주도의 문서 편집과 자동 실행 중 Agent의 변경을 구분하고, 기존 작업 범위·원격 게시·대상·Scope·patch·Baseline 검증을 유지한다. Comment에만 적힌 승인 문구나 모델의 제안은 Decision을 대신하지 않는다.

이 Requirement는 [R-066](%5BR-066%5D%20AGENTS%20변경%20사전%20승인.md)과 현행 `docs/PLANS.md`의 사용자 작성 Revision 필수·Revision 변경을 통한 재승인 규칙을 위 요청 식별 방식으로 대체하고, pending·approve·reject의 Patch를 반드시 비우는 규칙은 위 원안 표시·확정 patch 선택 규칙으로 대체한다. Agent 작성 결정 금지는 사용자가 명시적으로 요청한 문서 작성 도움을 제외한 자동 실행의 자기 승인 금지로 명확히 한다. 나머지 승인·검증·게시 규칙은 유지한다.

### 명확한 검증과 실패 처리

다음 입력은 승인하거나 일부만 적용하지 않고, 해당 ID와 위치 및 원인을 구분해 알려야 한다.

- Proposal 또는 대응 User Decision 누락, 제안·결정 순서 위반, Proposal과 User Decision의 ID 불일치.
- Proposal ID 또는 User Decision ID 중복, 잘못된 ID, Decision Log 밖의 guidance 제목.
- Decision·Patch 또는 diff 블록 누락, Comment를 포함한 필드 중복, 허용되지 않은 결정 값, 결정 종류에 맞지 않는 Patch·Baseline 및 기존 Revision 형식 위반.
- 한 결정에 종료 마커가 둘 이상 있거나, 대응 결정 밖에 종료 마커가 있는 경우. 코드 블록 안의 문자열은 이 개수에 포함하지 않는다.

종료 마커·Revision·Comment 부재 자체는 누락 오류가 아니다. 불완전한 본문은 마커나 EOF를 만났다는 이유로 유효해지지 않는다. 잘못된 입력에서는 기존 작업물·승인 기록·세션과 차단을 보존하고, 사용자가 직접 또는 모델의 도움으로 같은 결정란을 수정한 뒤 기존 검증·재개 절차를 따른다.

이번 변경은 공통 파서와 승인 등록·무효화·재승인·알림, Worker 요청 양식, `docs/PLANS.md`의 작성 규칙 및 pending·승인·거절·대체·사전 승인 예시에 함께 반영한다. 문서 예시와 실제 생성 양식은 같은 파서로 처리할 수 있어야 한다.

## Acceptance Criteria

- 종료 마커 직전까지의 완전한 pending·approve·reject·replace 결정을 읽고, 뒤의 일반 문단·목록·제목·`Decision:` 같은 메모는 결정 값이나 patch에 포함하지 않는다.
- 동일한 결정에 메모만 추가·변경해도 결정 identity와 확정 patch가 같으며, pending은 계속 대기한다. 유효한 사용자 결정과 메모를 함께 작성하면 같은 작업 공간·세션으로 정상 재개할 수 있다.
- 필드 옆의 작성 안내 주석을 허용하고 필드 값이나 patch로 읽지 않는다. 주석 유무와 Comment의 생략·빈 값·내용 변경만으로 결정 identity·patch·승인 상태가 바뀌지 않으며, Comment 중복은 명확히 거부한다.
- 마커 없는 기존 결정은 EOF, 다음 guidance 제목 또는 다음 최상위 절에서 정상 종료한다. LF·CRLF 및 마지막 개행 유무를 지원하며, 기존 승인 기록과 Baseline 없는 결정의 호환성을 유지한다.
- diff·코드 블록 안의 마커 문자열은 본문을 자르지 않는다. 마커를 결정 중간에 넣어 필수 필드나 닫는 diff fence가 빠지면 명확한 형식 오류로 거부한다.
- 복수 Proposal·User Decision 쌍과 쌍 사이의 마커 뒤 메모를 처리하고, 메모 뒤의 추가 guidance 제목도 누락·중복·ID 대응 검증을 통과해야 한다.
- 제안·결정·필드·diff 블록 누락, ID·필드·마커 중복, ID 불일치와 잘못된 마커 위치 각각을 구분해 진단한다. 실패한 배치의 일부 결정만 승인하거나 AGENTS.md를 변경하지 않는다.
- 결정을 요청할 때 실제 ExecPlan에 동일 ID의 완전한 pending 양식·Decision 값 안내 주석·빈 백틱의 Comment·Proposal의 정확한 전체 diff·종료 마커가 이미 존재하고 Revision이나 별도 선택 메모 영역을 생성하지 않는다. 재요청·재시작으로 중복하거나 사용자가 편집한 diff·결정을 덮어쓰지 않는다.
- 실제 생성 양식과 문서 예시에 Decision의 허용 값, Comment 백틱 안의 선택 입력, Patch 수정본을 적용할 때의 replace 결정을 필드 옆 주석으로 표시한다. 사전 승인 예시는 Baseline의 전체 SHA 조건도 주석으로 안내하며, 별도 사용자 작성 안내문·표·선택 메모 영역을 붙이지 않는다.
- 미리 채운 diff와 안내가 포함된 미작성 양식은 pending으로 파싱되고, 안내의 결정 값·Baseline·diff 설명을 사용자 결정으로 읽지 않는다. 사용자나 사용자의 명시적 요청을 받은 모델이 안내된 위치만 편집한 approve·reject·replace 양식도 정상 파싱된다.
- 원안 diff를 그대로 둔 approve는 원안을 적용하고 reject는 변경하지 않는다. pending에서 diff만 편집하면 계속 대기한다. 편집한 전체 diff를 replace로 확정하면 그 diff만 적용하며, approve에 원안과 다른 diff가 있으면 명확한 오류와 replace 안내를 제공한다.
- 기존 빈 Patch의 pending·approve·reject와 원안을 표시한 같은 Decision은 동일한 승인 의미로 처리한다. 표시용 원안의 추가·제거만으로 기존 승인 identity·SQLite 기록을 무효화하거나 같은 승인을 중복 적용하지 않는다.
- Revision 없는 새 형식을 일반 승인 대기와 사전 승인에서 수용하며 기존 Revision 문서·승인 identity·SQLite 기록을 보존한다. 재시작으로 같은 승인을 중복 등록·적용하지 않는다.
- 승인 기준이나 실제 결과가 달라져 승인이 무효화되면 새 요청 ID의 pending 양식을 제공한다. 이전 승인이나 Comment 변경은 재승인으로 인정하지 않고, 새 요청에 기록된 명시적 결정은 Revision 입력 없이 재승인된다. 재시작해도 동일 요청과 차단·감사 이력을 유지한다.
- 사용자 주도의 모델 작성 도움은 허용하고, 자동 실행 중 Agent의 자기 승인·기존 결정 변경·포괄적 허락만으로 결정 생성은 계속 거부한다. Comment에만 작성된 승인 문구는 적용하지 않는다.
- Worker 양식과 `docs/PLANS.md`의 예시·편집 안내를 함께 갱신하고, 일반 승인 대기와 게시된 사전 승인 모두에서 위 경계·메모·실패·호환성 조건을 확인하는 회귀 테스트를 제공한다.
- 주석·Comment·메모·경계 변경으로 Proposal·결정의 변조, 대상·Scope 위반 또는 승인 검사를 우회할 수 없다. 유효한 결정 뒤에는 기존 targeted test·전체 verify·Review·PR·사용자 Merge 절차를 유지한다.

## Product Impact

- 사용자 결정은 명시적 종료 마커로 일반 메모와 분리할 수 있으며, 마커 없는 기존 문서도 계속 지원한다.
- 승인 요청에는 작성 위치와 필수·조건부 입력을 표시한 양식을 미리 제공하고, 모호하거나 불완전한 결정은 적용 없이 구체적인 오류로 안내한다.
- 사용자는 Revision을 입력하지 않고 결정과 선택 Comment를 작성한다. 모델의 도움을 받을 수 있으며, 재승인은 프로그램이 준비한 새 요청에서 명시적으로 기록한다.
- 결정란에는 제안 diff를 항상 미리 표시하고, 원안 승인·거절은 Decision만 변경하면 된다. 수정본 적용은 Patch를 편집하고 replace로 명시한다.

## Non-Goals

- AGENTS 변경 이외의 모든 사용자 결정에 새 승인 형식을 도입하지 않는다.
- 기존 ExecPlan·archive 문서를 일괄 변환하거나 사용자 결정을 자동 작성·보정하지 않는다.
- 승인 종류, 승인 권한, SQLite 상태 모델과 PR Merge 정책을 변경하지 않는다.

## Open Questions

- 없음
