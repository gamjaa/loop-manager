"""Deterministic conflict identity and action tests."""

from unittest import TestCase
from unittest import mock
from types import SimpleNamespace

from loop_manager_internal.git.rebase_correction import (
    ConflictObservation, CorrectionAction, CorrectionSource, execute_git_step,
    next_action, stage_evidence_preserved,
    parse_unmerged,
)


class RebaseCorrectionTests(TestCase):
    def observation(self, *, stages=None, files=None, position='commit-a',
                    source=CorrectionSource.REBASE, stash=None):
        return ConflictObservation(
            'definition', 'base', 'before',
            (('app.py', 1, 'blob-a'),) if stages is None else stages,
            (('app.py', 'content-a'),) if files is None else files,
            'staged-a', position, source, stash,
        )

    def test_fingerprint_uses_stage_blobs_and_progress_uses_content_and_position(self):
        first = self.observation()
        content = self.observation(files=(('app.py', 'resolved'),))
        position = self.observation(position='commit-b')
        stage = self.observation(stages=(('app.py', 2, 'blob-b'),))
        self.assertEqual(first.fingerprint, content.fingerprint)
        self.assertEqual(first.fingerprint, position.fingerprint)
        self.assertNotEqual(first.progress, content.progress)
        self.assertNotEqual(first.progress, position.progress)
        self.assertNotEqual(first.fingerprint, stage.fingerprint)
        staged = ConflictObservation(
            first.plan_revision, first.target_base, first.before_head,
            first.stages, first.worktree_files, 'new staged blob',
            first.rebase_position, first.source, first.stash_oid,
        )
        self.assertEqual(first.fingerprint, staged.fingerprint)
        self.assertNotEqual(first.progress, staged.progress)

    def test_autostash_completion_never_continues_rebase(self):
        observation = self.observation(stages=(), source=CorrectionSource.AUTOSTASH,
                                       stash='stash-oid')
        observation = ConflictObservation(
            observation.plan_revision, observation.target_base,
            observation.before_head, (), observation.worktree_files,
            observation.staged_diff, None, observation.source,
            observation.stash_oid,
        )
        self.assertEqual(next_action(observation, worker_result=True, staged=True),
                         CorrectionAction.VERIFY)
        self.assertEqual(next_action(self.observation(source=CorrectionSource.AUTOSTASH),
                                     worker_result=False, staged=False),
                         CorrectionAction.USER_ACTION)

    def test_index_parser_keeps_conflict_paths_and_stage_oids(self):
        parsed = parse_unmerged('100644 blob-a 1\tapp.py\0'
                                '100644 blob-b 2\tapp.py\0')
        self.assertEqual(parsed, (('app.py', 1, 'blob-a'), ('app.py', 2, 'blob-b')))

    def test_completed_stage_accepts_only_disappeared_clean_tracked_paths(self):
        accepted = {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'app.py': 'accepted-content', 'other.py': 'other-content'},
        }
        observed = {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'other.py': 'other-content'},
        }
        clean = {'app.py': 'accepted-content'}
        self.assertTrue(stage_evidence_preserved(accepted, observed, {'app.py'}, clean))
        self.assertFalse(stage_evidence_preserved(accepted, observed, set(), clean))
        self.assertFalse(stage_evidence_preserved(accepted, observed, {'app.py'}, {}))
        self.assertFalse(stage_evidence_preserved(
            accepted, observed, {'app.py'}, {'app.py': 'head-content'},
        ))
        self.assertFalse(stage_evidence_preserved(
            accepted, {**observed, 'files': {'other.py': 'changed-content'}},
            {'app.py'}, clean,
        ))
        self.assertFalse(stage_evidence_preserved(
            accepted, {**observed, 'files': {'other.py': 'other-content',
                                            'extra.py': 'new-content'}},
            {'app.py'}, clean,
        ))
        self.assertFalse(stage_evidence_preserved(
            accepted, {**observed, 'head': 'different-head'}, {'app.py'}, clean,
        ))
        self.assertFalse(stage_evidence_preserved(None, observed, {'app.py'}, clean))

    def test_stage_and_continue_actions_use_narrow_git_operations(self):
        git = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='done'))
        stage = execute_git_step(CorrectionAction.STAGE, git=git, worktree='/worktree',
                                 paths=('app.py', 'docs/PRODUCT.md'))
        self.assertEqual(stage.action, CorrectionAction.STAGE)
        git.assert_called_with('/worktree', 'add', '-A', '--', 'app.py',
                               'docs/PRODUCT.md', check=False)
        resolved = ConflictObservation(
            'definition', 'base', 'before', (), (), '', 'position',
            CorrectionSource.REBASE,
        )
        self.assertEqual(next_action(resolved, worker_result=True, staged=True),
                         CorrectionAction.CONTINUE)
        self.assertEqual(next_action(resolved, worker_result=True, staged=False),
                         CorrectionAction.SKIP)

        git.reset_mock()
        continued = execute_git_step(CorrectionAction.CONTINUE, git=git, worktree='/worktree')
        self.assertEqual(continued.returncode, 0)
        self.assertEqual(continued.output, 'done')
        git.assert_called_once_with('/worktree', '-c', 'core.editor=true',
                                    'rebase', '--continue', check=False)

        git.reset_mock()
        git.return_value = SimpleNamespace(returncode=1, stdout='conflict remains')
        skipped = execute_git_step(CorrectionAction.SKIP, git=git, worktree='/worktree')
        self.assertEqual((skipped.returncode, skipped.output), (1, 'conflict remains'))
        git.assert_called_once_with('/worktree', 'rebase', '--skip', check=False)

    def test_observation_helper_preserves_explicit_empty_conflict_and_file_sets(self):
        empty = self.observation(stages=(), files=())
        self.assertEqual(empty.stages, ())
        self.assertEqual(empty.worktree_files, ())


