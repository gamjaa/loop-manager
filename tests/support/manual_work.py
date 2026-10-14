"""수동 작업 테스트의 명령 입력과 프로젝트 실행 경계."""
import argparse
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application.manual_work import ManualWorkService
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from tests.support.workspace import WorkspaceFixture


def completion(plan_id='G-012', **values):
    return argparse.Namespace(plan_id=plan_id,skip_verify=False,skip_review=False,
                              expected_head=None,skip_reason=None,**values)


def generated(title, source='docs/requirements/Active.md', depends_on=(), identity='draft-1'):
    return {'id':identity,'title':title,'sources':[source],'depends_on':list(depends_on),
        'complexity':'medium','risk':'medium','priority':'normal',
        'body':'\n\n'.join(f'## {section}\n\nConcrete requirement and implementation checks.'
            for section in ('Purpose','Acceptance Criteria','Implementation','Validation','Progress','Decision Log'))}


class ManualWorkFixture(WorkspaceFixture):
    def fixture(self, directory):
        repo,state,connection,plan,sha,branch,worktree=self._ready_execplan(directory)
        from tests.support.git import local_github_origin
        origin = local_github_origin(self, repo)
        connection.execute('UPDATE projects SET registered=1, origin=? WHERE repo=?',(origin, str(repo)))
        connection.commit()
        config_path=Path(directory)/'operational-config.ini'
        config_path.write_text('[loop]\nmax_workers=3\nverify_timeout_seconds=600\n'
            'verify_parallelism=auto\n\n[telegram]\ntoken=fixture-secret\nchat_id=fixture-chat\n',
            encoding='utf-8')
        effects=replace(loop_manager._manual_work_effects(),validate_repository=mock.Mock(),
                        run_project=mock.Mock(),generate_plans=mock.Mock(),operation_print=mock.Mock(),
                        resolve_profile=mock.Mock(return_value=ResolvedProfile('fixture-reviewer','medium')))
        service=ManualWorkService(effects,connection,state,repo,config_path)
        # The driver is not executed here; stage tests enter the real project pipeline.
        return service,branch,worktree
