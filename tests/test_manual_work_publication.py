import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application.manual_work import ManualWorkError
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from tests.support.manual_work import completion
from tests.support.git import git_command, isolated_git_test
from types import SimpleNamespace


from tests.support.manual_work_case import ManualWorkCase


@isolated_git_test
class ManualWorkTest(ManualWorkCase):
    def test_external_merge_during_revalidation_is_observed_without_active_slot(self):
        self._assert_external_merge_during_revalidation(active=False)

    def test_external_merge_during_active_revalidation_precedes_verify(self):
        self._assert_external_merge_during_revalidation(active=True)

    def _assert_external_merge_during_revalidation(self, *, active):
        from loop_manager_internal.recovery.manual_work_recovery import revalidate_waiting
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_publication_handler.common_guidance_report',
            return_value=''))
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_verify_handler.common_guidance_report',
            return_value=''))
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.submitted(directory)
            members=json.loads(group['members'])
            result,_,_=self.step(service,review='\n'.join(
                [f'MANUAL_PLAN_RESULT: {member} PASS' for member in members]+['VERDICT: PASS']))
            self.assertEqual(result,0)
            stored=service.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                (service.repo,group['owner_id'])).fetchone()
            pr={'url':stored['pr_url'],'headRefName':group['branch'],'baseRefName':'main',
                'headRefOid':stored['pr_head_oid'],'state':'OPEN'}
            service.config_path.write_text(service.config_path.read_text().replace(
                'verify_timeout_seconds=600','verify_timeout_seconds=601'),encoding='utf-8')
            self.assertTrue(revalidate_waiting(service.connection,service.repo,stored,pr,
                git_command(service.repo_path,'rev-parse','origin/main'),config_path=service.config_path,
                provider=service.project['provider'],resolve_profile=service.effects.resolve_profile))
            self.assertTrue(all(service.runtime.get(service.repo,key).status=='MANUAL_PENDING' for key in members))
            if active:
                service.runtime.update_plan(service.repo,group['owner_id'],
                    plan_values={'status':'VERIFYING'},enqueue=False)
                service.runtime.hydrate(service.repo,group['owner_id'])
            service.connection.commit()
            def lookup(observation):
                return lambda _repo, branch, *args, **kwargs: (
                    observation if branch == group['branch'] else None)

            from loop_manager_internal.storage.execution_checkpoint_state import (
                ExecutionCheckpointRepository, pending_completion_plan_ids)
            checkpoints=ExecutionCheckpointRepository(service.connection)
            checkpoint=checkpoints.get(service.repo,group['owner_id'])
            # CLOSED/잘못된 Merge HEAD는 재검증 상태나 최종 게시 근거를 바꾸지 않는다.
            if not active:
                with mock.patch('loop_manager.pr_for_branch',side_effect=lookup({**pr,'state':'CLOSED'})):
                    loop_manager.observe_waiting_pull_requests(service.connection,service.repo_path,
                        service.state_dir,service.repo)
                self.assertTrue(all(service.runtime.get(service.repo,key).status=='MANUAL_PENDING' for key in members))
                with mock.patch('loop_manager.pr_for_branch',side_effect=lookup({
                        **pr,'state':'MERGED','mergedAt':'now','headRefOid':'foreign'})):
                    loop_manager.observe_waiting_pull_requests(service.connection,service.repo_path,
                        service.state_dir,service.repo)
                self.assertTrue(all(service.runtime.get(service.repo,key).status=='MANUAL_PENDING' for key in members))
                observed=checkpoints.get(service.repo,group['owner_id'])
                evidence=observed.snapshot['external_completion']
                self.assertTrue(evidence['db_pending'])
                self.assertIn('최종 게시 근거',evidence['error'])
                self.assertEqual(replace(observed,snapshot={k:v for k,v in observed.snapshot.items()
                    if k!='external_completion'} or None),checkpoint)
                self.assertNotIn('G-012',pending_completion_plan_ids(service.connection,service.repo))
            git_command(service.repo_path,'merge','--no-ff',group['branch'],'-m','External Merge')
            git_command(service.repo_path,'push','origin','main')
            merged={**pr,'state':'MERGED','mergedAt':'now'}
            with mock.patch('loop_manager.remove_cleanup_worktree',wraps=loop_manager.remove_cleanup_worktree) as cleanup:
                if active:
                    # Merge 복구 이후의 별도 READY/Planner 배정은 이 회귀의 경계 밖이다.
                    with mock.patch('loop_manager_internal.application.project_service.ProjectService._run_coordinators',
                                    return_value=0):
                        _,verify,agent=self.step(service,pr=lookup(merged))
                    verify.assert_not_called()
                    agent.assert_not_called()
                else:
                    with mock.patch('loop_manager.pr_for_branch',side_effect=lookup(merged)):
                        loop_manager.observe_waiting_pull_requests(service.connection,service.repo_path,
                            service.state_dir,service.repo)
                self.assertEqual(cleanup.call_count,1)
                self.assertTrue(all(service.runtime.get(service.repo,key).status=='MERGED' for key in members))
                self.assertEqual(service.store.get(service.repo,group['group_id'])['state'],'MERGED')
                with mock.patch('loop_manager.pr_for_branch',side_effect=lookup(merged)) as repeated_lookup:
                    loop_manager.observe_waiting_pull_requests(service.connection,service.repo_path,
                        service.state_dir,service.repo)
                    repeated_lookup.assert_called_once_with(service.repo_path,'loop-manager/G-012')
                self.assertFalse(checkpoints.get(service.repo,group['owner_id']).snapshot['external_completion']['db_pending'])
                self.assertEqual(cleanup.call_count,1)

    def test_publication_stage_tokens_are_absent_without_manual_request(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            self.assertIsNone(candidate.publication_stage_tokens(context))


    def test_start_rejects_remote_draft_and_saved_decision_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            from loop_manager_internal.storage import execplan_draft_state
            path=service.repo_path/'docs/plans/[G-012] Recovery Safety.md'
            text=path.read_text().replace('risk: low\n','risk: low\ndraft: true\n')
            text+=('\n## Acceptance Criteria\n\nPreserve recovery safety.\n'
                   '\n## Draft Decision\n\nQuestion: Which recovery policy should be used?\n'
                   'Facts: Both policies preserve user changes.\n'
                   'Options: A retries automatically; B waits for an explicit decision.\n'
                   'Impact: The choice changes recovery acceptance criteria.\n')
            path.write_text(text)
            git_command(service.repo_path,'add',path.relative_to(service.repo_path).as_posix())
            git_command(service.repo_path,'commit','-m','Publish user decision draft')
            git_command(service.repo_path,'push','origin','main')
            with self.assertRaisesRegex(ManualWorkError,'draft.*사용자 판단'):
                service.start('G-012')
            self.assertIsNone(service.store.for_plan(service.repo,'G-012'))
            self.assertFalse(worktree.exists())
            loop_manager.reconcile_execplan_catalog(service.connection,service.repo_path,service.repo,'main')
            self.assertIn('G-012',execplan_draft_state.pending_ids(service.connection,service.repo))
            path.write_text(text.replace('draft: true\n',''))
            git_command(service.repo_path,'add',path.relative_to(service.repo_path).as_posix())
            git_command(service.repo_path,'commit','-m','Remove flag without approving draft')
            git_command(service.repo_path,'push','origin','main')
            git_command(service.repo_path,'fetch','origin','main')
            loop_manager.reconcile_execplan_catalog(service.connection,service.repo_path,service.repo,'main')
            with self.assertRaisesRegex(ManualWorkError,'draft.*사용자 판단'):
                service.start('G-012')
            self.assertIsNone(service.store.for_plan(service.repo,'G-012'))
            self.assertFalse(worktree.exists())


    def test_explicit_draft_false_uses_latest_base_and_allows_manual_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            path=service.repo_path/'docs/plans/[G-012] Recovery Safety.md'
            path.write_text(path.read_text().replace('risk: low\n','risk: low\ndraft: false\n'))
            git_command(service.repo_path,'add',path.relative_to(service.repo_path).as_posix())
            git_command(service.repo_path,'commit','-m','Explicitly executable goal')
            git_command(service.repo_path,'push','origin','main')
            self.assertEqual(service.start('G-012'),0)
            self.assertEqual(git_command(worktree,'rev-parse','HEAD'),git_command(service.repo_path,'rev-parse','origin/main'))
            (worktree/'src/app.py').write_text('VALUE = 77\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',branch)
            args=completion()
            args.skip_verify=args.skip_review=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='External verification and review'
            service.complete(args)
            result,verify,agent=self.step(service)
            self.assertEqual(result,0)
            verify.assert_not_called()
            agent.assert_not_called()
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'PR_OPEN')


    def test_remote_user_push_fast_forwards_without_losing_local_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            user=Path(directory)/'user'
            git_command(service.repo_path,'worktree','add','--detach',str(user),branch)
            (user/'src/app.py').write_text('VALUE = 91\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','Remote implementation')
            incoming=git_command(user,'rev-parse','HEAD')
            git_command(user,'push','origin',f'HEAD:{branch}')
            service.complete(completion())
            result,verify,agent=self.step(service,review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS')
            self.assertEqual(result,0)
            self.assertEqual(git_command(worktree,'rev-parse','HEAD'),incoming)
            self.assertEqual((worktree/'src/app.py').read_text(),'VALUE = 91\n')
            self.assertEqual(git_command(user,'rev-parse','HEAD'),incoming)
            self.assertEqual(verify.call_count,1)
            self.assertEqual(agent.call_count,1)


    def test_shared_pr_is_published_closed_reopened_and_merged_as_one_group(self):
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_publication_handler.common_guidance_report',
            return_value=''))
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_verify_handler.common_guidance_report',
            return_value=''))
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.submitted(directory)
            members=json.loads(group['members'])
            from loop_manager_internal.agents import scope_validation
            with mock.patch.object(scope_validation, 'validate_agent_scope',
                                   wraps=scope_validation.validate_agent_scope) as scope_check:
                result,verify,agent=self.step(service,review='\n'.join(
                    [f'MANUAL_PLAN_RESULT: {member} PASS' for member in members]+['VERDICT: PASS']))
            worker_checks = [call for call in scope_check.call_args_list if call.args[0] == 'worker']
            self.assertTrue(worker_checks)
            self.assertEqual(worker_checks[0].args[2].head, group['prepared_head'])
            self.assertEqual(result,0)
            self.assertEqual(verify.call_count,1)
            self.assertEqual(agent.call_count,1)
            rows=[service.runtime.get(service.repo,key) for key in members]
            self.assertTrue(all(row.status=='PR_OPEN' for row in rows))
            self.assertEqual(len({row.pr_url for row in rows}),1)
            request_id=service.store.get(service.repo,group['group_id'])['request_id']
            repeat=completion()
            repeat.branch='source'
            repeat.requirement=['docs/requirements/Active.md']
            self.assertEqual(service.submit(repeat),0)
            self.assertEqual(service.store.get(service.repo,group['group_id'])['request_id'],request_id)
            first=service.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',(service.repo,members[0])).fetchone()
            loop_manager._block_closed_pull_request(service.connection,service.repo,first,first['pr_url'],first['pr_head_oid'])
            self.assertTrue(all(service.runtime.get(service.repo,key).status=='PLAN_BLOCKED' for key in members))
            reopened={'url':first['pr_url'],'headRefName':group['branch'],'baseRefName':'main',
                'headRefOid':first['pr_head_oid'],'state':'OPEN'}
            with mock.patch('loop_manager.pr_for_branch',return_value=reopened), \
                 mock.patch('loop_manager.classify_pull_request_mergeability',return_value='mergeable'), \
                 mock.patch('loop_manager._agent_profile',
                            return_value=ResolvedProfile('fixture-reviewer','medium')) as resolve_profile:
                loop_manager.observe_waiting_pull_requests(
                    service.connection,service.repo_path,service.state_dir,service.repo,
                    config_path=service.config_path,
                )
            resolve_profile.assert_called_once_with(
                service.config_path,service.project['provider'],'reviewer',None,Path(group['worktree']),
            )
            self.assertTrue(all(service.runtime.get(service.repo,key).status=='PR_OPEN' for key in members))
            pr={'url':first['pr_url'],'headRefName':group['branch'],'baseRefName':'main',
                'headRefOid':first['pr_head_oid'],'state':'MERGED','mergedAt':'now'}
            git_command(service.repo_path,'merge','--no-ff',group['branch'],'-m','User Merge')
            git_command(service.repo_path,'push','origin','main')
            git_command(service.repo_path,'fetch','origin','main')
            with mock.patch('loop_manager.pr_for_branch',return_value=pr), \
                 mock.patch('loop_manager.remove_cleanup_worktree',wraps=loop_manager.remove_cleanup_worktree) as cleanup:
                loop_manager.observe_waiting_pull_requests(service.connection,service.repo_path,service.state_dir,service.repo)
            self.assertEqual(cleanup.call_count,1)
            self.assertTrue(all(service.runtime.get(service.repo,key).status=='MERGED' for key in members))
            self.assertEqual(service.store.get(service.repo,group['group_id'])['state'],'MERGED')


    def test_shared_publication_rolls_back_every_member_on_failure(self):
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_verify_handler.common_guidance_report',
            return_value=''))
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.submitted(directory)
            first,second=json.loads(group['members'])
            self.step(service,review=f'MANUAL_PLAN_RESULT: {first} PASS\nMANUAL_PLAN_RESULT: {second} PASS\nVERDICT: PASS',stop='publish')
            service.runtime.update_plan(service.repo,first,plan_values={'status':'PUBLISHING'},enqueue=False)
            service.runtime.hydrate(service.repo,first)
            service.connection.commit()
            head=git_command(Path(group['worktree']),'rev-parse','HEAD')
            from loop_manager_internal.storage import runtime_state
            real_update=runtime_state.RuntimeStateRepository.update_plan
            def interrupted(repository,repo,plan_id,**kwargs):
                if plan_id==second:
                    raise RuntimeError('interrupted shared publication')
                return real_update(repository,repo,plan_id,**kwargs)
            with mock.patch.object(runtime_state.RuntimeStateRepository,'update_plan',new=interrupted):
                with self.assertRaisesRegex(RuntimeError,'interrupted'):
                    context=SimpleNamespace(worktree=Path(group['worktree']),base_branch='main')
                    stage_tokens=self.candidate(service,first).publication_stage_tokens(context)
                    service.runtime.open_pull_request(service.repo,pr_url='https://example/pull/1',
                        pr_head_oid=head,manual_stage_tokens=stage_tokens)
            self.assertEqual(service.runtime.get(service.repo,first).status,'PUBLISHING')
            self.assertEqual(service.runtime.get(service.repo,second).status,'MANUAL_PENDING')
            self.assertEqual(service.store.get(service.repo,group['group_id'])['state'],'AUTOMATIC')
            self.assertIsNone(service.connection.execute('SELECT 1 FROM manual_work_publications').fetchone())


    def test_dirty_manual_checkout_keeps_staged_untracked_and_remote_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            user=Path(directory)/'user'
            git_command(service.repo_path,'worktree','add','--detach',str(user),branch)
            (user/'src/app.py').write_text('VALUE = 7\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','Remote user implementation')
            head=git_command(user,'rev-parse','HEAD')
            git_command(user,'push','origin',f'HEAD:{branch}')
            (worktree/'src/app.py').write_text('VALUE = 8\n')
            git_command(worktree,'add','src/app.py')
            (worktree/'local-notes.txt').write_text('preserve this')
            service.complete(completion())
            before=git_command(worktree,'diff','--cached')
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(resolving_rebase=False,worktree=worktree,branch=branch)
            self.assertFalse(candidate.adopt_remote(context,head,None,None,mock.Mock(),mock.Mock()))
            self.assertEqual(git_command(worktree,'diff','--cached'),before)
            self.assertEqual((worktree/'local-notes.txt').read_text(),'preserve this')
            self.assertEqual(git_command(service.repo_path,'ls-remote','--heads','origin',branch).split()[0],head)


    def test_push_interruption_recovers_same_intent_and_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            real_git=service.effects.git
            def interrupted(target,*args,**kwargs):
                result=real_git(target,*args,**kwargs)
                if args[0]=='push':
                    raise KeyboardInterrupt('after push before confirmation')
                return result
            service.effects=replace(service.effects,git=interrupted)
            with self.assertRaises(KeyboardInterrupt):
                service.start('G-012')
            group=service.store.for_plan(service.repo,'G-012')
            self.assertEqual(group['state'],'STARTING')
            service.effects=replace(service.effects,git=real_git)
            self.assertEqual(service.start('G-012'),0)
            self.assertEqual(service.store.for_plan(service.repo,'G-012')['group_id'],group['group_id'])
            self.assertFalse(loop_manager.manager_git_journal.has_pending_project_mutation(service.state_dir,service.repo))