class DocumentConflictPolicyTests(TestCase):
    def test_clean_replay_after_document_adoption_is_not_accepted(self):
        from pathlib import Path
        from loop_manager_internal.execution.execution_rebase_handler import RebaseHandler
        from loop_manager_internal.git import rebase_documents
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        path = 'docs/requirements/[R-001] Goal.md'
        desired = {'mode': '100644', 'oid': 'base-blob', 'blob': 'base-hash'}
        earlier = {'state': 'staged', 'target': 'base', 'before_head': 'before',
                   'paths': [path], 'originals': {path: {'base': desired}}}
        # The final replay position may have adopted a different document.
        record = {**earlier, 'paths': [], 'originals': {}, 'history': [earlier]}
        checkpoint = SimpleNamespace(snapshot={'rebase_documents': record},
                                     target_base_oid='base', expected_head='before')
        ports = mock.Mock()
        ports.checkpoint.get.return_value = checkpoint
        ports._unmerged_stages.return_value = ()
        ports._rebase_in_progress_for_branch.return_value = False
        handler = RebaseHandler(ports)
        handler._advance_document_rebase = mock.Mock(return_value=SimpleNamespace(output='clean replay complete'))
        context = SimpleNamespace(worktree=Path('/owned'), branch='loop-manager/G-001',
                                  plan=SimpleNamespace(plan_id='G-001'))
        actual = {path: {'head': {**desired, 'oid': 'stale-blob'},
                        'index': {'0': {**desired, 'oid': 'stale-blob'}},
                        'worktree': {'mode': '100644', 'blob': 'stale-hash'}}}
        with mock.patch.object(rebase_documents.rebase_file_evidence, 'capture_originals',
                               return_value=(actual, {})) as capture:
            with self.assertRaises(RecoveryFailure) as caught:
                handler._post_rebase_observation(context, 'base', 'initial document conflict')
        self.assertIs(caught.exception.reason, RecoveryReason.PLAN_INPUT_INVALID)
        self.assertIn(path, str(caught.exception))
        self.assertEqual(capture.call_args.args[1], (path,))
        ports._finish_rebase_journal.assert_not_called()
        ports.checkpoint.record.assert_not_called()
        self.assertEqual(checkpoint.snapshot['rebase_documents'], record)

    def test_final_adopted_document_requires_head_index_and_worktree_original(self):
        from pathlib import Path
        from loop_manager_internal.git import rebase_documents
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
        path = 'docs/requirements/[R-001] Goal.md'
        desired = {'mode': '100644', 'oid': 'pinned', 'blob': 'digest'}
        record = {'state': 'staged', 'target': 'base', 'before_head': 'before',
                  'paths': [path], 'originals': {path: {'base': desired}}}
        checkpoint = SimpleNamespace(snapshot={'rebase_documents': record},
                                     target_base_oid='base', expected_head='before')
        valid = {'head': desired, 'index': {'0': desired},
                 'worktree': {'mode': '100644', 'blob': 'digest'}}
        for field in (None, 'head', 'index', 'worktree'):
            actual = dict(valid)
            if field:
                actual[field] = {}
            with self.subTest(field=field), mock.patch.object(
                    rebase_documents.rebase_file_evidence, 'capture_originals', return_value=({path: actual}, {})):
                if field:
                    with self.assertRaises(RecoveryFailure):
                        rebase_documents.validate_final(context=SimpleNamespace(worktree=Path('/owned')),
                                                       checkpoint=checkpoint, git_bytes=mock.Mock())
                else:
                    rebase_documents.validate_final(context=SimpleNamespace(worktree=Path('/owned')),
                                                   checkpoint=checkpoint, git_bytes=mock.Mock())

    def test_only_direct_requirement_and_execplan_documents_are_candidates(self):
        from loop_manager_internal.git.rebase_documents import document_path
        for path in ('docs/requirements/[R-001] Goal.md',
                     'docs/requirements/archive/[R-001] Goal.md',
                     'docs/plans/[G-001] Goal.md', 'docs/plans/archive/[G-001] Goal.md'):
            self.assertTrue(document_path(path), path)
        for path in ('docs/PRODUCT.md', 'docs/ARCHITECTURE.md', 'docs/AGENTS.worker.md',
                     'AGENTS.md', 'docs/other.md', 'app.py', 'test.py', 'config.ini',
                     'docs/plans/manual/[G-001] Goal.md', 'docs/plans/../Goal.md',
                     'docs/plans/x\\Goal.md', 'docs/plans/x:Goal.md'):
            self.assertFalse(document_path(path), path)

    def test_requirement_rename_archive_and_deletion_selection_uses_identity(self):
        from loop_manager_internal.git.rebase_documents import select_paths
        old = 'docs/requirements/[R-001] Old.md'
        renamed = 'docs/requirements/[R-001] New.md'
        archived = 'docs/requirements/archive/[R-001] Old.md'
        catalog = SimpleNamespace(all={})
        self.assertEqual(select_paths((old, 'app.py'), catalog,
            {renamed: '# [R-001] New\n\nNew condition.\n'}), {old, renamed})
        self.assertEqual(select_paths((old,), catalog,
            {archived: '# [R-001] Old\n\nArchived condition.\n'}), {old, archived})
        self.assertEqual(select_paths((old,), catalog, {}), {old})
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            select_paths((old,), catalog, {
                renamed: '# [R-001] New\n',
                'docs/requirements/[R-001] Duplicate.md': '# [R-001] Duplicate\n'})
        with self.assertRaises(ValueError):
            select_paths((renamed,), catalog, {renamed: 'broken identity'})

    def test_execplan_rename_archive_deletion_keeps_pinned_id(self):
        from loop_manager_internal.git.rebase_documents import select_paths
        old = 'docs/plans/[G-002] Old.md'
        original = SimpleNamespace(all={'G-002': SimpleNamespace(path=old, plan_id='G-002')})
        for destination in ('docs/plans/[G-002] New.md', 'docs/plans/archive/[G-002] Old.md'):
            latest = SimpleNamespace(all={'G-002': SimpleNamespace(path=destination, plan_id='G-002')})
            self.assertEqual(select_paths((old, 'src/app.py'), latest, {}, original), {old, destination})
        self.assertEqual(select_paths((old,), SimpleNamespace(all={}), {}, original), {old})
        with self.assertRaisesRegex(RuntimeError, 'identity unavailable'):
            select_paths((old,), SimpleNamespace(all={}), {}, SimpleNamespace(all={}))

    def resolution_ports(self, *, snapshot=None):
        from pathlib import Path
        current = SimpleNamespace(snapshot=snapshot or {}, operation='rebase', generation=2,
                                  target_base_oid='owned-target', expected_head='before',
                                  observed_remote_oid='lease')
        checkpoints = mock.Mock()
        checkpoints.get.return_value = current
        context = SimpleNamespace(worktree=Path('/worktree'), branch='loop-manager/G-001',
            plan=SimpleNamespace(plan_id='G-001', path='docs/plans/[G-001] Goal.md'))
        identity = SimpleNamespace(repo='repo', repo_path=Path('/repo'), base='main')
        def read(_repo, *args, **_kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(stdout='100644 oid 2\tdocs/plans/[G-001] Goal.md\0', returncode=0)
            return SimpleNamespace(stdout='replay' if 'REBASE_HEAD' in args else 'new-base', returncode=0)
        return dict(context=context, identity=identity, checkpoints=checkpoints,
                    git=mock.Mock(side_effect=read), git_bytes=mock.Mock(),
                    require_owned=mock.Mock(), capture_snapshot=mock.Mock(), lookup_pr=mock.Mock())

    def test_invalid_source_or_dependency_preserves_reason_without_selecting_files(self):
        from loop_manager_internal.git import rebase_documents
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        for reason in ('missing source R-001', 'missing dependency G-002', 'ambiguous source R-001'):
            ports = self.resolution_ports()
            with self.subTest(reason=reason), mock.patch.object(
                    rebase_documents.catalog_service, 'revision_execplan_snapshot', side_effect=RuntimeError(reason)):
                with self.assertRaises(RecoveryFailure) as caught:
                    rebase_documents.resolve(**ports)
                self.assertIs(caught.exception.reason, RecoveryReason.PLAN_INPUT_INVALID)
                record = ports['checkpoints'].record.call_args.kwargs['snapshot']['rebase_documents']
                self.assertEqual(record['state'], 'uncertain')
                self.assertEqual(record['reason'], reason)
                self.assertEqual(record['target'], 'owned-target')
                ports['capture_snapshot'].assert_not_called()
                self.assertFalse(any('update-index' in call.args for call in ports['git'].call_args_list))

    def test_later_uncertainty_preserves_adoption_bytes_through_resume_and_final_check(self):
        import base64
        import hashlib
        import tempfile
        from pathlib import Path
        from tests.support.runtime import runtime_rebase_fixture
        from loop_manager_internal.git import rebase_documents
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
        _, _, checkpoints, *_ = runtime_rebase_fixture(self)
        path_a = 'docs/requirements/[R-001] A.md'
        path_b = 'docs/requirements/[R-002] B.md'
        content = b'# [R-002] B\n\nPinned condition.\n'
        digest = hashlib.sha256(content).hexdigest()
        desired = {'mode': '100644', 'oid': 'pinned-b', 'blob': digest}
        adopted = {'state': 'staged', 'position': 'earlier-replay', 'revision': 'new-base',
                   'target': 'owned-target', 'before_head': 'before', 'paths': [path_a],
                   'originals': {path_a: {'base': {'absent': True},
                                          'before': {'mode': '100644', 'oid': 'old-a', 'blob': 'old-a-hash'}}},
                   'blobs': {'old-a-hash': base64.b64encode(b'Original A bytes').decode()},
                   'history': []}
        current = checkpoints.get('repo', 'G-047')
        checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head='before', target_base_oid='owned-target',
            observed_remote_oid='lease', snapshot={'rebase_documents': adopted, 'failure': 'prior failure'})
        ports = self.resolution_ports()
        ports['checkpoints'] = checkpoints
        ports['context'].plan.plan_id = 'G-047'
        revision = ['new-base']
        def git(_root, *args, **kwargs):
            output = ('100644 old-b 2\t' + path_b + '\0' if args[:2] == ('ls-files', '-u') else
                      'replay' if 'REBASE_HEAD' in args else revision[0])
            return SimpleNamespace(stdout=output, returncode=0)
        ports['git'].side_effect = git
        with mock.patch.object(rebase_documents.catalog_service, 'revision_execplan_snapshot',
                               side_effect=RuntimeError('catalog unavailable')):
            with self.assertRaises(RecoveryFailure):
                rebase_documents.resolve(**ports)
        uncertain = checkpoints.get('repo', 'G-047')
        saved = uncertain.snapshot['rebase_documents']
        self.assertEqual(saved['state'], 'uncertain')
        preserved_adoption = {key: value for key, value in adopted.items() if key != 'history'}
        self.assertEqual(saved['history'], [preserved_adoption])
        self.assertEqual(rebase_documents.adopted_originals(uncertain), {path_a: {'absent': True}})
        # Unchanged remote input retries neither catalog nor document mutation.
        with mock.patch.object(rebase_documents.catalog_service, 'revision_execplan_snapshot') as catalog_read:
            with self.assertRaises(RecoveryFailure):
                rebase_documents.resolve(**ports)
            catalog_read.assert_not_called()
        self.assertEqual(checkpoints.get('repo', 'G-047'), uncertain)
        revision[0] = 'corrected-base'
        catalog = SimpleNamespace(all={}, active={'G-047': ports['context'].plan})
        snapshot = SimpleNamespace(head='intermediate', branch=None, index={}, dirty={}, operations={})
        ports['capture_snapshot'].return_value = snapshot
        original_b = {'base': desired, 'worktree': {'absent': True}, 'index': {}}
        applied_b = {'worktree': {'mode': '100644', 'blob': digest}, 'index': {'0': desired}}
        with tempfile.TemporaryDirectory() as directory:
            ports['context'].worktree = Path(directory)
            with mock.patch.object(rebase_documents.catalog_service, 'revision_execplan_snapshot', return_value=catalog), \
                 mock.patch.object(rebase_documents, 'revision_requirement_documents', return_value={path_b: content}), \
                 mock.patch.object(rebase_documents.rebase_file_evidence, 'capture_originals', side_effect=[
                     ({path_b: original_b}, {digest: content}), ({path_b: original_b}, {digest: content}),
                     ({path_b: original_b}, {}), ({path_b: applied_b}, {})]):
                self.assertTrue(rebase_documents.resolve(**ports))
            self.assertEqual((Path(directory) / path_b).read_bytes(), content)
        resumed = checkpoints.get('repo', 'G-047')
        self.assertEqual(resumed.snapshot['failure'], 'prior failure')
        self.assertEqual(resumed.snapshot['rebase_documents']['history'][0], preserved_adoption)
        self.assertEqual(rebase_documents.adopted_originals(resumed), {path_a: {'absent': True}, path_b: desired})
        actual = {path_a: {'head': {'mode': '100644', 'oid': 'stale', 'blob': 'stale'},
                          'index': {}, 'worktree': {'absent': True}},
                  path_b: {'head': desired, **applied_b}}
        with mock.patch.object(rebase_documents.rebase_file_evidence, 'capture_originals',
                               return_value=(actual, {})) as capture:
            with self.assertRaisesRegex(RecoveryFailure, 'A.md'):
                rebase_documents.validate_final(context=ports['context'], checkpoint=resumed, git_bytes=ports['git_bytes'])
        self.assertEqual(capture.call_args.args[1], (path_a, path_b))

    def test_pinned_catalog_rejects_actual_missing_source_and_dependency(self):
        from loop_manager_internal.git import rebase_documents
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        for source, dependency, reason in (
                ('docs/requirements/[R-001] Missing.md', '[]', 'source'),
                ('docs/PRODUCT.md', '[G-002]', 'depends_on')):
            ports = self.resolution_ports()
            plan = ('---\nid: G-001\ntitle: Goal\nsources:\n  - ' + source +
                    '\ndepends_on: ' + dependency + '\ncomplexity: low\nrisk: low\npriority: normal\n---\n\n## Purpose\nCurrent goal.\n')
            bodies = {'plan': plan.encode(), 'product': b'# Product\n'}
            tree = b'100644 blob plan\tdocs/plans/[G-001] Goal.md\0' + b'100644 blob product\tdocs/PRODUCT.md\0'
            def read_bytes(_repo, *args):
                return tree if args[0] == 'ls-tree' else bodies[args[-1]]
            ports['git_bytes'].side_effect = read_bytes
            with self.subTest(reason=reason), self.assertRaises(RecoveryFailure) as caught:
                rebase_documents.resolve(**ports)
            self.assertIs(caught.exception.reason, RecoveryReason.PLAN_INPUT_INVALID)
            record = ports['checkpoints'].record.call_args.kwargs['snapshot']['rebase_documents']
            self.assertEqual(record['state'], 'uncertain')
            self.assertIn(reason, record['reason'])
            self.assertFalse(any('update-index' in call.args for call in ports['git'].call_args_list))

    def test_prepared_resolution_keeps_immutable_revision_and_rejects_pr_race(self):
        from loop_manager_internal.git import rebase_documents
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        saved = {'state': 'prepared', 'position': 'replay', 'target': 'owned-target',
                 'before_head': 'before', 'revision': 'pinned-old-base', 'originals': {},
                 'prs': {'loop-manager/G-001': None}, 'paths': ['docs/plans/[G-001] Goal.md']}
        ports = self.resolution_ports(snapshot={'rebase_documents': saved})
        ports['lookup_pr'].return_value = {'url': 'newly-registered-pr'}
        with self.assertRaises(RecoveryFailure) as caught:
            rebase_documents.resolve(**ports)
        self.assertIs(caught.exception.reason, RecoveryReason.REMOTE_SNAPSHOT_RACE)
        ports['checkpoints'].record.assert_not_called()
        ports['git_bytes'].assert_not_called()
        self.assertFalse(any('origin/main^{commit}' in call.args for call in ports['git'].call_args_list))
        ports['capture_snapshot'].assert_not_called()

    def test_saved_document_pr_observation_failure_preserves_original_intent(self):
        from loop_manager_internal.git import rebase_documents
        saved = {'state': 'prepared', 'position': 'replay', 'target': 'owned-target',
                 'before_head': 'before', 'revision': 'pinned-old-base', 'originals': {},
                 'prs': {'loop-manager/G-001': None}, 'paths': ['docs/plans/[G-001] Goal.md']}
        ports = self.resolution_ports(snapshot={'rebase_documents': saved})
        ports['lookup_pr'].side_effect = RuntimeError('PR observation unavailable')
        with self.assertRaisesRegex(RuntimeError, 'PR observation unavailable'):
            rebase_documents.resolve(**ports)
        ports['checkpoints'].record.assert_not_called()
        ports['capture_snapshot'].assert_not_called()
        ports['git_bytes'].assert_not_called()

    def test_ownership_or_pr_observation_failure_never_becomes_success(self):
        from loop_manager_internal.git import rebase_documents
        ports = self.resolution_ports()
        ports['require_owned'].side_effect = RuntimeError('ownership/PR unavailable')
        with self.assertRaisesRegex(RuntimeError, 'ownership/PR unavailable'):
            rebase_documents.resolve(**ports)
        ports['checkpoints'].record.assert_not_called()
        ports['git_bytes'].assert_not_called()
        ports['capture_snapshot'].assert_not_called()
