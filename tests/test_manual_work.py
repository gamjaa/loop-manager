import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application.manual_work import ManualWorkError, validate_options
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
from tests.support.manual_work import completion, generated
from tests.support.git import git_command, isolated_git_test
from types import SimpleNamespace


from tests.support.manual_work_case import ManualWorkCase


@isolated_git_test
class ManualWorkTest(ManualWorkCase):
    def test_options_require_exact_head_and_reason_independently(self):
        for values in ({'skip_verify':True},{'skip_review':True},{'skip_reason':'external'},
                       {'expected_head':'main'},{'expected_head':'a'*12}):
            args=completion()
            for key,value in values.items():
                setattr(args,key,value)
            with self.assertRaises(ValueError):
                validate_options(args)
        args=completion()
        args.skip_review=True
        args.expected_head='a'*40
        args.skip_reason='external review'
        validate_options(args)


    def test_legacy_manual_requests_keep_unknown_stage_inputs_null(self):
        connection=sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory=sqlite3.Row
        connection.execute("CREATE TABLE manual_work_requests (request_id TEXT PRIMARY KEY,repo TEXT NOT NULL,"
            "group_id TEXT NOT NULL,source_head TEXT NOT NULL,base_sha TEXT NOT NULL,input_token TEXT NOT NULL,"
            "skip_verify INTEGER NOT NULL,skip_review INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL)")
        connection.execute("INSERT INTO manual_work_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
            ('old-request','repo','group','head','base','candidate',1,1,'external','2026-10-07T00:00:00+00:00'))
        ManualWorkRepository(connection)
        request=connection.execute('SELECT * FROM manual_work_requests WHERE request_id=?',('old-request',)).fetchone()
        self.assertIsNone(request['verify_input_token'])
        self.assertIsNone(request['review_input_token'])


    def test_start_releases_projection_and_does_not_call_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            with mock.patch('loop_manager.run_agent') as agent:
                self.assertEqual(service.start('G-012'),0)
            agent.assert_not_called()
            row=service.runtime.get(service.repo,'G-012')
            self.assertEqual(row.status,'MANUAL_WORKING')
            self.assertTrue(row.ever_started)
            self.assertTrue(worktree.is_dir())
            self.assertEqual(git_command(service.repo_path,'ls-remote','--heads','origin',branch).split()[0],
                             git_command(worktree,'rev-parse','HEAD'))
            self.assertIsNone(service.runtime.projected(service.repo))
            decision=loop_manager.catalog_schedule_decision(service.connection,service.repo)
            self.assertTrue(decision.has_waiting)
            self.assertIsNone(decision.runnable_ready)
            self.assertEqual(service.start('G-012'),0)


    def test_correctable_block_can_be_handed_off_without_releasing_safety_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            service.runtime.block_plan(service.repo,'G-012',reason_code='verify_retry_exhausted',
                                       evidence='actual failure',release_fingerprint=None)
            service.runtime.hydrate(service.repo,'G-012')
            service.connection.commit()
            self.assertEqual(service.start('G-012'),0)
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'MANUAL_WORKING')
            self.assertIsNone(service.runtime.projected(service.repo))
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            service.runtime.block_plan(service.repo,'G-012',reason_code='guidance_approval_required',
                                       evidence='human decision needed',release_fingerprint=None)
            service.connection.commit()
            with self.assertRaisesRegex(ManualWorkError,'인계할 수 없는 차단'):
                service.start('G-012')
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'PLAN_BLOCKED')


    def test_user_verify_does_not_verify_a_past_worker_correction_turn(self):
        from loop_manager_internal.storage import review_correction_state
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            review_correction_state.save(service.connection,service.repo,'G-012',
                {'candidate':{'level':1,'completed':True,'verified_head':None}})
            service.connection.commit()
            (worktree/'src/app.py').write_text('VALUE = 88\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','New user implementation')
            git_command(worktree,'push','origin',branch)
            service.complete(completion())
            result,verify,agent=self.step(service,review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS',stop='publish')
            self.assertEqual(verify.call_count,1)
            self.assertEqual(agent.call_count,1)
            self.assertEqual(agent.call_args.args[4],'reviewer')
            self.assertIsNone(review_correction_state.load(service.connection,service.repo,'G-012')['candidate']['verified_head'])


    def test_dependency_flag_without_actual_merge_cannot_create_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.connection.execute("UPDATE exec_plans SET depends_on='[\"G-001\"]' WHERE plan_id='G-012'")
            service.connection.commit()
            text=(service.repo_path/'docs/plans/[G-012] Recovery Safety.md').read_text()
            text=text.replace('depends_on: []','depends_on:\n  - G-001')
            service.effects.git(service.repo_path,'show-ref','--verify','--quiet',f'refs/heads/{branch}',check=False)
            with mock.patch.object(service,'document',return_value=text),self.assertRaisesRegex(ManualWorkError,'G-001'):
                service.start('G-012')
            self.assertFalse(worktree.exists())
            self.assertIsNone(service.store.for_plan(service.repo,'G-012'))


    def test_complete_accepts_while_an_independent_plan_owns_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            service.connection.execute("UPDATE projects SET phase='PLANNING' WHERE repo=?",(service.repo,))
            service.connection.commit()
            self.assertEqual(service.complete(completion()),1)
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'MANUAL_PENDING')
            group=service.store.for_plan(service.repo,'G-012')
            self.assertEqual(group['state'],'AUTOMATIC')
            self.assertIsNotNone(service.store.request(group))
            self.assertEqual(service.connection.execute('SELECT phase FROM projects').fetchone()[0],'PLANNING')
            service.effects.run_project.assert_not_called()


    def test_manual_wait_defers_new_maintenance_and_keeps_independent_ready_running(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            from tests.support.catalog import write_execplan
            from tests.support.git import commit_and_push
            from loop_manager_internal.storage import planning_input_state, maintenance_state
            path=write_execplan(service.repo_path,'G-020','Independent')
            commit_and_push(service.repo_path,'Add independent plan')
            revision=git_command(service.repo_path,'rev-parse','origin/main')
            plans=[(path.relative_to(service.repo_path).as_posix(),loop_manager._parse_execplan(service.repo_path,path))]
            loop_manager.register_execplans(service.connection,service.repo,service.repo_path,plans,revision,
                                           loop_manager.planning_input_fingerprint(service.repo_path,'main'))
            planning_input_state.record_general(service.connection,service.repo,revision,
                                               loop_manager.plan_declarations(service.repo_path,revision))
            service.connection.commit()
            service.start('G-012')
            result,_,agent=self.step(service,stop='run')
            agent.assert_not_called()
            self.assertEqual(service.runtime.active_row(service.repo)['plan_id'],'G-020')
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'MANUAL_WORKING')
            self.assertEqual(maintenance_state.MaintenanceRepository(service.connection).get(service.repo).phase,
                             maintenance_state.MaintenancePhase.IDLE)


    def test_manual_overlay_does_not_restore_a_user_deleted_base_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            git_command(service.repo_path,'rm','docs/plans/[G-012] Recovery Safety.md')
            git_command(service.repo_path,'commit','-m','User deletes goal')
            git_command(service.repo_path,'push','origin','main')
            git_command(service.repo_path,'fetch','origin','main')
            loop_manager.reconcile_execplan_catalog(service.connection,service.repo_path,service.repo,'main')
            from loop_manager_internal.recovery import scoped_blocking
            self.assertIn('G-012',scoped_blocking.catalog_held_plan_ids(service.connection,service.repo))
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'MANUAL_WORKING')
            self.assertTrue(worktree.exists())


    def test_user_base_goal_revision_replaces_stale_manual_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 12\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','User implementation')
            git_command(worktree,'push','origin',branch)
            path=service.repo_path/'docs/plans/[G-012] Recovery Safety.md'
            text=path.read_text()+'\n## Acceptance Criteria\n\nLatest user acceptance applies.\n'
            path.write_text(text)
            git_command(service.repo_path,'add',path.relative_to(service.repo_path).as_posix())
            git_command(service.repo_path,'commit','-m','User revises acceptance')
            git_command(service.repo_path,'push','origin','main')
            args=completion()
            args.skip_verify=args.skip_review=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='External checks on the old candidate'
            service.complete(args)
            result,verify,agent=self.step(service,stop='publish')
            verify.assert_not_called()
            agent.assert_not_called()
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'VERIFYING')
            result,verify,agent=self.step(service,review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS',stop='publish')
            self.assertEqual(verify.call_count,1)
            self.assertEqual(agent.call_count,1)
            self.assertEqual(agent.call_args.args[4],'reviewer')
            self.assertIn('Latest user acceptance applies.',agent.call_args.args[5])
            self.assertIn('Latest user acceptance applies.',(worktree/'docs/plans/[G-012] Recovery Safety.md').read_text())


    def test_expected_head_mismatch_does_not_accept(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            args=completion()
            args.expected_head='f'*40
            with self.assertRaisesRegex(ManualWorkError,'HEAD 불일치'):
                service.complete(args)
            self.assertEqual(service.store.for_plan(service.repo,'G-012')['state'],'MANUAL_WORKING')


    def test_verify_failure_resumes_worker_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 3\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',branch)
            service.complete(completion())
            self.step(service,verify_result=(False,'test failure'),stop='run')
            row=service.runtime.get(service.repo,'G-012')
            self.assertIn(row.status,{'IMPLEMENTING','MANUAL_PENDING'})
            self.assertEqual(service.store.for_plan(service.repo,'G-012')['state'],'AUTOMATIC')
            self.assertIsNotNone(service.connection.execute("SELECT 1 FROM manual_work_results WHERE stage='verify' AND result='FAIL'").fetchone())
            service.resume()
            self.assertEqual(service.runtime.active_row(service.repo)['status'],'IMPLEMENTING')


    def test_shared_review_failure_rotates_only_one_active_member(self):
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_verify_handler.common_guidance_report',
            return_value=''))
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.submitted(directory)
            first,second=json.loads(group['members'])
            self.step(service,review=f'MANUAL_PLAN_RESULT: {first} PASS\nMANUAL_PLAN_RESULT: {second} FAIL\n'
                      'ISSUE_ID: src/app.py::value::incorrect\nVERDICT: FAIL',stop='run')
            service.resume()
            active=service.runtime.active_row(service.repo)
            self.assertEqual(active['plan_id'],second)
            self.assertEqual(active['status'],'IMPLEMENTING')
            self.assertEqual(service.runtime.get(service.repo,first).status,'MANUAL_PENDING')
            from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
            loop_manager.validate_execplan_workspace_identity(service.state_dir,service.repo,
                manual_workspace_owner(service.connection,service.repo,second),group['branch'],Path(group['worktree']))


    def test_internal_dependency_rejected_before_owned_branch_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            git_command(service.repo_path,'branch','source')
            git_command(service.repo_path,'push','origin','source')
            service.effects.generate_plans.return_value=json.dumps({'plans':[
                generated('First',identity='a'),generated('Second',depends_on=('a',),identity='b')]})
            args=completion()
            args.branch='source'
            args.requirement=['docs/requirements/Active.md']
            self.assertEqual(service.submit(args),1)
            group=service.store.for_source(service.repo,'source',args.requirement)
            self.assertIsNone(group['branch'])
            self.assertIn('별도 branch/PR',group['last_error'])


    def test_manual_candidate_reaches_pr_without_fabricating_worker_or_test_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 2\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',branch)
            head=git_command(worktree,'rev-parse','HEAD')
            args=completion()
            args.skip_verify=args.skip_review=True
            args.expected_head=head
            args.skip_reason='외부 verify와 Review를 진행함'
            service.complete(args)
            real_command=loop_manager.command
            def command(arguments, *positional, **keywords):
                if list(arguments)[:3]==['gh','pr','create']:
                    return SimpleNamespace(returncode=0,stdout='https://example/pull/1\n')
                return real_command(arguments,*positional,**keywords)
            with mock.patch('loop_manager.validate_repository',return_value=service.repo_path), \
                 mock.patch('loop_manager.ensure_safe_base_worktree'), \
                 mock.patch('loop_manager.pr_for_branch',return_value=None), \
                 mock.patch('loop_manager.immutable_github_blob_url',return_value='https://example/blob'), \
                 mock.patch('loop_manager.command',side_effect=command), \
                 mock.patch('loop_manager._agent_profile',return_value=ResolvedProfile('fixture-reviewer','medium')), \
                 mock.patch('loop_manager.verify') as verify, mock.patch('loop_manager.run_agent') as agent:
                result=loop_manager._run_project_step(service.connection,service.state_dir,service.repo,
                    service.config_path,'main',False,registered_only=True)
            self.assertEqual(result,0)
            verify.assert_not_called()
            agent.assert_not_called()
            row=service.runtime.get(service.repo,'G-012')
            self.assertEqual(row.status,'PR_OPEN')
            self.assertIsNone(row.worker_result)
            results=service.connection.execute("SELECT stage,result FROM manual_work_results WHERE result!='NOT_RUN'").fetchall()
            self.assertEqual({tuple(row) for row in results},
                             {('verify','SKIPPED_BY_USER'),('review','SKIPPED_BY_USER')})
            from loop_manager_internal.recovery.manual_work_recovery import revalidate_waiting
            pr={'headRefOid':row.pr_head_oid}
            base=git_command(service.repo_path,'rev-parse','origin/main')
            stored=service.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                (service.repo,'G-012')).fetchone()
            kwargs={'config_path':service.config_path,'provider':service.project['provider'],
                    'resolve_profile':service.effects.resolve_profile}
            self.assertFalse(revalidate_waiting(service.connection,service.repo,stored,pr,base,**kwargs))
            service.config_path.write_text(service.config_path.read_text().replace(
                'verify_timeout_seconds=600','verify_timeout_seconds=601'),encoding='utf-8')
            self.assertTrue(revalidate_waiting(service.connection,service.repo,stored,pr,base,**kwargs))
            self.assertEqual(service.runtime.get(service.repo,'G-012').status,'MANUAL_PENDING')


    def test_manual_project_sets_configured_verify_parallelism(self):
        from loop_manager_internal.application.manual_work import ManualProjectDriverEffects, run_manual_project
        from loop_manager_internal.config.managed_config import fast_mode_settings
        from loop_manager_internal.verification.coordination import settings as verify_settings

        config=SimpleNamespace(verify_timeout_seconds=432,verify_parallelism=2,
                               refactoring_review_interval=7)
        observed={}
        def run_project_result(*args,**kwargs):
            observed['settings']=verify_settings.get()
            observed['fast_mode']=fast_mode_settings.get()
            observed['args']=args
            observed['kwargs']=kwargs
            return 'completed'

        effects=ManualProjectDriverEffects(lambda _path:config,run_project_result)
        coordinator=mock.Mock()
        coordinator.__enter__=mock.Mock(return_value=coordinator)
        coordinator.__exit__=mock.Mock(return_value=False)
        with mock.patch('loop_manager_internal.verification.coordination.VerifyCoordinator',
                        return_value=coordinator):
            result=run_manual_project(effects,None,Path('/state'),'repo',Path('/config'),'main',False)

        self.assertEqual(result,'completed')
        self.assertEqual(observed['settings'].cap,2)
        self.assertEqual(observed['settings'].state_dir,Path('/state'))
        self.assertIs(observed['fast_mode'],config)
        self.assertEqual(observed['kwargs']['verify_timeout_seconds'],432)
        self.assertEqual(observed['kwargs']['refactoring_review_interval'],7)
        self.assertIsNone(verify_settings.get())
        self.assertIsNone(fast_mode_settings.get())
