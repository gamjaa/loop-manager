# [R-088] 로컬 작업 데이터 누락의 문서 fallback

## Context

환경 전환이나 로컬 상태 데이터 누락으로 작업 행이 없으면, 저장소에 선행 ExecPlan이 있어도 dependency 조회가 실패한다. 활성·archive 문서로 필요한 작업 데이터를 복구해 처리를 이어가야 한다.

## Requirement

Manager는 로컬 DB에 필요한 ExecPlan 행이 없을 때 같은 프로젝트의 활성·archive 문서를 fallback으로 사용한다. 활성 문서는 기본 READY 작업으로 등록하고, archive 문서는 추가 PR Merge 조회나 과거 게시 본문 검증 없이 MERGED 작업으로 받아들인다. archive 문서를 신뢰한 복구 근거는 실제 PR 관측과 구분한다.

기존 실행 상태·소유권·session·실패·게시 감사는 누락 데이터 복구로 초기화하거나 덮어쓰지 않는다. 문서의 형식·ID 고유성·source와 dependency 관계 검증은 유지하며, 활성·archive 어디에도 없는 작업은 누락으로 보고한다. 조회 전용 명령은 DB를 복구하거나 원격을 갱신하지 않는다.

## Acceptance Criteria

- 새 DB와 이미 초기화된 DB 모두 활성 문서의 누락 행을 복구하고, archive 선행 작업을 MERGED로 받아 dependency 처리를 이어간다.
- archive fallback은 브랜치명이나 GitHub 조회 가능 여부에 의존하지 않으며 추가 병합·과거 본문 검증을 호출하지 않는다.
- archive 선행 작업의 선행 관계도 같은 문서 fallback으로 처리한다. 파일이 양쪽에 없거나 ID가 중복되거나 선언이 무효하면 이를 성공으로 처리하지 않는다.
- 반복 동기화와 재시작에서 복구한 상태와 문서 기반 완료 근거를 유지하며, 기존 행의 실행·실패·소유권 이력은 보존한다.
- 원격 revision 경쟁이나 DB 반영 중단에서는 기존 transaction 경계에 따라 부분 복구를 남기지 않는다.

## Non-Goals

- 문서로 Agent session이나 유실된 실행 로그를 추정해 복원하지 않는다.
- 기존 DB 행의 미완료 상태를 archive 파일의 존재만으로 완료로 변경하지 않는다.
