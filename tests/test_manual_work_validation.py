import json
import tempfile
from unittest import mock

import loop_manager
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.execution.manual_candidate import ManualCandidate
from tests.support.manual_work import completion
from tests.support.git import git_command, isolated_git_test
from types import SimpleNamespace


from tests.support.manual_work_case import ManualWorkCase


@isolated_git_test
class ManualWorkTest(ManualWorkCase):
    def test_existing_pr_recovery_observes_manual_stage_settings(self):
        from loop_manager_internal.runtime.contracts import PublicationSafetyError
        for scenario in ('unchanged','changed','unreadable'):
            with self.subTest(settings=scenario), tempfile.TemporaryDirectory() as directory:
                service,_branch,_worktree,head,base,project,pull_request = (
                    self._manual_recovery_candidate(directory)
                )
                if scenario=='changed':
                    service.config_path.write_text(service.config_path.read_text().replace(
                        'verify_timeout_seconds=600','verify_timeout_seconds=601'),encoding='utf-8')
                elif scenario=='unreadable':
                    service.config_path.write_text('[loop\ninvalid config',encoding='utf-8')
                with mock.patch('loop_manager._agent_profile',
                                return_value=ResolvedProfile('fixture-reviewer','medium')):
                    if scenario=='unchanged':
                        ports=loop_manager._ExecutionLifecyclePorts(
                            service.connection,service.runtime,repo=service.repo,
                            repo_path=service.repo_path,state_dir=service.state_dir,
                            base='main',base_sha=base,provider=service.project['provider'],
                            config_path=service.config_path,newly_activated=False,
                        )
                        self.assertEqual(ports.effects.publication.reconcile_existing_pull_request(
                            service.repo_path,service.state_dir,service.repo,project,pull_request),0)
                        self.assertEqual(service.runtime.get(service.repo,'G-012').status,'PR_OPEN')
                        publication=service.connection.execute(
                            'SELECT head FROM manual_work_publications WHERE repo=?',
                            (service.repo,),
                        ).fetchone()
                        self.assertEqual(publication['head'],head)
                    elif scenario=='changed':
                        result=loop_manager.reconcile_existing_pull_request(
                            service.connection,service.repo_path,service.state_dir,service.repo,
                            project,pull_request,config_path=service.config_path)
                        self.assertEqual(result,loop_manager.PROJECT_STEP_RETRY)
                        self.assertEqual(service.runtime.get(service.repo,'G-012').status,'VERIFYING')
                        self.assertEqual(service.store.for_plan(service.repo,'G-012')['state'],'AUTOMATIC')
                        self.assertEqual(service.runtime.get(service.repo,'G-012').pr_url,pull_request['url'])
                        self.assertEqual(service.runtime.get(service.repo,'G-012').pr_head_oid,head)
                        self.assertIsNone(service.connection.execute(
                            'SELECT 1 FROM manual_work_publications WHERE repo=?',(service.repo,),
                        ).fetchone())
                        with mock.patch(
                                'loop_manager_internal.execution.execution_publication_handler.refresh_common_guidance_pr'):
                            result,verify,agent=self.step(
                                service,pr=pull_request,
                                review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS',
                            )
                        self.assertEqual(result,0)
                        self.assertEqual(verify.call_count,1)
                        self.assertEqual(agent.call_count,1)
                        self.assertEqual(service.runtime.get(service.repo,'G-012').status,'PR_OPEN')
                        publication=service.connection.execute(
                            'SELECT head,evidence FROM manual_work_publications WHERE repo=?',
                            (service.repo,),
                        ).fetchone()
                        self.assertEqual(publication['head'],head)
                        self.assertEqual({row['result'] for row in json.loads(publication['evidence'])},
                                         {'PASS'})
                    else:
                        with self.assertRaises(PublicationSafetyError):
                            loop_manager.reconcile_existing_pull_request(
                                service.connection,service.repo_path,service.state_dir,service.repo,
                                project,pull_request,config_path=service.config_path)
                        self.assertEqual(service.runtime.get(service.repo,'G-012').status,'PUBLISHING')
                        self.assertIsNone(service.connection.execute(
                            'SELECT 1 FROM manual_work_publications WHERE repo=?',(service.repo,),
                        ).fetchone())


    def test_confirmed_push_recovery_observes_manual_stage_settings(self):
        for scenario in ('unchanged','changed','unreadable'):
            with self.subTest(settings=scenario), tempfile.TemporaryDirectory() as directory:
                service,_branch,_worktree,head,_base,project,pull_request = (
                    self._manual_recovery_candidate(directory,pushed_head=True)
                )
                if scenario=='changed':
                    service.config_path.write_text(service.config_path.read_text().replace(
                        'verify_timeout_seconds=600','verify_timeout_seconds=601'),encoding='utf-8')
                    checkpoint_repository=loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(
                        service.connection)
                    checkpoint=checkpoint_repository.get(service.repo,'G-012')
                    checkpoint_repository.record(service.repo,'G-012',
                        expected_generation=checkpoint.generation,operation='push',expected_head=head,
                        target_base_oid=_base,observed_remote_oid=_base,
                        snapshot={'remote_before':_base})
                elif scenario=='unreadable':
                    service.config_path.write_text('[loop\ninvalid config',encoding='utf-8')
                with mock.patch('loop_manager._agent_profile',
                                return_value=ResolvedProfile('fixture-reviewer','medium')):
                    result=loop_manager.reconcile_existing_pull_request(
                        service.connection,service.repo_path,service.state_dir,service.repo,
                        project,pull_request,config_path=service.config_path)
                if scenario=='unchanged':
                    self.assertEqual(result,0)
                    self.assertEqual(service.runtime.get(service.repo,'G-012').status,'PR_OPEN')
                    publication=service.connection.execute(
                        'SELECT head FROM manual_work_publications WHERE repo=?',
                        (service.repo,),
                    ).fetchone()
                    self.assertEqual(publication['head'],head)
                elif scenario=='changed':
                    self.assertEqual(result,loop_manager.PROJECT_STEP_RETRY)
                    selected=service.runtime.get(service.repo,'G-012')
                    self.assertEqual(selected.status,'VERIFYING')
                    self.assertEqual(selected.pr_url,pull_request['url'])
                    self.assertEqual(selected.pr_head_oid,head)
                    self.assertEqual(selected.publication_remote_oid,head)
                    self.assertEqual(selected.pr_conflict_recovery,0)
                    checkpoint=loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(
                        service.connection).get(service.repo,'G-012')
                    self.assertIsNone(checkpoint.operation)
                    self.assertEqual(checkpoint.observed_remote_oid,head)
                    self.assertIsNone(service.connection.execute(
                        'SELECT 1 FROM manual_work_publications WHERE repo=?',(service.repo,),
                    ).fetchone())
                    with mock.patch(
                            'loop_manager_internal.execution.execution_publication_handler.refresh_common_guidance_pr'):
                        result,verify,agent=self.step(
                            service,pr=pull_request,
                            review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS',
                        )
                    self.assertEqual(result,0)
                    self.assertEqual(verify.call_count,1)
                    self.assertEqual(agent.call_count,1)
                    self.assertEqual(service.runtime.get(service.repo,'G-012').status,'PR_OPEN')
                    publication=service.connection.execute(
                        'SELECT head,evidence FROM manual_work_publications WHERE repo=?',
                        (service.repo,),
                    ).fetchone()
                    self.assertEqual(publication['head'],head)
                    self.assertEqual({row['result'] for row in json.loads(publication['evidence'])},
                                     {'PASS'})
                else:
                    self.assertEqual(result,1)
                    self.assertNotEqual(service.runtime.get(service.repo,'G-012').status,'PR_OPEN')
                    self.assertIsNone(service.connection.execute(
                        'SELECT 1 FROM manual_work_publications WHERE repo=?',(service.repo,),
                    ).fetchone())


    def test_default_completion_invokes_verify_and_read_only_review_without_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.config_path.write_text(service.config_path.read_text().replace(
                'verify_timeout_seconds=600','verify_timeout_seconds=741').replace(
                'verify_parallelism=auto','verify_parallelism=3'),encoding='utf-8')
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 2\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',branch)
            service.complete(completion())
            composed=[]
            with mock.patch('loop_manager.validate_repository',return_value=service.repo_path), \
                 mock.patch('loop_manager.ensure_safe_base_worktree'), \
                 mock.patch('loop_manager.pr_for_branch',return_value=None), \
                 mock.patch('loop_manager.verify',return_value=(True,'private raw verify output')) as verify, \
                 mock.patch('loop_manager.run_agent',return_value=(0,'MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS')) as agent, \
                 mock.patch('loop_manager._agent_profile',return_value=ResolvedProfile('fixture-reviewer','medium')), \
                 mock.patch('loop_manager._ExecutionLifecyclePorts',wraps=loop_manager._ExecutionLifecyclePorts) as ports:
                # Keep this phase test local: stop at the publishing boundary.
                effects_ports=ports._mock_wraps
                def composition(*args,**kwargs):
                    handler=effects_ports(*args,**kwargs)
                    composed.append(handler)
                    handler.publish=mock.Mock(return_value=loop_manager.ExecutionOutcome(
                        loop_manager.ExecutionOutcomeKind.STABLE_WAIT,detail='publication boundary'))
                    return handler
                ports.side_effect=composition
                from loop_manager_internal.verification.coordination import execution_settings
                with execution_settings(mock.Mock(),3,service.state_dir):
                    loop_manager._run_project_step(service.connection,service.state_dir,service.repo,service.config_path,
                                                   'main',False,registered_only=True,
                                                   verify_timeout_seconds=741)
            self.assertEqual(verify.call_count,1)
            self.assertEqual(agent.call_count,1)
            manual=composed[0].effects.state.manual
            self.assertEqual(manual.verify_timeout_seconds,741)
            self.assertEqual(manual.verify_parallelism,3)
            self.assertEqual(agent.call_args.args[4],'reviewer')
            self.assertTrue(agent.call_args.kwargs['read_only'])
            self.assertIsNone(service.runtime.get(service.repo,'G-012').worker_result)
            reviewer_prompt = agent.call_args.args[5]
            self.assertIn('Worker targeted unit test는 실행되지 않았고 외부 targeted test 결과도',
                          reviewer_prompt)
            self.assertIn('Manager 확인: 전체 verify PASS', reviewer_prompt)
            self.assertIn('수동 단계 결과 (로그 본문 생략)', reviewer_prompt)
            self.assertIn('G-012 verify: PASS @', reviewer_prompt)
            self.assertNotIn('private raw verify output', reviewer_prompt)
            self.assertIn('MANUAL_EXTERNAL_TARGETED_TESTS_UNCONFIRMED',
                          self.candidate(service,'G-012')
                          .publication('',{'id':'G-012','title':'Test'},mock.Mock()).test_evidence)


    def test_changed_verify_settings_run_default_verify_without_resubmission(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 2\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',service.store.for_plan(service.repo,'G-012')['branch'])
            args=completion()
            args.skip_verify=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='External verify'
            service.complete(args)
            service.config_path.write_text(service.config_path.read_text().replace(
                'verify_timeout_seconds=600','verify_timeout_seconds=900'),encoding='utf-8')
            result,verify,_agent=self.step(service,review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS',
                                          stop='publish')
            self.assertEqual(result,0)
            verify.assert_called_once()
            receipts=service.connection.execute("SELECT detail FROM manual_work_results "
                "WHERE group_id=? AND stage='verify' AND result='NOT_RUN'",
                (service.store.for_plan(service.repo,'G-012')['group_id'],)).fetchall()
            self.assertTrue(any('verify 설정' in receipt['detail'] for receipt in receipts))


    def test_changed_reviewer_profile_runs_default_review_without_resubmission(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 2\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',service.store.for_plan(service.repo,'G-012')['branch'])
            args=completion()
            args.skip_review=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='External review'
            service.complete(args)
            result,_verify,agent=self.step(service,
                review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS',
                profile=ResolvedProfile('changed-reviewer','high'),stop='publish')
            self.assertEqual(result,0)
            self.assertEqual(agent.call_count,1)
            self.assertEqual(agent.call_args.kwargs['resolved_profile'],ResolvedProfile('changed-reviewer','high'))
            results={row[0] for row in service.connection.execute("SELECT result FROM manual_work_results "
                "WHERE stage='review'")}
            self.assertIn('NOT_RUN',results)
            self.assertIn('PASS',results)
            self.assertNotIn('SKIPPED_BY_USER',results)


    def test_stage_result_keeps_settings_observed_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            service.start('G-012')
            service.complete(completion())
            candidate=ManualCandidate(service.connection,service.repo,'G-012',service.effects.git,
                service.effects.git_bytes,config_path=service.config_path,provider=service.project['provider'],
                resolve_profile=service.effects.resolve_profile,verify_timeout_seconds=600,
                verify_parallelism='auto')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            snapshot=candidate.capture_stage(context,'verify')
            service.config_path.write_text(service.config_path.read_text().replace(
                'verify_timeout_seconds=600','verify_timeout_seconds=900'),encoding='utf-8')

            candidate.record(context,'verify',True,'verify PASS',snapshot=snapshot)

            receipt=service.connection.execute("SELECT input_token FROM manual_work_results "
                "WHERE stage='verify' AND result='PASS'").fetchone()
            self.assertEqual(receipt['input_token'],snapshot.input_token)
            current=candidate.inputs(context,'verify')
            self.assertNotEqual(current[5],snapshot.input_token)

            service.effects.resolve_profile.return_value=ResolvedProfile('captured-reviewer','low')
            review_snapshot=candidate.capture_stage(context,'review')
            service.effects.resolve_profile.return_value=ResolvedProfile('changed-reviewer','high')
            candidate.record(context,'review',True,'review PASS',snapshot=review_snapshot)
            review_receipt=service.connection.execute("SELECT input_token FROM manual_work_results "
                "WHERE stage='review' AND result='PASS'").fetchone()
            self.assertEqual(review_receipt['input_token'],review_snapshot.input_token)
            self.assertNotEqual(candidate.inputs(context,'review')[5],review_snapshot.input_token)


    def test_actual_failure_cannot_be_erased_with_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            service.complete(completion())
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            candidate.record(context,'verify',False,'actual failure')
            args=completion()
            args.skip_verify=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='external verification'
            service.complete(args)
            self.assertIsNone(candidate.waive(context,'verify'))
            self.assertEqual(service.connection.execute("SELECT count(*) FROM manual_work_results WHERE result='FAIL'").fetchone()[0],1)
