import argparse
import contextlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application.manual_work import ManualWorkService, ManualWorkError, validate_options
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.execution.manual_candidate import ManualCandidate
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
from tests.support.manual_work import ManualWorkFixture, completion, generated
from tests.support.git import git_command
from tests.support.recovery_boundary import RecoveryBoundary
from types import SimpleNamespace


class ManualWorkTest(ManualWorkFixture, unittest.TestCase):
    def candidate(self, service, plan_id):
        return ManualCandidate(service.connection,service.repo,plan_id,service.effects.git,
            service.effects.git_bytes,config_path=service.config_path,provider=service.project['provider'],
            resolve_profile=service.effects.resolve_profile)

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

    def test_publication_stage_tokens_are_absent_without_manual_request(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            self.assertIsNone(candidate.publication_stage_tokens(context))

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

    def test_manual_waivers_follow_the_pinned_common_guidance_version(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.agents.agent_invocation import InvocationContext
        from tests.support.workspace import pin_common_guidance
        for pinned in (False,True):
            with self.subTest(pinned=pinned),tempfile.TemporaryDirectory() as directory:
                service,branch,worktree=self.fixture(directory)
                service.start('G-012')
                guidance=None
                if pinned:
                    guidance=pin_common_guidance(service.connection,InvocationContext(service.repo,'G-012'),
                                                 worktree,service.state_dir)
                pinned_hash = service.connection.execute(
                    'SELECT content_hash FROM common_guidance_versions WHERE task_id = ?',
                    (guidance.task_id,),
                ).fetchone()[0] if pinned else None
                args=completion()
                args.skip_verify=args.skip_review=True
                args.expected_head=git_command(worktree,'rev-parse','HEAD')
                args.skip_reason='External verification and review'
                service.complete(args)
                context=SimpleNamespace(worktree=worktree,base_branch='main')
                candidate=self.candidate(service,'G-012')
                self.assertIsNotNone(candidate.waive(context,'verify',record=False))
                with mock.patch.object(common_guidance,'runtime_content_hash',return_value='b'*64):
                    if pinned:
                        self.assertIsNotNone(candidate.waive(context,'verify',record=False))
                        self.assertIsNotNone(candidate.waive(context,'review',record=False))
                    else:
                        self.assertIsNone(candidate.waive(context,'verify',record=False))
                        self.assertIsNone(candidate.waive(context,'review',record=False))
                    definition=candidate.definition(context,None,candidate.group()['base_sha'])
                    self.assertEqual(definition.guidance_hash,pinned_hash if pinned else 'b'*64)

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

    def test_submit_registers_all_g_on_owned_branch_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            source='user-implementation'
            git_command(service.repo_path,'branch',source)
            git_command(service.repo_path,'push','origin',source)
            sha=git_command(service.repo_path,'rev-parse',source)
            service.effects.generate_plans.return_value=json.dumps({'plans':[
                generated('First',identity='a'),generated('Second',identity='b')]})
            args=completion()
            args.branch=source
            args.requirement=['docs/requirements/Active.md']
            self.assertEqual(service.submit(args),1)
            group=service.store.for_source(service.repo,source,args.requirement)
            self.assertEqual(len(json.loads(group['members'])),2)
            self.assertEqual(group['state'],'AUTOMATIC')
            self.assertEqual(git_command(service.repo_path,'rev-parse',source),sha)
            self.assertNotEqual(group['prepared_head'],sha)
            rows=service.connection.execute('SELECT status,branch FROM exec_plans WHERE plan_id IN (?,?)',
                tuple(json.loads(group['members']))).fetchall()
            self.assertEqual({row['status'] for row in rows},{'MANUAL_PENDING'})
            self.assertEqual({row['branch'] for row in rows},{group['branch']})
            self.assertFalse(any(row['status']=='READY' for row in rows))
            result=loop_manager.reconcile_execplan_catalog(service.connection,service.repo_path,service.repo,'main')
            self.assertTrue(result.usable)
            self.assertEqual(service.submit(args),1)
            self.assertEqual(service.effects.generate_plans.call_count,1)

    def submitted(self, directory, *, count=2, skip=False):
        service,_,_=self.fixture(directory)
        user=Path(directory)/'source-worktree'
        git_command(service.repo_path,'worktree','add','-b','source',str(user),'origin/main')
        (user/'src/app.py').write_text('VALUE = 2\n')
        git_command(user,'add','src/app.py')
        git_command(user,'commit','-m','User implementation')
        git_command(user,'push','origin','source')
        service.effects.generate_plans.return_value=json.dumps({'plans':[
            generated(f'Submitted {index}',identity=f'draft-{index}') for index in range(count)]})
        args=completion()
        args.branch='source'
        args.requirement=['docs/requirements/Active.md']
        if skip:
            args.skip_verify=args.skip_review=True
            args.expected_head=git_command(user,'rev-parse','HEAD')
            args.skip_reason='External checks'
        service.submit(args)
        return service,service.store.for_source(service.repo,'source',args.requirement),user

    def step(self, service, *, verify_result=(True,'verify PASS'), review='VERDICT: PASS',
             stop=None, pr=None, profile=None):
        real_command=loop_manager.command
        def command(arguments,*args,**kwargs):
            if list(arguments)[:3]==['gh','pr','create']:
                return SimpleNamespace(returncode=0,stdout='https://example/pull/1\n')
            return real_command(arguments,*args,**kwargs)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch('loop_manager.validate_repository',return_value=service.repo_path))
            stack.enter_context(mock.patch('loop_manager.ensure_safe_base_worktree'))
            stack.enter_context(mock.patch('loop_manager.pr_for_branch',return_value=pr))
            stack.enter_context(mock.patch('loop_manager.immutable_github_blob_url',return_value='https://example/blob'))
            stack.enter_context(mock.patch('loop_manager.command',side_effect=command))
            stack.enter_context(mock.patch('loop_manager._agent_profile',
                return_value=profile or ResolvedProfile('fixture-reviewer','medium')))
            verify=stack.enter_context(mock.patch('loop_manager.verify',return_value=verify_result))
            agent=stack.enter_context(mock.patch('loop_manager.run_agent',return_value=(0,review)))
            if stop:
                real_ports=loop_manager._ExecutionLifecyclePorts
                def ports(*args,**kwargs):
                    result=real_ports(*args,**kwargs)
                    setattr(result,stop,mock.Mock(return_value=loop_manager.ExecutionOutcome(
                        loop_manager.ExecutionOutcomeKind.STABLE_WAIT,detail='test boundary')))
                    return result
                stack.enter_context(mock.patch('loop_manager._ExecutionLifecyclePorts',side_effect=ports))
            result=loop_manager._run_project_step(service.connection,service.state_dir,service.repo,
                service.config_path,'main',False,registered_only=True)
        return result,verify,agent

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

    def test_generated_tracking_commit_invalidates_source_waiver(self):
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.submitted(directory,count=1,skip=True)
            owner=group['owner_id']
            context=SimpleNamespace(worktree=Path(group['worktree']),base_branch='main')
            candidate=self.candidate(service,owner)
            self.assertIsNone(candidate.waive(context,'verify'))
            result,verify,agent=self.step(service,review=f'MANUAL_PLAN_RESULT: {owner} PASS\nVERDICT: PASS')
            self.assertEqual(result,0)
            self.assertEqual(verify.call_count,1)
            self.assertEqual(agent.call_count,1)

    def test_shared_review_failure_rotates_only_one_active_member(self):
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

    def test_shared_pr_is_published_closed_reopened_and_merged_as_one_group(self):
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.submitted(directory)
            members=json.loads(group['members'])
            result,verify,agent=self.step(service,review='\n'.join(
                [f'MANUAL_PLAN_RESULT: {member} PASS' for member in members]+['VERDICT: PASS']))
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
            closed=service.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',(service.repo,members[0])).fetchone()
            loop_manager._resume_reopened_pull_request(service.connection,service.repo,closed,closed['pr_url'],closed['pr_head_oid'],recovery=False)
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

    def test_default_completion_invokes_verify_and_read_only_review_without_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 2\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',branch)
            service.complete(completion())
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
                    handler.publish=mock.Mock(return_value=loop_manager.ExecutionOutcome(
                        loop_manager.ExecutionOutcomeKind.STABLE_WAIT,detail='publication boundary'))
                    return handler
                ports.side_effect=composition
                loop_manager._run_project_step(service.connection,service.state_dir,service.repo,service.config_path,
                                               'main',False,registered_only=True)
            self.assertEqual(verify.call_count,1)
            self.assertEqual(agent.call_count,1)
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

    def test_reviewer_prompt_distinguishes_a_valid_user_verify_waiver(self):
        from types import SimpleNamespace
        from tests.support.execution import execution_fixture
        from loop_manager_internal.execution.manual_candidate import ManualCandidate, candidate_token
        from loop_manager_internal.agents.agent_invocation import AgentResult
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from loop_manager_internal.storage import verify_receipts
        from loop_manager_internal.verification.inputs import InputObservation

        fixture = execution_fixture(self)
        head, base = 'candidate-head', 'base'
        fixture.effects.workspace.git.side_effect = lambda _path, *args, **_kwargs: SimpleNamespace(
            stdout=(head if args == ('rev-parse', 'HEAD') else base), returncode=0,
        )
        context = fixture.review_context(verify_output='placeholder verify result')
        documents = [{
            'path': 'docs/plans/[G-047] Runtime.md',
            'text': '# Runtime',
            'plan': dict(fixture.plan),
        }]
        store = ManualWorkRepository(fixture.connection)
        group = store.create(
            fixture.repo, 'start', 'loop-manager/G-047', (), head, base,
            members=('G-047',), documents=documents,
        )
        store.update(group, state='AUTOMATIC', owner_id='G-047',
                     branch='loop-manager/G-047', worktree=str(context.worktree))
        group = store.for_plan(fixture.repo, 'G-047')

        def candidate_git(_path, *args, **_kwargs):
            value = head if args == ('rev-parse', 'HEAD') else base
            return SimpleNamespace(stdout=value, returncode=0)

        candidate = ManualCandidate(
            fixture.connection, fixture.repo, 'G-047', candidate_git, mock.Mock(),
        )
        candidate.documents = mock.Mock(return_value=documents)
        candidate_input = candidate_token(fixture.connection, fixture.repo, head, base, documents)
        store.accept(
            group, head=head, base=base, input_token=candidate_input,
            skip_verify=True, reason='password: waiver-secret-value',
        )
        fixture.state.manual = candidate
        waiver = candidate.waive(context, 'verify')
        self.assertIn('SKIPPED_BY_USER', waiver)

        definition = fixture.handler._definition_snapshot
        fixture.state.update_project(
            fixture.repo, phase='REVIEWING', worker_result=None,
            active_sha=base, validated_base_sha=base, verify_result=waiver,
            verify_token=definition.verify_token(base, head, waiver),
        )
        context = fixture.handler._context()
        observation = InputObservation('manual-waiver-review-inputs')
        verify_receipts.start_cycle(fixture.connection, fixture.repo, 'G-047')
        selection = verify_receipts.selected(fixture.connection, fixture.repo, 'G-047')
        receipt_id, selection = verify_receipts.record(
            fixture.connection, selection, head=head, observation=observation,
            kind='run', reason='user verify waiver',
        )
        verify_receipts.complete(
            fixture.connection, selection, receipt_id, passed=True,
            output=waiver, executed=False,
        )
        fixture.effects.validation.observe_verify_inputs.return_value = observation
        fixture.effects.agent.reviewer_prompt.side_effect = lambda *args, **kwargs: candidate.prompt(
            loop_manager.reviewer_prompt(*args, **kwargs), review=True,
        )
        review_result = 'MANUAL_PLAN_RESULT: G-047 PASS\nVERDICT: PASS'

        def reviewer(*_args, **kwargs):
            kwargs['on_session_started']('review-session')
            return AgentResult(0, review_result, session_id='review-session')

        fixture.effects.agent.run_agent.side_effect = reviewer
        fixture.handler._fetch_latest_base = lambda: base
        fixture.handler._post_effect_guard = lambda current: current
        fixture.handler._document_draft = lambda _context: None
        fixture.handler._document_review_prompt = lambda prompt, _draft: prompt

        outcome = fixture.handler.review(context)

        self.assertEqual(outcome.kind, loop_manager.ExecutionOutcomeKind.PHASE_ADVANCED)
        fixture.effects.validation.verify.assert_not_called()
        prompt = fixture.effects.agent.run_agent.call_args.args[5]
        self.assertIn('Worker targeted unit test는 실행되지 않았고 외부 targeted test 결과도', prompt)
        self.assertIn('유효한 사용자 verify 생략 (SKIPPED_BY_USER', prompt)
        self.assertIn('전체 verify PASS가 아닙니다', prompt)
        self.assertIn('<REDACTED>', prompt)
        self.assertNotIn('waiver-secret-value', prompt)
        self.assertNotIn('Manager 확인: 전체 verify PASS', prompt)
        self.assertEqual(fixture.connection.execute(
            "SELECT result FROM manual_work_results WHERE stage='verify'"
        ).fetchone()['result'], 'SKIPPED_BY_USER')
        self.assertEqual(fixture.connection.execute(
            "SELECT result FROM manual_work_results WHERE stage='review'"
        ).fetchone()['result'], 'PASS')

    def test_grant_expires_for_environment_new_sha_and_new_default_request(self):
        with tempfile.TemporaryDirectory() as directory:
            service,branch,worktree=self.fixture(directory)
            service.start('G-012')
            args=completion()
            args.skip_verify=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='external verify'
            service.complete(args)
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            self.assertIsNotNone(candidate.waive(context,'verify'))
            with mock.patch.dict('os.environ',{'R077_CHANGED_VERIFY_ENVIRONMENT':'changed'}):
                self.assertIsNone(candidate.waive(context,'verify'))
                self.assertFalse(candidate.valid_evidence(context,'verify','SKIPPED_BY_USER old'))
            service.complete(completion())
            self.assertIsNone(candidate.waive(context,'verify'))
            service.complete(args)
            git_command(worktree,'commit','--allow-empty','-m','Same tree new SHA')
            self.assertIsNone(candidate.waive(context,'verify'))

    def test_external_verify_settings_change_invalidates_the_bound_waiver(self):
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
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            self.assertIsNotNone(candidate.waive(context,'verify',record=False))
            service.config_path.write_text(service.config_path.read_text().replace(
                'verify_timeout_seconds=600','verify_timeout_seconds=900'),encoding='utf-8')
            self.assertIsNone(candidate.waive(context,'verify',record=False))
            notice=candidate.invalid_waiver_notice(context,'verify')
            self.assertIn('verify 설정',notice)
            self.assertNotIn('fixture-secret',notice)

    def test_reviewer_profile_change_invalidates_review_waiver(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            service.start('G-012')
            (worktree/'src/app.py').write_text('VALUE = 2\n')
            git_command(worktree,'add','src/app.py')
            git_command(worktree,'commit','-m','Manual implementation')
            git_command(worktree,'push','origin',service.store.for_plan(service.repo,'G-012')['branch'])
            args=completion()
            args.skip_verify=args.skip_review=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='External review'
            service.complete(args)
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            self.assertIsNotNone(candidate.waive(context,'verify',record=False))
            self.assertIsNotNone(candidate.waive(context,'review',record=False))
            service.effects.resolve_profile.return_value=ResolvedProfile('changed-reviewer','high')
            self.assertIsNotNone(candidate.waive(context,'verify',record=False))
            self.assertIsNone(candidate.waive(context,'review',record=False))
            notice=candidate.invalid_waiver_notice(context,'review')
            self.assertIn('Reviewer profile',notice)

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
            result,verify,_agent=self.step(service,review='MANUAL_PLAN_RESULT: G-012 PASS\nVERDICT: PASS')
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
                profile=ResolvedProfile('changed-reviewer','high'))
            self.assertEqual(result,0)
            self.assertEqual(agent.call_count,1)
            results={row[0] for row in service.connection.execute("SELECT result FROM manual_work_results "
                "WHERE stage='review'")}
            self.assertIn('NOT_RUN',results)
            self.assertIn('PASS',results)
            self.assertNotIn('SKIPPED_BY_USER',results)

    def test_unreadable_or_legacy_stage_settings_never_authorize_a_waiver(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            service.start('G-012')
            args=completion()
            args.skip_verify=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='External verify'
            service.complete(args)
            group=service.store.for_plan(service.repo,'G-012')
            with service.connection:
                service.connection.execute('UPDATE manual_work_requests SET verify_input_token=NULL WHERE request_id=?',
                                           (group['request_id'],))
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            self.assertIsNone(candidate.waive(context,'verify',record=False))
            service.config_path.unlink()
            self.assertIsNone(candidate.waive(context,'verify',record=False))
            self.assertFalse(candidate.valid_evidence(context,'verify','SKIPPED_BY_USER old'))
            values=[]
            for table in ('manual_work_requests','manual_work_results','manual_work_publications'):
                for row in service.connection.execute(f'SELECT * FROM {table}'):
                    values.extend(str(value) for value in row)
            self.assertNotIn('fixture-secret',' '.join(values))

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

    def test_repeated_source_head_preserves_members_source_and_owned_history(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            git_command(service.repo_path,'branch','source')
            git_command(service.repo_path,'push','origin','source')
            service.effects.generate_plans.return_value=json.dumps({'plans':[generated('Submitted')]})
            args=completion()
            args.branch='source'
            args.requirement=['docs/requirements/Active.md']
            service.submit(args)
            original=service.store.for_source(service.repo,'source',args.requirement)
            user=Path(directory)/'user'
            git_command(service.repo_path,'worktree','add',str(user),'source')
            (user/'src/app.py').write_text('VALUE = 42\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','Source followup')
            git_command(user,'push','origin','source')
            incoming=git_command(user,'rev-parse','HEAD')
            service.submit(args)
            self.assertIsNone(service.store.for_source(service.repo,'source',args.requirement)['pending_source_head'])
            self.assertEqual(service.store.for_source(service.repo,'source',args.requirement)['source_head'],incoming)
            service.runtime.transition_and_clear(service.repo,original['owner_id'],plan_values={'status':'MANUAL_PENDING'},enqueue=False)
            service.resume()
            current=service.store.for_source(service.repo,'source',args.requirement)
            self.assertEqual(current['group_id'],original['group_id'])
            self.assertEqual(current['members'],original['members'])
            self.assertIsNone(current['pending_source_head'])
            head=git_command(service.repo_path,'ls-remote','--heads','origin',original['branch']).split()[0]
            self.assertEqual(git_command(service.repo_path,'show',f'{head}:src/app.py'),'VALUE = 42')
            self.assertEqual(git_command(user,'rev-parse','HEAD'),incoming)
            self.assertEqual(service.effects.generate_plans.call_count,1)

    def pending_source(self, directory):
        service,group,user=self.submitted(directory)
        (user/'src/app.py').write_text('VALUE = 42\n')
        git_command(user,'add','src/app.py')
        git_command(user,'commit','-m','Repeated source')
        git_command(user,'push','origin','source')
        incoming=service.fetch('source')
        with service.connection:
            group=service.store.update(group,pending_source_head=incoming)
            service.store.accept(group,head=incoming,base=group['base_sha'],input_token='pending-source',
                                 skip_verify=True,skip_review=True,reason='External checks')
        return service,service.store.get(service.repo,group['group_id']),user

    def remote_followup(self, service, group, directory, *, conflict=False):
        other=Path(directory)/'other-device'
        git_command(service.repo_path,'worktree','add','--detach',str(other),f"origin/{group['branch']}")
        path='src/app.py' if conflict else 'followup.txt'
        (other/path).write_text('VALUE = 99\n' if conflict else 'Other device\n')
        git_command(other,'add',path)
        git_command(other,'commit','-m','Other device followup')
        git_command(other,'push','origin',f"HEAD:refs/heads/{group['branch']}")
        return git_command(other,'rev-parse','HEAD')

    @contextlib.contextmanager
    def interrupt_source_push(self):
        from loop_manager_internal.git import manager_git_journal as journal
        execute=journal.execute
        def interrupt(*arguments,**kwargs):
            if kwargs['kind']=='manual_branch_push':
                raise KeyboardInterrupt('after candidate before push intent')
            return execute(*arguments,**kwargs)
        with mock.patch.object(journal,'execute',side_effect=interrupt):
            yield

    def test_source_update_recovers_each_boundary_and_preserves_remote_followups(self):
        from loop_manager_internal.git import manager_git_journal as journal
        from loop_manager_internal.git.manual_source_update import update_source
        preparation=self.enterContext(RecoveryBoundary())
        directory=preparation.directory
        service,group,user=self.pending_source(directory)
        incoming=group['pending_source_head']
        request_id=group['request_id']
        before=group['prepared_head']
        real_git=service.effects.git
        worktree=Path(group['worktree'])
        (worktree/'staged.txt').write_text('Local staged work\n')
        git_command(worktree,'add','staged.txt')
        (worktree/'untracked.txt').write_text('Local untracked work\n')
        local_status=git_command(worktree,'status','--porcelain')
        effects=service.effects
        preparation.save(service.connection)
        for boundary in ('candidate','before_push','after_push','receipt','sqlite'):
            for followup in (False,True):
                with self.subTest(boundary=boundary,followup=followup):
                    preparation.restore(service.connection)
                    service.effects=effects
                    def interrupt(target,*arguments,**kwargs):
                        if boundary=='before_push' and arguments[0]=='push':
                            raise KeyboardInterrupt('before push')
                        result=real_git(target,*arguments,**kwargs)
                        if boundary=='after_push' and arguments[0]=='push':
                            raise KeyboardInterrupt('after push')
                        return result
                    with contextlib.ExitStack() as stack:
                        if boundary=='candidate':
                            stack.enter_context(self.interrupt_source_push())
                        elif boundary=='sqlite':
                            stack.enter_context(mock.patch.object(service.runtime,'update_plan',side_effect=KeyboardInterrupt))
                        elif boundary=='receipt':
                            write=journal._write_new
                            slot=journal._slot(service.state_dir,service.repo,'manual_branch_push',
                                               f"refs/heads/{group['branch']}")
                            def interrupt_receipt(path,value):
                                write(path,value)
                                if path.parent==slot and path.name.endswith('.result.json'):
                                    raise KeyboardInterrupt('after result before active removal')
                            stack.enter_context(mock.patch.object(journal,'_write_new',side_effect=interrupt_receipt))
                        else:
                            service.effects=replace(service.effects,git=interrupt)
                        with self.assertRaises(KeyboardInterrupt):
                            update_source(service,group)
                    service.effects=replace(service.effects,git=real_git)
                    saved=service.store.get(service.repo,group['group_id'])
                    candidate=saved['source_update_candidate']
                    self.assertEqual(saved['source_update_remote_oid'],before)
                    self.assertEqual(git_command(service.repo_path,'rev-list','--parents','-n','1',candidate).split(),
                                     [candidate,before,incoming])
                    if followup:
                        service.fetch(group['branch'])
                        later=self.remote_followup(service,group,directory)
                    spy=mock.Mock(wraps=real_git)
                    service.effects=replace(service.effects,git=spy)
                    current=update_source(service,saved)
                    pushes=[call.args for call in spy.call_args_list if call.args[1]=='push']
                    published=boundary in ('after_push','receipt','sqlite')
                    self.assertEqual(len(pushes),0 if published else 1)
                    head=service.fetch(group['branch'])
                    if followup:
                        git_command(service.repo_path,'merge-base','--is-ancestor',later,head)
                    git_command(service.repo_path,'merge-base','--is-ancestor',incoming,head)
                    git_command(service.repo_path,'merge-base','--is-ancestor',before,head)
                    if published:
                        git_command(service.repo_path,'merge-base','--is-ancestor',candidate,head)
                    elif followup:
                        self.assertNotEqual(head,candidate)
                        self.assertIn(journal.exact_branch_lease(group['branch'],later),pushes[0])
                    else:
                        self.assertIn(journal.exact_branch_lease(group['branch'],before),pushes[0])
                    self.assertEqual(git_command(service.repo_path,'show',f'{head}:src/app.py'),'VALUE = 42')
                    self.assertEqual(service.fetch('source'),incoming)
                    self.assertEqual(git_command(user,'rev-parse','HEAD'),incoming)
                    self.assertEqual(git_command(worktree,'status','--porcelain'),local_status)
                    self.assertEqual((worktree/'staged.txt').read_text(),'Local staged work\n')
                    self.assertEqual((worktree/'untracked.txt').read_text(),'Local untracked work\n')
                    self.assertEqual(current['members'],group['members'])
                    self.assertEqual(current['request_id'],request_id)
                    self.assertIsNone(current['source_update_candidate'])
                    self.assertIsNone(current['source_update_remote_oid'])
                    self.assertIsNone(current['pending_source_head'])
                    self.assertFalse(journal.has_pending_project_mutation(service.state_dir,service.repo))
                    self.assertEqual(service.effects.generate_plans.call_count,1)
                    spy.reset_mock()
                    update_source(service,current)
                    spy.assert_not_called()

    def test_source_update_descendant_equal_reinterruptions_and_rewrite(self):
        from loop_manager_internal.git import manager_git_journal as journal
        from loop_manager_internal.git.manual_source_update import update_source
        preparation=self.enterContext(RecoveryBoundary())
        directory=preparation.directory
        service,group,_=self.pending_source(directory)
        real_git=service.effects.git
        def after_push(target,*arguments,**kwargs):
            result=real_git(target,*arguments,**kwargs)
            if arguments[0]=='push':
                raise KeyboardInterrupt('after exact push')
            return result
        service.effects=replace(service.effects,git=after_push)
        with self.assertRaises(KeyboardInterrupt):
            update_source(service,group)
        service.effects=replace(service.effects,git=real_git)
        saved=service.store.get(service.repo,group['group_id'])
        service.fetch(group['branch'])
        later=self.remote_followup(service,group,directory)
        effects=service.effects
        remote_head=later
        preparation.save(service.connection)
        for boundary in ('before_result','after_result','sqlite','aborted_receipt','before_equal'):
            for rewrite in (False,True):
                with self.subTest(boundary=boundary,rewrite=rewrite):
                    preparation.restore(service.connection)
                    service.effects=effects
                    later=remote_head
                    slot=journal._slot(service.state_dir,service.repo,'manual_branch_push',
                                       f"refs/heads/{group['branch']}")
                    write=journal._write_new
                    def interrupt_equal(path,value):
                        equal=path.parent==slot and value.get('status')=='equal'
                        if equal and boundary=='before_result':
                            raise KeyboardInterrupt('before equal receipt')
                        write(path,value)
                        if equal and boundary=='after_result':
                            raise KeyboardInterrupt('after equal receipt')
                        if path.parent==slot and value.get('status')=='aborted' and boundary=='aborted_receipt':
                            raise KeyboardInterrupt('after retirement receipt')
                    execute=journal.execute
                    def interrupt_before_equal(*args,**kwargs):
                        if kwargs['kind']=='manual_branch_push' and kwargs['before']==kwargs['expected']:
                            raise KeyboardInterrupt('before equal intent')
                        return execute(*args,**kwargs)
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch.object(journal,'_write_new',side_effect=interrupt_equal))
                        if boundary=='before_equal':
                            stack.enter_context(mock.patch.object(journal,'execute',side_effect=interrupt_before_equal))
                        if boundary=='sqlite':
                            stack.enter_context(mock.patch.object(service.runtime,'update_plan',side_effect=KeyboardInterrupt))
                        with self.assertRaises(KeyboardInterrupt):
                            update_source(service,saved)
                    ref=f"refs/heads/{group['branch']}"
                    if rewrite:
                        tree=git_command(service.repo_path,'rev-parse',f"{group['prepared_head']}^{{tree}}")
                        later=git_command(service.repo_path,'commit-tree',tree,'-p',group['prepared_head'],
                                          '-m','Remove published candidate and descendant')
                        git_command(service.repo_path,'push','--force','origin',f'{later}:{ref}')
                    spy=mock.Mock(wraps=real_git)
                    service.effects=replace(service.effects,git=spy)
                    if rewrite:
                        with self.assertRaisesRegex(RuntimeError,'게시 완료한 source 후보'):
                            update_source(service,saved)
                        self.assertEqual(dict(service.store.get(service.repo,group['group_id'])),dict(saved))
                    else:
                        current=update_source(service,saved)
                        self.assertEqual(current['prepared_head'],later)
                        self.assertEqual(current['request_id'],group['request_id'])
                        self.assertEqual(current['members'],group['members'])
                        self.assertIsNone(current['pending_source_head'])
                        self.assertFalse(journal.has_pending_project_mutation(service.state_dir,service.repo))
                    self.assertFalse(any(call.args[1]=='push' for call in spy.call_args_list))
                    self.assertEqual(service.fetch(group['branch']),later)
                    self.assertEqual(service.fetch('source'),group['pending_source_head'])

    def test_saved_source_candidate_without_matching_basis_never_pushes(self):
        from loop_manager_internal.git.manual_source_update import update_source
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.pending_source(directory)
            with self.interrupt_source_push(),self.assertRaises(KeyboardInterrupt):
                update_source(service,group)
            saved=service.store.get(service.repo,group['group_id'])
            spy=mock.Mock(wraps=service.effects.git)
            service.effects=replace(service.effects,git=spy)
            for changes in ({'source_update_remote_oid':None},
                            {'source_update_remote_oid':group['source_head']},
                            {'pending_source_head':group['initial_source_head']}):
                with self.subTest(changes=changes),service.connection:
                    altered=service.store.update(saved,**changes)
                    with self.assertRaisesRegex(RuntimeError,'근거가 없습니다'):
                        update_source(service,altered)
                    self.assertEqual(service.fetch(group['branch']),group['prepared_head'])
                    self.assertEqual(altered['source_update_candidate'],saved['source_update_candidate'])
                    service.store.update(saved,source_update_remote_oid=saved['source_update_remote_oid'],
                                         pending_source_head=saved['pending_source_head'])
            self.assertFalse(any(call.args[1]=='push' for call in spy.call_args_list))

    def test_source_update_lease_race_retains_candidate_and_reintegrates_conflict(self):
        from loop_manager_internal.git import manager_git_journal as journal
        from loop_manager_internal.git.manual_source_update import update_source
        with tempfile.TemporaryDirectory() as directory:
            service,group,user=self.pending_source(directory)
            real_git=service.effects.git
            later=None
            def compete(target,*arguments,**kwargs):
                nonlocal later
                if arguments[0]=='push':
                    later=self.remote_followup(service,group,directory,conflict=True)
                return real_git(target,*arguments,**kwargs)
            service.effects=replace(service.effects,git=compete)
            with self.assertRaises(RuntimeError):
                update_source(service,group)
            service.effects=replace(service.effects,git=real_git)
            saved=service.store.get(service.repo,group['group_id'])
            self.assertEqual(saved['source_update_remote_oid'],group['prepared_head'])
            self.assertEqual(service.fetch(group['branch']),later)
            other=Path(directory)/'other-device'
            def compete_again(target,*arguments,**kwargs):
                nonlocal later
                if arguments[0]=='push':
                    (other/'second-followup.txt').write_text('Another user commit\n')
                    git_command(other,'add','second-followup.txt')
                    git_command(other,'commit','-m','Compete during recovery')
                    git_command(other,'push','origin',f"HEAD:refs/heads/{group['branch']}")
                    later=git_command(other,'rev-parse','HEAD')
                return real_git(target,*arguments,**kwargs)
            service.effects=replace(service.effects,git=compete_again)
            with self.assertRaises(RuntimeError):
                update_source(service,saved)
            service.effects=replace(service.effects,git=real_git)
            saved=service.store.get(service.repo,group['group_id'])
            self.assertEqual(service.fetch(group['branch']),later)
            members=json.loads(group['members'])
            service.runtime.block_plan(service.repo,members[1],reason_code='guidance_approval_required',
                evidence='Separate user decision',release_fingerprint=None)
            service.runtime.update_plan(service.repo,members[0],plan_values={
                'worker_session_id':'existing-session','verify_token':'old-verify','review_token':'old-review',
                'publication_token':'old-publication'},enqueue=False)
            service.connection.commit()
            current=update_source(service,saved)
            head=service.fetch(group['branch'])
            git_command(service.repo_path,'merge-base','--is-ancestor',later,head)
            git_command(service.repo_path,'merge-base','--is-ancestor',group['pending_source_head'],head)
            self.assertEqual(git_command(service.repo_path,'show',f'{head}:src/app.py'),'VALUE = 99')
            self.assertEqual(git_command(service.repo_path,'show',f'{head}:second-followup.txt'),'Another user commit')
            self.assertEqual(current['resume_phase'],'IMPLEMENTING')
            self.assertEqual(current['pending_source_head'],group['pending_source_head'])
            self.assertEqual(current['source_update_conflict'],1)
            self.assertEqual(current['request_id'],group['request_id'])
            owner=service.runtime.get(service.repo,members[0])
            self.assertEqual(owner.worker_session_id,'existing-session')
            self.assertIsNone(owner.verify_token)
            self.assertIsNone(owner.review_token)
            self.assertIsNone(owner.publication_token)
            self.assertEqual(service.runtime.get(service.repo,members[1]).status,'PLAN_BLOCKED')
            context=SimpleNamespace(worktree=Path(group['worktree']),base_branch='main')
            manual=ManualCandidate(service.connection,service.repo,members[0],real_git,service.effects.git_bytes,
                config_path=service.config_path,provider=service.project['provider'],
                resolve_profile=service.effects.resolve_profile)
            self.assertIn('VALUE = 42',manual.prompt('Correct source conflict'))
            self.assertIsNone(manual.waive(context,'verify',record=False))
            self.assertIsNone(manual.waive(context,'review',record=False))
            self.assertFalse(journal.has_pending_project_mutation(service.state_dir,service.repo))
            (user/'src/app.py').write_text('VALUE = 99\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','User resolves source conflict')
            git_command(user,'push','origin','source')
            args=completion(branch='source',requirement=json.loads(group['requirements']))
            service.submit(args)
            current=service.store.get(service.repo,group['group_id'])
            self.assertIsNone(current['pending_source_head'])
            self.assertEqual(current['source_update_conflict'],0)
            self.assertEqual(current['resume_phase'],'VERIFYING')
            self.assertEqual(service.runtime.get(service.repo,members[1]).status,'PLAN_BLOCKED')

    def test_new_source_submit_cannot_replace_an_unconfirmed_saved_candidate(self):
        from loop_manager_internal.git.manual_source_update import update_source
        with tempfile.TemporaryDirectory() as directory:
            service,group,user=self.pending_source(directory)
            with self.interrupt_source_push(),self.assertRaises(KeyboardInterrupt):
                update_source(service,group)
            saved=service.store.get(service.repo,group['group_id'])
            (user/'src/app.py').write_text('VALUE = 43\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','Next source request')
            git_command(user,'push','origin','source')
            args=completion(branch='source',requirement=json.loads(group['requirements']))
            with self.assertRaisesRegex(ManualWorkError,'이전 source 후보'):
                service.submit(args)
            self.assertEqual(dict(service.store.get(service.repo,group['group_id'])),dict(saved))

    def test_saved_source_candidate_keeps_deleted_or_rewritten_remote_untouched(self):
        from loop_manager_internal.git.manual_source_update import update_source
        for change in ('deleted','rewritten','published_removed'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                service,group,_=self.pending_source(directory)
                interruption=(mock.patch.object(service.runtime,'update_plan',side_effect=KeyboardInterrupt)
                              if change=='published_removed' else self.interrupt_source_push())
                with interruption,self.assertRaises(KeyboardInterrupt):
                    update_source(service,group)
                saved=service.store.get(service.repo,group['group_id'])
                ref=f"refs/heads/{group['branch']}"
                new='' if change=='deleted' else group['base_sha']
                if change=='published_removed':
                    tree=git_command(service.repo_path,'rev-parse',f"{group['prepared_head']}^{{tree}}")
                    new=git_command(service.repo_path,'commit-tree',tree,'-p',group['prepared_head'],
                                    '-m','User rewrites after published candidate')
                git_command(service.repo_path,'push','--force','origin',f'{new}:{ref}')
                spy=mock.Mock(wraps=service.effects.git)
                service.effects=replace(service.effects,git=spy)
                with self.assertRaises(RuntimeError):
                    update_source(service,saved)
                self.assertFalse(any(call.args[1]=='push' for call in spy.call_args_list))
                self.assertEqual(dict(service.store.get(service.repo,group['group_id'])),dict(saved))
                actual=git_command(service.repo_path,'ls-remote','--heads','origin',ref)
                self.assertEqual(actual.split()[0] if actual else '',new)
