"""Read-only Planner contract for a user's explicitly selected remote intake."""
from dataclasses import dataclass
from collections.abc import Callable
import json
import hashlib

from loop_manager_internal.agents.agent_invocation import InvocationContext


@dataclass(frozen=True)
class ManualGenerationEffects:
    git: Callable
    run_agent: Callable
    prepare_worktree: Callable
    remove_worktree: Callable
    compact_execplan_catalog: Callable


def generate(effects, connection, repo, state_dir, config_path, provider, group, requirements):
    diff = effects.git(repo,'diff','--stat',group['base_sha'],group['source_head']).stdout
    catalog = effects.compact_execplan_catalog(connection, str(repo))
    guidance = {}
    for path in ('docs/PRODUCT.md','docs/ARCHITECTURE.md','docs/PLANS.md'):
        result=effects.git(repo,'show',f"{group['source_head']}:{path}",check=False)
        if result.returncode==0:
            guidance[path]=result.stdout
    implementation=effects.git(repo,'diff',group['base_sha'],group['source_head'],'--','.',':!docs/plans').stdout
    prompt=(
        '명시적으로 제출된 원격 R·코드를 추적할 완성된 ExecPlan 초안을 생성하세요. 읽기 전용입니다. '
        'checkout 파일을 수정하지 마세요. 입력은 아래 commit의 문서·diff이며 로컬 checkout은 제출 입력이 아닙니다. '
        'R의 목적·Acceptance Criteria를 보존하고 구현했다는 이유로 조건을 축소하지 마세요. '
        '기존 G와 중복되거나 제출 범위/실제 dependency가 불명확하면 plans=[]와 reason을 반환하세요. '
        '독립 G가 여러 개이면 각각 생성하세요. 내부 선행 Merge가 필요하면 depends_on을 그대로 명시하세요. '
        'Manager가 이를 거부하고 별도 순차 PR을 안내합니다. 임의로 dependency를 삭제하지 마세요. '
        'sources에는 제출한 R만 연결하세요. id는 초안 내 고유 식별자이며 외부 dependency는 실제 G ID입니다. '
        'body에는 PLANS의 Purpose, Acceptance Criteria, Implementation, Validation, Progress, Decision Log '
        '모든 절을 작성하세요. complexity와 risk는 각각 설계 난도와 실패 영향 및 복구 가능성의 구체적인 근거를 '
        '본문에 기록하세요. 파일 수·핵심 경로·Git/SQLite 사용만으로 high를 정하지 마세요. '
        'high 또는 critical이면 관련 실패·복구를 Acceptance Criteria, Validation, Review에 연결하세요. '
        '구체적인 완료 조건과 targeted unit test/Manager verify 조건도 작성하세요. '
        'PASS/상태/branch/session을 frontmatter에 기록하지 마세요. '
        '출력은 설명이나 markdown fence 없는 JSON 하나입니다: '
        '{"plans":[{"id":"draft-1","title":"Title","sources":["docs/requirements/R.md"],'
        '"depends_on":[],"complexity":"medium","risk":"medium","priority":"normal",'
        '"body":"## Purpose\\n..."}]}\n'
        +json.dumps({'source_head':group['source_head'],'base':group['base_sha'],
                    'requirements':requirements,'guidance':guidance,'catalog':json.loads(catalog),
                    'diff_stat':diff,'implementation_diff':implementation},ensure_ascii=False))
    mutation_id=hashlib.sha256((group['group_id']+group['source_head']).encode()).hexdigest()[:32]
    runtime=effects.prepare_worktree(repo,state_dir,mutation_id=mutation_id,
                                    commit_sha=group['source_head'],concurrent=True)
    result=effects.run_agent(provider,runtime.path,state_dir,config_path,'planner',prompt,read_only=True,
                            connection=connection,invocation=InvocationContext(str(repo),None,
                                task_id='manual-submit:'+group['group_id'],
                                purpose='general_planning', target='manual-submit:'+group['group_id']))
    # Interrupted/unknown Agents retain their runtime; only an observed return
    # reaches the clean, non-forced removal boundary.
    effects.remove_worktree(repo,state_dir,runtime)
    code,message=result
    if code:
        raise RuntimeError('수동 제출 G 생성 Planner가 실패했습니다. 제출 intent는 보존됐습니다.')
    return message.strip()
