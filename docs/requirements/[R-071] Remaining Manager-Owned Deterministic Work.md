# [R-071] 남은 규칙 기반 작업의 Manager 처리

## Context

Archive된 [R-063](archive/%5BR-063%5D%20Manager-Owned%20Deterministic%20Work%20and%20Minimal%20Model%20Use.md)의 규칙 기반 처리 목표 중 새 계획 충돌 교정, 유지보수 barrier 추가와 결과 형식 처리가 남아 있다. 관련 계획의 Merge만으로 이 범위가 구현됐다고 볼 수 없다. 확정된 ID·dependency·형식·객관적 사실을 모델에게 다시 작성시키면 불필요한 호출과 교정이 발생한다.

이 문서는 이후 명세로 대체되지 않은 잔여 목표만 활성 입력으로 보존한다. Requirement 완료 판단과 이동은 [R-068](%5BR-068%5D%20Planner%20Judgments%20and%20Manager-Owned%20Requirement%20Retirement.md)의 정책을 따른다.

## Requirement

- Manager는 아직 게시·등록하지 않은 새 ExecPlan batch의 ID·경로 충돌을 고유 ID·경로로 교정하고, 그 batch 안에서 대상이 확정된 구조적 dependency를 함께 갱신한다. 활성·archive·등록·취소·예약된 ID를 재사용하지 않는다. 기존 게시·등록·실행 계획과 사용자 변경은 보존한다.
- 서로 다른 목표가 같은 ID로 제시되거나 본문 참조의 대상을 확정할 수 없으면 기계적 교정으로 의미를 선택하지 않는다. 해당 모호함만 기존 Planner 판단 또는 사용자 결정 절차에 남긴다.
- 유지보수 Planner는 리팩터링 필요성·범위와 DAG를 판단한다. Manager가 검증된 DAG의 모든 말단을 활성 barrier mode의 적격 미착수·무소유 READY snapshot에 dependency로 추가한다. 기존 ID·경로·본문·다른 metadata와 dependency 순서는 보존한다. 사후 구조 변경에 맞춘 의미 있는 계획 개정은 기존 Planner 역할로 남긴다.
- Worker·Reviewer·Planner·모듈 검토의 실제 결과 경로에서 의미가 하나로 확정되는 표식 배치·구분자·공백 차이는 Manager가 정규화한다. 확정된 대상·ID·revision·manifest·digest·링크는 검증된 호출 입력으로 결합한다. 이 형식 처리만을 위한 모델 호출이나 구현·Review 실패 횟수 증가, Worker level 승격을 만들지 않는다.
- 상충하는 verdict, 명시적 대상 identity 불일치, 누락된 판단·issue·원본 인용·필수 검증 근거는 성공 결과로 보충하지 않는다. 실제로 부족한 의미 판단은 기존 해당 역할의 교정 절차로 처리한다.
- 기존 증분 코드맵과 읽기 전용 조회를 재사용한다. 남아 있는 빈 파일·공백 chunk의 객관적 사실 설명과 이미 계산된 관계·manifest·digest·링크의 재작성만을 위한 모델 호출을 제거한다. 미지원 형식·파싱 실패·동적 또는 미확인 관계는 확정된 사실로 만들지 않는다. 빈 파일을 포함하는 의미 있는 모듈 검토는 유지한다.
- 자동 편집은 허용된 관리 작업 공간과 최신 snapshot에서 수행한다. 원격·snapshot·소유권이 바뀌거나 cycle·대상 이탈이 발견되면 게시를 보류하고 작업물을 보존한다. 중단 뒤 실제 파일·Git 결과와 저장 상태를 대조해 같은 ID 배정·dependency 추가·게시를 중복 적용하지 않는다.
- 기존 fast·targeted·full 유지보수, 성공 모듈 결과 재사용, 필요한 일반 계획·최종 문서 전문 확인·Review·사용자 승인과 게시 검증을 유지한다. 자동 변경은 해당 게시 절차의 실제 diff와 검증·검토 대상에 포함한다.

## Acceptance Criteria

- 게시 전 새 batch의 ID·경로 충돌과 확정된 내부 dependency가 추가 Planner 호출 없이 교정된다. 기존·취소·예약 ID와 사용자 변경은 보존되며, 경쟁 게시에서도 중복 ID가 등록되지 않는다.
- 모호한 본문 참조와 서로 다른 목표의 충돌은 임의로 해소되지 않고 필요한 의미 판단으로 남는다.
- 검증된 리팩터링 DAG의 모든 말단이 적격 READY snapshot에 Manager 코드로 반영된다. 기존 dependency 순서·본문·metadata가 보존되고, 소유된 계획·cycle·범위 밖 편집은 거부된다.
- 의미가 명확한 결과 형식 차이는 추가 모델 호출 없이 처리된다. 상충 verdict·누락·identity 불일치·허위 근거는 성공으로 바뀌지 않으며 실제 내용 교정이 유지된다.
- 빈 파일·공백 chunk의 사실 설명과 확정된 정형 정보 재작성만을 위한 모델 호출은 없다. 필요한 모듈 의미 검토와 불확실한 분석의 보수적 처리는 유지된다.
- 재시작·중복 처리·원격 경쟁에서 기계적 편집이 중복 적용되지 않고, 기존 작업물·사용자 결정·독립 차단과 최신 게시 gate가 보존된다.
- 실제 모델을 호출하지 않는 검증으로 생략된 호출과 필요한 호출의 유지, 실제 자동 diff의 검증·Review 포함 여부를 확인할 수 있다.

## Product Impact

- 확정된 ID·dependency·형식·객관적 사실은 Manager가 처리하고 의미 판단은 기존 Agent 역할에 남긴다.
- 규칙 기반 처리 때문에 작업물 보존·필수 검증·Review·사용자 판단을 약화하지 않는다.

## Non-Goals

- R-063의 마지막 구현 PR에서 Requirement를 archive하던 조건을 복원하지 않는다.
- 기존 계획의 목표·ID를 변경하거나 AGENTS의 Agent 쓰기 권한을 확대하지 않는다.
- 종전 파일·관계·종합 평가 단계를 새 기본 유지보수 흐름으로 복원하지 않는다.
- 새 Agent·외부 서비스·병렬 실행을 추가하지 않는다.
- 기존 활성 Requirement가 다루는 세션 feedback 축소·권한·draft 정책을 중복 구현하지 않는다.

## Open Questions

- 없음
