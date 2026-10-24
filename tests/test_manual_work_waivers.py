import tempfile
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.execution.manual_candidate import ManualCandidate
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
from tests.support.manual_work import completion
from tests.support.git import git_command, isolated_git_test
from types import SimpleNamespace


from tests.support.manual_work_case import ManualWorkCase


@isolated_git_test
class ManualWorkTest(ManualWorkCase):
    def test_manual_waivers_follow_current_runtime_even_with_legacy_guidance(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.agents.agent_invocation import InvocationContext
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                service, branch, worktree = self.fixture(directory)
                service.start('G-012')
                if legacy:
                    identity = common_guidance.task_identity(
                        InvocationContext(service.repo, 'G-012'), 'worker', worktree)
                    GuidanceRepository(service.connection).save(
                        identity, service.repo, 'a' * 64, 'a' * 40, '/old-guidance.json')
                args = completion()
                args.skip_verify = args.skip_review = True
                args.expected_head = git_command(worktree, 'rev-parse', 'HEAD')
                args.skip_reason = 'External verification and review'
                service.complete(args)
                context = SimpleNamespace(worktree=worktree, base_branch='main')
                candidate = self.candidate(service, 'G-012')
                self.assertIsNotNone(candidate.waive(context, 'verify', record=False))
                self.assertIsNotNone(candidate.waive(context, 'review', record=False))
                with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='b' * 64):
                    self.assertIsNone(candidate.waive(context, 'verify', record=False))
                    self.assertIsNone(candidate.waive(context, 'review', record=False))
                    definition = candidate.definition(context, None, candidate.group()['base_sha'])
                    self.assertEqual(definition.guidance_hash, 'b' * 64)


    def test_generated_tracking_commit_invalidates_source_waiver(self):
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_publication_handler.common_guidance_report',
            return_value=''))
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_verify_handler.common_guidance_report',
            return_value=''))
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


    def test_reviewer_prompt_distinguishes_a_valid_user_verify_waiver(self):
        from types import SimpleNamespace
        from tests.support.execution import execution_fixture
        from loop_manager_internal.execution.manual_candidate import (
            MANUAL_TEST_EVIDENCE, ManualCandidate, candidate_token, stage_input_tokens,
        )
        from loop_manager_internal.agents.agent_invocation import AgentResult, ResolvedProfile
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
            if args == ('rev-parse', 'HEAD'):
                value = head
            elif args == ('rev-parse', 'refs/remotes/origin/main'):
                value = base
            else:
                raise AssertionError(f'unexpected manual candidate Git: {args}')
            return SimpleNamespace(stdout=value, returncode=0)

        config_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'operational-config.ini'
        config_path.write_text('[loop]\nverify_timeout_seconds=600\nverify_parallelism=auto\n', encoding='utf-8')
        profile = ResolvedProfile('fixture-reviewer', 'medium')
        resolve_profile = mock.Mock(return_value=profile)
        candidate = ManualCandidate(
            fixture.connection, fixture.repo, 'G-047', candidate_git, mock.Mock(),
            config_path=config_path, provider='codex', resolve_profile=resolve_profile,
        )
        candidate.documents = mock.Mock(return_value=documents)
        candidate_input = candidate_token(fixture.connection, fixture.repo, head, base, documents)
        verify_input_token, review_input_token = stage_input_tokens(
            config_path, 'codex', context.worktree, resolve_profile,
        )
        store.accept(
            group, head=head, base=base, input_token=candidate_input,
            verify_input_token=verify_input_token, review_input_token=review_input_token,
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

        with mock.patch('subprocess.Popen', side_effect=AssertionError(
                'unexpected manual candidate process')):
            outcome = fixture.handler.review(context)

        self.assertEqual(outcome.kind, loop_manager.ExecutionOutcomeKind.PHASE_ADVANCED)
        fixture.effects.validation.verify.assert_not_called()
        prompt = fixture.effects.agent.run_agent.call_args.args[5]
        self.assertIn('Worker targeted unit test는 실행되지 않았고 외부 targeted test 결과도', prompt)
        self.assertIn('유효한 사용자 verify 생략 (SKIPPED_BY_USER', prompt)
        self.assertIn('전체 verify PASS가 아닙니다', prompt)
        self.assertIn('제공 사유:', prompt)
        self.assertIn('<REDACTED>', prompt)
        self.assertNotIn('waiver-secret-value', prompt)
        self.assertNotIn('Manager 확인: 전체 verify PASS', prompt)
        self.assertEqual(fixture.connection.execute(
            "SELECT result FROM manual_work_results WHERE stage='verify'"
        ).fetchone()['result'], 'SKIPPED_BY_USER')
        self.assertEqual(fixture.connection.execute(
            "SELECT result FROM manual_work_results WHERE stage='review'"
        ).fetchone()['result'], 'PASS')
        publication = candidate.publication('', fixture.plan, lambda *_: self.fail(
            'manual candidate should not use Worker publication data'))
        self.assertEqual(publication.test_evidence, MANUAL_TEST_EVIDENCE)
        self.assertNotIn('targeted test: PASS', publication.summary)
        self.assertIn('제공 사유:', publication.summary)
        self.assertIn('<REDACTED>', publication.summary)
        self.assertNotIn('waiver-secret-value', publication.summary)
        body=candidate.decorate_body('PR body')
        self.assertIn('제공 사유:', body)
        self.assertIn('<REDACTED>', body)
        self.assertNotIn('waiver-secret-value', body)
        self.assertIsNone(fixture.state.row()['worker_result'])


    def test_missing_and_legacy_waiver_reasons_are_explicit_without_rewriting_audit(self):
        fixture,context,candidate,_config_path=self._verify_fixture_with_manual_skip(reason='')
        waiver=candidate.waive(context,'verify')
        self.assertEqual(waiver,'SKIPPED_BY_USER @candidate-head; 사유 미입력')
        request=fixture.connection.execute('SELECT reason FROM manual_work_requests').fetchone()
        saved=fixture.connection.execute("SELECT detail FROM manual_work_results "
            "WHERE stage='verify' AND result='SKIPPED_BY_USER'").fetchone()
        self.assertEqual(request['reason'],'')
        self.assertEqual(saved['detail'],waiver)

        prompt=candidate.prompt('Reviewer input',review=True)
        publication=candidate.publication('',fixture.plan,lambda *_: self.fail(
            'manual candidate should not use Worker publication data'))
        body=candidate.decorate_body('PR body')
        worker_context='\n'.join(candidate.worker_context().values())
        for rendered in (prompt,publication.summary,body,worker_context):
            self.assertIn('사유 미입력',rendered)
            self.assertNotIn('제공 사유:',rendered)

        legacy_detail='SKIPPED_BY_USER @candidate-head; '
        with fixture.connection:
            fixture.connection.execute("UPDATE manual_work_results SET detail=? "
                "WHERE stage='verify' AND result='SKIPPED_BY_USER'",(legacy_detail,))
        prompt=candidate.prompt('Reviewer input',review=True)
        publication=candidate.publication('',fixture.plan,lambda *_: self.fail(
            'manual candidate should not use Worker publication data'))
        body=candidate.decorate_body('PR body')
        worker_context='\n'.join(candidate.worker_context().values())
        for rendered in (prompt,publication.summary,body,worker_context):
            self.assertIn('사유 미입력',rendered)
        saved=fixture.connection.execute("SELECT detail FROM manual_work_results "
            "WHERE stage='verify' AND result='SKIPPED_BY_USER'").fetchone()
        self.assertEqual(saved['detail'],legacy_detail)

        legacy_provided_detail='SKIPPED_BY_USER @candidate-head; 사유 미입력'
        with fixture.connection:
            fixture.connection.execute('UPDATE manual_work_requests SET reason=?',('사유 미입력',))
            fixture.connection.execute("UPDATE manual_work_results SET detail=? "
                "WHERE stage='verify' AND result='SKIPPED_BY_USER'",(legacy_provided_detail,))
        prompt=candidate.prompt('Reviewer input',review=True)
        publication=candidate.publication('',fixture.plan,lambda *_: self.fail(
            'manual candidate should not use Worker publication data'))
        body=candidate.decorate_body('PR body')
        worker_context='\n'.join(candidate.worker_context().values())
        for rendered in (prompt,publication.summary,body,worker_context):
            self.assertIn('제공 사유: 사유 미입력',rendered)
            self.assertNotIn('; 사유 미입력',rendered)
        saved=fixture.connection.execute("SELECT detail FROM manual_work_results "
            "WHERE stage='verify' AND result='SKIPPED_BY_USER'").fetchone()
        self.assertEqual(saved['detail'],legacy_provided_detail)


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


    def test_unreadable_stage_settings_never_authorize_a_waiver(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,worktree=self.fixture(directory)
            service.start('G-012')
            args=completion()
            args.skip_verify=True
            args.expected_head=git_command(worktree,'rev-parse','HEAD')
            args.skip_reason='External verify'
            service.complete(args)
            candidate=self.candidate(service,'G-012')
            context=SimpleNamespace(worktree=worktree,base_branch='main')
            self.assertIsNotNone(candidate.waive(context,'verify',record=False))
            service.config_path.unlink()
            self.assertIsNone(candidate.waive(context,'verify',record=False))
            self.assertFalse(candidate.valid_evidence(context,'verify','SKIPPED_BY_USER old'))
            values=[]
            for table in ('manual_work_requests','manual_work_results','manual_work_publications'):
                for row in service.connection.execute(f'SELECT * FROM {table}'):
                    values.extend(str(value) for value in row)
            self.assertNotIn('fixture-secret',' '.join(values))


    def test_legacy_stage_settings_without_input_token_never_authorize_a_waiver(self):
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
            self.assertFalse(candidate.valid_evidence(context,'verify','SKIPPED_BY_USER old'))


    def test_verify_setting_change_after_execution_composition_disables_waiver(self):
        fixture,context,candidate,config_path=self._verify_fixture_with_manual_skip()
        config_path.write_text(config_path.read_text().replace(
            'verify_timeout_seconds=600','verify_timeout_seconds=900'),encoding='utf-8')

        _outcome,output=fixture.handler._verify_once(context,candidate_head='candidate-head')

        fixture.effects.validation.verify.assert_called_once()
        group=candidate.group()
        rows=fixture.connection.execute("SELECT result,detail FROM manual_work_results "
            "WHERE group_id=? AND stage='verify'",(group['group_id'],)).fetchall()
        self.assertIsNotNone(output)
        self.assertIn('NOT_RUN',{row['result'] for row in rows})
        self.assertIn('PASS',{row['result'] for row in rows})
        self.assertNotIn('SKIPPED_BY_USER',{row['result'] for row in rows})
        self.assertTrue(any('실행 구성 이후 단계 설정이 달라져' in row['detail'] for row in rows))


    def test_unreadable_setting_after_execution_composition_disables_waiver(self):
        fixture,context,candidate,config_path=self._verify_fixture_with_manual_skip()
        config_path.unlink()

        _outcome,output=fixture.handler._verify_once(context,candidate_head='candidate-head')

        fixture.effects.validation.verify.assert_called_once()
        group=candidate.group()
        rows=fixture.connection.execute("SELECT result,detail FROM manual_work_results "
            "WHERE group_id=? AND stage='verify'",(group['group_id'],)).fetchall()
        self.assertIsNotNone(output)
        self.assertIn('NOT_RUN',{row['result'] for row in rows})
        self.assertIn('PASS',{row['result'] for row in rows})
        self.assertNotIn('SKIPPED_BY_USER',{row['result'] for row in rows})
        self.assertTrue(any('현재 단계 설정을 확인할 수 없어' in row['detail'] for row in rows))
