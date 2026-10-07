"""수동 작업 테스트의 명령 입력과 프로젝트 실행 경계."""
import argparse
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application.manual_work import ManualWorkService
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
        connection.execute('UPDATE projects SET registered=1 WHERE repo=?',(str(repo),))
        connection.commit()
        effects=replace(loop_manager._manual_work_effects(),validate_repository=mock.Mock(),
                        run_project=mock.Mock(),generate_plans=mock.Mock(),operation_print=mock.Mock())
        service=ManualWorkService(effects,connection,state,repo,Path('unused.ini'))
        # The driver is not executed here; stage tests enter the real project pipeline.
        return service,branch,worktree
