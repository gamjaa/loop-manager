"""Publication policy uses real SQLite/files and mocked Git command observations."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.catalog.archive_git import ArchiveGitEffects, ExecPlanArchiveGitPort
from loop_manager_internal.catalog.requirement_correction_publication import CorrectionPending, RequirementCorrectionPublisher
from loop_manager_internal.storage.requirement_correction_state import claimed_paths
from loop_manager_internal.storage.agent_invocation_state import SCHEMA as AGENT_INVOCATIONS_SCHEMA


A = 'docs/requirements/[R-001] A.md'
B = 'docs/requirements/[R-001] B.md'
NEW = 'docs/requirements/[R-002] B.md'


def requirement(title):
    return f'# [R-001] {title}\n\nPreserve criteria.\n'.encode()


class GitObservations:
    """Only external commands are replaced; publisher/calculator/adapter run."""
    def __init__(self, root):
        self.root = root
        self.trees = {}
        self.blobs = {}
        self.heads = {}
        self.index = {}
        self.parents = {'base': None}
        self.remote = 'base'
        self.commits = self.pushes = 0
        self.fail = None
        self.fetch_hook = None
        self.commit_hook = None
        self.fetches = 0
        self.checkout_crlf = False
        self.add_tree('base', {A: requirement('A'), B: requirement('B'),
            'docs/PRODUCT.md': b'[link](requirements/%5BR-001%5D%20B.md)\n'})

    def add_tree(self, revision, files):
        tree = {}
        for path, raw in files.items():
            oid = hashlib.sha256(raw).hexdigest()
            self.blobs[oid] = raw
            tree[path] = ('100644', 'blob', oid)
        self.trees[revision] = tree

    def fetch(self, *args, **kwargs):
        self.fetches += 1
        if self.fetch_hook:
            self.fetch_hook(self.fetches)
        return self.remote

    def trip(self, boundary):
        if self.fail == boundary:
            self.fail = None
            raise RuntimeError('interrupted at ' + boundary)

    def bytes(self, repo, command, *args):
        if command == 'ls-tree':
            revision = next(arg for arg in args if arg in self.trees)
            return b''.join(f'{mode} {kind} {oid}\t{path}'.encode() + b'\0'
                for path, (mode, kind, oid) in self.trees[revision].items())
        if command == 'cat-file':
            return self.blobs[args[-1]]
        raise AssertionError((command, args))

    def files(self, workspace):
        return {path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob('*') if path.is_file()}

    def git(self, repo, command, *args, check=True):
        output, code = '', 0
        repo = Path(repo)
        if command == 'rev-list':
            output = 'base\n' if '--reverse' in args else args[-1] + ' ' + self.parents[args[-1]]
        elif command == 'log':
            output = 'base\n'
        elif command == 'rev-parse':
            revision = args[0]
            output = revision[:-7] if revision.endswith('^{tree}') else self.heads[repo]
        elif command == 'worktree':
            workspace, base = Path(args[-2]), args[-1]
            workspace.mkdir(parents=True)
            self.heads[workspace] = base
            self.index[workspace] = dict(self.trees[base])
            for path, (_mode, _kind, oid) in self.trees[base].items():
                target = workspace / path
                target.parent.mkdir(parents=True, exist_ok=True)
                raw = self.blobs[oid]
                target.write_bytes(raw.replace(b'\n', b'\r\n') if self.checkout_crlf else raw)
            self.trip('worktree')
        elif command == 'write-tree':
            output = 'tree-' + hashlib.sha256(repr(self.index[repo]).encode()).hexdigest()
            self.trees[output] = dict(self.index[repo])
        elif command == 'diff':
            base = self.trees[next(arg for arg in args if arg in self.trees)]
            if '--cached' in args:
                current = self.index[repo]
                changed = {p for p in base.keys() | current.keys() if base.get(p) != current.get(p)}
            else:
                files = self.files(repo)
                if self.checkout_crlf:
                    files = {path: raw.replace(b'\r\n', b'\n') for path, raw in files.items()}
                changed = {p for p in base if p not in files or files[p] != self.blobs[base[p][2]]}
            output = ''.join(p + '\0' for p in sorted(changed))
        elif command == 'ls-files':
            output = ''.join(p + '\0' for p in self.files(repo) if p not in self.index[repo])
        elif command == 'add':
            for path in args[1:]:
                path = path.removeprefix(':(literal)')
                raw = (repo / path).read_bytes()
                oid = hashlib.sha256(raw).hexdigest()
                self.blobs[oid] = raw
                self.index[repo][path] = ('100644', 'blob', oid)
            self.trip('add')
        elif command == 'update-index':
            for path in args[args.index('--') + 1:]:
                self.index[repo].pop(path, None)
            self.trip('delete')
        elif command == 'status':
            head = self.trees[self.heads[repo]]
            files = self.files(repo)
            if self.checkout_crlf:
                files = {path: raw.replace(b'\r\n', b'\n') for path, raw in files.items()}
            dirty = (set(files) != set(head) or self.index[repo] != head or
                     any(files[p] != self.blobs[value[2]] for p, value in head.items() if p in files))
            output = 'dirty' if dirty else ''
        elif command == 'merge-base':
            ancestor, descendant = args[-2:]
            while descendant is not None and descendant != ancestor:
                descendant = self.parents.get(descendant)
            code = 0 if descendant == ancestor else 1
        elif command == 'remote':
            output = 'mock://origin\n'
        elif command == 'push':
            self.pushes += 1
            if self.fail == 'push-rejected':
                self.fail = None
                code, output = 1, 'protected branch'
            else:
                self.remote = args[-1].split(':')[0]
                self.trip('push')
        else:
            raise AssertionError((command, args))
        return subprocess.CompletedProcess([command, *args], code, output)

    def command(self, argv, **kwargs):
        workspace = Path(argv[argv.index('-C') + 1])
        self.commits += 1
        oid = 'commit-' + str(self.commits)
        self.parents[oid] = self.heads[workspace]
        self.trees[oid] = dict(self.index[workspace])
        self.heads[workspace] = oid
        if self.commit_hook:
            self.commit_hook()
        self.trip('commit')
        return subprocess.CompletedProcess(argv, 0, '')


class RequirementCorrectionPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.connection = sqlite3.connect(self.root / 'state.sqlite')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript('''
            CREATE TABLE exec_plans(repo TEXT,plan_id TEXT,sources TEXT);
            CREATE TABLE execplan_cancellations(repo TEXT,plan_id TEXT,sources TEXT);
            CREATE TABLE document_drafts(repo TEXT,target_path TEXT,state TEXT);
        ''')
        self.observations = GitObservations(self.root)
        effects = ArchiveGitEffects(self.observations.fetch, mock.Mock(),
            self.observations.command, self.observations.git, self.observations.bytes,
            lambda output: False, mock.Mock())
        port = ExecPlanArchiveGitPort(self.root / 'checkout', self.root / 'state', 'repo', effects=lambda: effects)
        self.safe = mock.Mock(return_value=True)
        self.publisher = RequirementCorrectionPublisher(self.connection, port, refresh=lambda base: True, safe=self.safe)

    def test_publishes_exact_calculated_bytes_links_and_preserves_stored_sources(self):
        self.connection.execute('INSERT INTO exec_plans VALUES (?,?,?)', ('repo', 'G-001', json.dumps([A])))
        self.connection.commit()
        commit = self.publisher.dispatch('main')
        tree = self.observations.trees[commit]
        self.assertNotIn(B, tree)
        self.assertEqual(self.observations.blobs[tree[NEW][2]], requirement('B').replace(b'R-001', b'R-002'))
        self.assertIn(b'%5BR-002%5D', self.observations.blobs[tree['docs/PRODUCT.md'][2]])
        self.assertEqual(self.connection.execute('SELECT sources FROM exec_plans').fetchone()[0], json.dumps([A]))
        self.assertEqual(self.publisher.repository.confirmed('repo')[0]['commit_oid'], commit)
        self.assertIsNone(self.publisher.dispatch('main'))
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_stored_only_reference_changes_keeper(self):
        self.connection.execute('INSERT INTO exec_plans VALUES (?,?,?)', ('repo', 'G-002', json.dumps([B])))
        result = self.publisher.observe('base')
        self.assertEqual(result['assignments'][0][0], A)

    def test_writer_gate_distinguishes_unstarted_failures_from_unconfirmed_writers(self):
        self.connection.execute(AGENT_INVOCATIONS_SCHEMA)
        cases = [
            ('not_executed', 'finished', kind, None, False)
            for kind in ('exception', 'interrupted', 'timeout')
        ] + [
            (status, 'finished', kind, None, True)
            for status in ('executed', 'unknown')
            for kind in ('exception', 'interrupted', 'timeout')
        ] + [
            ('not_executed', None, 'exception', None, True),
            ('executed', None, None, None, True),
            ('executed', 'finished', 'exception', 1, True),
            ('executed', 'finished', 'interrupted', -1, True),
            ('unknown', 'finished', 'success', 0, True),
        ]
        for status, finished, kind, code, blocked in cases:
            with self.subTest(status=status, finished=finished, kind=kind, code=code):
                self.connection.execute('DELETE FROM agent_invocations')
                self.connection.execute('''INSERT INTO agent_invocations
                    (invocation_id, repo, role, provider, model, effort, transition,
                     attempt, started_at, execution_status, finished_at, result_kind, result_code)
                    VALUES ('attempt', 'repo', 'planner', 'codex', 'model', 'medium',
                            'new', 1, 'started', ?, ?, ?, ?)''', (status, finished, kind, code))
                if blocked:
                    with self.assertRaisesRegex(CorrectionPending, '종료가 확인되지 않은 Agent writer'):
                        self.publisher.observe('base')
                else:
                    result = self.publisher.observe('base')
                    self.assertTrue(result['assignments'])
                self.connection.execute("UPDATE agent_invocations SET execution_finished_at='observed'")
                self.assertTrue(self.publisher.observe('base')['assignments'])
                row = self.connection.execute('SELECT result_kind,result_code FROM agent_invocations').fetchone()
                self.assertEqual(tuple(row), (kind, code))
        self.assertEqual((self.observations.commits, self.observations.pushes), (0, 0))

    def test_reference_query_failure_does_not_mean_no_references(self):
        self.connection.execute('DROP TABLE execplan_cancellations')
        with self.assertRaisesRegex(CorrectionPending, 'execplan_cancellations'):
            self.publisher.dispatch('main')
        self.assertEqual((self.observations.commits, self.observations.pushes), (0, 0))

    def test_push_rejection_preserves_candidate_and_reuses_commit(self):
        self.observations.fail = 'push-rejected'
        with self.assertRaisesRegex(CorrectionPending, 'protected branch'):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        self.assertTrue(claimed_paths(self.connection, 'repo'))
        self.assertEqual(self.publisher.dispatch('main'), saved['commit_oid'])
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 2))

    def test_recovery_at_file_commit_push_and_state_boundaries(self):
        for boundary in ('worktree', 'delete', 'add', 'commit', 'push', 'commit-state', 'confirm-state', 'intent-state'):
            with self.subTest(boundary=boundary):
                # Independent SQLite, paths and command observations per case.
                case = RequirementCorrectionPublicationTest('test_stored_only_reference_changes_keeper')
                case.setUp()
                try:
                    original = case.publisher.repository.update
                    record = case.publisher.repository.record
                    tripped = False
                    def update(*args, **kwargs):
                        nonlocal tripped
                        if not tripped and ((boundary == 'commit-state' and kwargs.get('commit_oid')) or
                                (boundary == 'confirm-state' and kwargs.get('stage') == 'confirmed')):
                            tripped = True
                            raise RuntimeError('state interrupted')
                        return original(*args, **kwargs)
                    def record_then_interrupt(*args, **kwargs):
                        record(*args, **kwargs)
                        raise RuntimeError('intent interrupted')
                    case.observations.fail = boundary
                    with mock.patch.object(case.publisher.repository, 'update', side_effect=update), mock.patch.object(
                            case.publisher.repository, 'record', side_effect=record_then_interrupt if boundary == 'intent-state' else record):
                        with self.assertRaises(CorrectionPending):
                            case.publisher.dispatch('main')
                    commit = case.publisher.dispatch('main')
                    self.assertEqual(case.publisher.repository.confirmed('repo')[0]['commit_oid'], commit)
                    self.assertEqual((case.observations.commits, case.observations.pushes), (1, 1))
                finally:
                    case.doCleanups()

    def test_new_stored_reference_before_push_preserves_then_recalculates(self):
        def commit_then_reference():
            self.connection.execute('INSERT INTO exec_plans VALUES (?,?,?)', ('repo', 'G-002', json.dumps([B])))
            self.connection.commit()
        self.observations.commit_hook = commit_then_reference
        with self.assertRaisesRegex(CorrectionPending, '직전'):
            self.publisher.dispatch('main')
        self.observations.commit_hook = None
        with self.assertRaisesRegex(CorrectionPending, '입력이 변경'):
            self.publisher.dispatch('main')
        commit = self.publisher.dispatch('main')
        self.assertIn('docs/requirements/[R-002] A.md', self.observations.trees[commit])
        self.assertIn(B, self.observations.trees[commit])
        self.assertEqual(self.observations.pushes, 1)

    def test_remote_advance_and_destination_race_do_not_push_stale_result(self):
        def advance(count):
            if count == 2:
                self.observations.add_tree('advanced', {A: requirement('A'), B: requirement('B'), NEW: b'# [R-002] B\n'})
                self.observations.parents['advanced'] = 'base'
                self.observations.remote = 'advanced'
        self.observations.fetch_hook = advance
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        commit = self.publisher.dispatch('main')
        self.assertIn('docs/requirements/[R-003] B.md', self.observations.trees[commit])
        self.assertEqual(self.observations.pushes, 1)

    def test_tampered_or_removed_result_is_preserved_without_recommit(self):
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        workspace = self.publisher._workspace(saved['operation_id'])
        (workspace / NEW).write_bytes(b'user edit')
        with self.assertRaisesRegex(CorrectionPending, '변조'):
            self.publisher.dispatch('main')
        self.assertEqual((workspace / NEW).read_bytes(), b'user edit')
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_remote_published_result_removed_before_state_confirmation(self):
        self.observations.fail = 'push'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        self.observations.add_tree('later', {A: requirement('A')})
        self.observations.parents['later'] = self.observations.remote
        self.observations.remote = 'later'
        with self.assertRaisesRegex(CorrectionPending, '변경·제거'):
            self.publisher.dispatch('main')
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_nonregular_input_and_ownership_gate_prevent_writes(self):
        self.observations.trees['base'][B] = ('120000', 'blob', self.observations.trees['base'][B][2])
        with self.assertRaisesRegex(CorrectionPending, '일반 파일'):
            self.publisher.dispatch('main')
        self.assertEqual(self.observations.commits, 0)
        self.safe.return_value = False
        self.assertIsNone(self.publisher.dispatch('main'))

    def test_archived_plan_and_user_decision_remain_byte_identical(self):
        plan_path = 'docs/plans/archive/[G-001] Old.md'
        raw = b'---\nid: G-001\ntitle: Old\nsources:\n  - docs/PRODUCT.md\ndepends_on:\ncomplexity: low\nrisk: low\n---\n## User Decision\n[keep](../../requirements/%5BR-001%5D%20B.md)\n'
        files = {A: requirement('A'), B: requirement('B'), plan_path: raw}
        self.observations.add_tree('base', files)
        commit = self.publisher.dispatch('main')
        self.assertEqual(self.observations.blobs[self.observations.trees[commit][plan_path][2]], raw)

    def test_connection_dispatches_correction_before_archive_without_model(self):
        from loop_manager_internal.catalog import archive_service, execplan_archive
        refresh = mock.Mock()
        refresh.refresh.side_effect = lambda base: base
        effects = archive_service.DispatchExecplanArchiveHousekeepingEffects(
            lambda *_args: self.publisher.git, self.observations.git, mock.Mock(), mock.Mock())
        with mock.patch.object(execplan_archive.ExecPlanArchiveDispatcher, 'dispatch') as archive:
            def archive_after_correction(*_args):
                self.assertEqual(self.observations.pushes, 1)
                return execplan_archive.ArchiveDispatchOutcome(execplan_archive.ArchiveDispatchKind.NO_CANDIDATES)
            archive.side_effect = archive_after_correction
            archive_service.dispatch_execplan_archive_housekeeping(
                effects, self.connection, self.publisher.git.repo_path, self.publisher.git.state_dir,
                'repo', 'main', catalog_refresh=refresh, archive_safety=self.safe)
        self.assertEqual(self.observations.commits, 1)

    def test_project_boundary_does_not_swallow_correction_failure(self):
        from types import SimpleNamespace
        from loop_manager_internal.application.project_service import ProjectService
        service = ProjectService.__new__(ProjectService)
        service.identity = SimpleNamespace(connection=self.connection, state_dir=self.root,
            registered_only=False, refactoring_review_interval=5)
        service.state = SimpleNamespace(repo='repo', repo_path=self.root, base='main',
            validated_agent_scope=None, catalog_refresh=None)
        service.effects = SimpleNamespace(_execplan_archive_housekeeping_safe=lambda *_a, **_k: True,
            dispatch_execplan_archive_housekeeping=mock.Mock(side_effect=CorrectionPending('preserve candidate')))
        with self.assertRaisesRegex(CorrectionPending, 'preserve candidate'):
            service._archive_housekeeping()

    def test_prepared_candidate_blocks_conflicting_manual_publication(self):
        from types import SimpleNamespace
        from loop_manager_internal.storage import work_claims
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        with self.assertRaisesRegex(RuntimeError, '교정 게시 소유 예약'):
            work_claims.assert_manual_scopes_available(self.connection, 'repo', SimpleNamespace(publication_paths=(B,)))

    def test_document_draft_preserves_link_bytes_and_manual_source_defers(self):
        self.connection.execute('INSERT INTO document_drafts VALUES (?,?,?)', ('repo', 'docs/PRODUCT.md', 'pending'))
        self.connection.commit()
        commit = self.publisher.dispatch('main')
        self.assertEqual(self.observations.blobs[self.observations.trees[commit]['docs/PRODUCT.md'][2]],
            b'[link](requirements/%5BR-001%5D%20B.md)\n')
        self.connection.executescript('''CREATE TABLE manual_work_groups
            (repo TEXT,requirements TEXT,documents TEXT,state TEXT);
            INSERT INTO manual_work_groups VALUES ('repo','["docs/requirements/[R-001] A.md"]','[]','GENERATING');''')
        self.assertIsNone(self.publisher.dispatch('main'))
        self.assertEqual(self.observations.pushes, 1)

    def test_confirmed_correction_allows_later_content_preserving_archive(self):
        commit = self.publisher.dispatch('main')
        files = {path: self.observations.blobs[entry[2]] for path, entry in self.observations.trees[commit].items()}
        files['docs/requirements/archive/[R-002] B.md'] = files.pop(NEW)
        self.observations.add_tree('archive', files)
        self.observations.parents['archive'] = commit
        self.observations.remote = 'archive'
        self.assertIsNone(self.publisher.dispatch('main'))
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_confirmed_correction_result_deletion_keeps_audit_and_allows_independent_review(self):
        commit = self.publisher.dispatch('main')
        self.observations.add_tree('deleted-result', {A: requirement('A')})
        self.observations.parents['deleted-result'] = commit
        self.observations.remote = 'deleted-result'

        self.assertIsNone(self.publisher.dispatch('main'))
        confirmed = self.publisher.repository.confirmed('repo')
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]['commit_oid'], commit)
        self.assertNotIn(NEW, self.observations.trees['deleted-result'])
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_manual_preparation_gate_reads_durable_correction_reservation(self):
        from types import SimpleNamespace
        from loop_manager_internal.application.manual_concurrency import preparation_gate
        from loop_manager_internal.application.manual_errors import ManualConflictError
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        self.connection.executescript('''CREATE TABLE project_blocks(repo TEXT);
            CREATE TABLE manual_work_groups(repo TEXT,kind TEXT,state TEXT,group_id TEXT,requirements TEXT);''')
        service = SimpleNamespace(connection=self.connection, repo='repo')
        with self.assertRaisesRegex(ManualConflictError, '게시 소유 예약'):
            preparation_gate(service, requirements=[B])

    def test_file_mode_tampering_stops_recovery(self):
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        target = self.publisher._workspace(saved['operation_id']) / NEW
        original_stat = Path.stat
        observed = target.stat()
        changed = os.stat_result((observed.st_mode | 0o111, *observed[1:]))
        with mock.patch.object(Path, 'stat', autospec=True, side_effect=
                               lambda path, *args, **kwargs: changed if path == target
                               else original_stat(path, *args, **kwargs)), \
             self.assertRaisesRegex(CorrectionPending, 'mode'):
            self.publisher.dispatch('main')
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_diagnostic_storage_failure_still_stops_project_boundary(self):
        self.observations.fail = 'push-rejected'
        with mock.patch.object(self.publisher.repository, 'update', side_effect=sqlite3.OperationalError('storage failure')):
            with self.assertRaisesRegex(CorrectionPending, '진단 저장 실패'):
                self.publisher.dispatch('main')
        self.assertEqual(self.observations.pushes, 0)

    def test_manual_source_ownership_defers_before_remote_observation(self):
        self.connection.executescript('''CREATE TABLE manual_work_groups
            (repo TEXT,requirements TEXT,documents TEXT,state TEXT);
            INSERT INTO manual_work_groups VALUES ('repo','["docs/requirements/[R-001] A.md"]','[]','GENERATING');''')
        with mock.patch.object(self.observations, 'fetch', side_effect=AssertionError('remote observation forbidden')):
            self.assertIsNone(self.publisher.dispatch('main'))
        self.assertEqual(self.observations.fetches, 0)
        self.assertEqual((self.observations.commits, self.observations.pushes), (0, 0))
        self.assertIsNone(self.publisher.repository.pending('repo'))

    def test_manual_owner_preserves_pending_candidate_until_boundary_reopens(self):
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        workspace = self.publisher._workspace(saved['operation_id'])
        files = self.observations.files(workspace)
        self.connection.executescript('''CREATE TABLE manual_work_groups
            (repo TEXT,requirements TEXT,documents TEXT,state TEXT);
            INSERT INTO manual_work_groups VALUES ('repo','["docs/requirements/[R-001] A.md"]','[]','GENERATING');''')
        observed = self.observations.fetches
        self.assertIsNone(self.publisher.dispatch('main'))
        self.assertEqual(self.publisher.repository.pending('repo'), saved)
        self.assertEqual(self.observations.files(workspace), files)
        self.assertEqual(self.observations.fetches, observed)
        self.connection.execute("UPDATE manual_work_groups SET state='MERGED'")
        self.connection.commit()
        self.assertEqual(self.publisher.dispatch('main'), saved['commit_oid'])
        self.assertEqual(self.observations.commits, 1)

    def test_closed_initial_safety_gate_never_observes_remote_or_state_identity(self):
        self.safe.return_value = False
        # The closed gate also precedes the Git adapter's owner identity read.
        port = mock.Mock()
        publisher = RequirementCorrectionPublisher(self.connection, port, refresh=mock.Mock(), safe=self.safe)
        self.assertIsNone(publisher.dispatch('main'))
        port.fetch_remote_base.assert_not_called()
        port._manifest.assert_not_called()

    def test_safety_gate_closing_after_admission_remains_a_failure(self):
        self.safe.side_effect = [True, False]
        with self.assertRaisesRegex(CorrectionPending, '안전한 처리'):
            self.publisher.dispatch('main')
        self.assertEqual((self.observations.commits, self.observations.pushes), (0, 0))

    def test_crlf_checkout_publishes_exact_blob_bytes(self):
        self.observations.checkout_crlf = True
        # Stop after checkout to prove the starting file differs from its blob.
        self.observations.fail = 'worktree'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        workspace = self.publisher._workspace(saved['operation_id'])
        self.assertEqual((workspace / B).read_bytes(), requirement('B').replace(b'\n', b'\r\n'))
        commit = self.publisher.dispatch('main')
        self.assertEqual(self.observations.blobs[self.observations.trees[commit][NEW][2]],
                         requirement('B').replace(b'R-001', b'R-002'))
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_committed_crlf_checkout_recovers_without_recommit(self):
        self.observations.checkout_crlf = True
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        workspace = self.publisher._workspace(saved['operation_id'])
        for path in self.observations.files(workspace):
            target = workspace / path
            target.write_bytes(target.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
        self.assertEqual(self.publisher.dispatch('main'), saved['commit_oid'])
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 2))

    def test_crlf_checkout_still_rejects_content_and_lone_cr_mutations(self):
        self.observations.checkout_crlf = True
        self.observations.fail = 'worktree'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        target = self.publisher._workspace(saved['operation_id']) / B
        original = target.read_bytes()
        for raw in (original.replace(b'criteria', b'changed'), original.replace(b'\r\n', b'\r')):
            with self.subTest(raw=raw):
                target.write_bytes(raw)
                with self.assertRaisesRegex(CorrectionPending, '변조'):
                    self.publisher.dispatch('main')
                self.assertEqual(target.read_bytes(), raw)
                self.assertEqual((self.observations.commits, self.observations.pushes), (0, 0))

    def test_superseded_candidate_reuses_commit_when_draft_input_returns(self):
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        self.connection.execute('INSERT INTO document_drafts VALUES (?,?,?)', ('repo', 'docs/PRODUCT.md', 'pending'))
        self.connection.commit()
        with self.assertRaisesRegex(CorrectionPending, '입력이 변경'):
            self.publisher.dispatch('main')
        self.assertIsNone(self.publisher.repository.pending('repo'))
        self.connection.execute("UPDATE document_drafts SET state='rejected'")
        self.connection.commit()
        commit = self.publisher.dispatch('main')
        self.assertEqual(commit, saved['commit_oid'])
        self.assertEqual(self.publisher.repository.confirmed('repo')[0]['operation_id'], saved['operation_id'])
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 2))

    def test_superseded_reentry_still_rejects_tampered_worktree(self):
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        self.connection.execute('INSERT INTO document_drafts VALUES (?,?,?)', ('repo', 'docs/PRODUCT.md', 'pending'))
        self.connection.commit()
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        target = self.publisher._workspace(saved['operation_id']) / NEW
        target.write_bytes(b'user edit')
        self.connection.execute("UPDATE document_drafts SET state='rejected'")
        self.connection.commit()
        with self.assertRaisesRegex(CorrectionPending, '변조'):
            self.publisher.dispatch('main')
        self.assertEqual(target.read_bytes(), b'user edit')
        self.assertEqual((self.observations.commits, self.observations.pushes), (1, 1))

    def test_superseded_record_guard_keeps_stage_commit_and_reason_on_stale_input(self):
        self.observations.fail = 'push-rejected'
        with self.assertRaises(CorrectionPending):
            self.publisher.dispatch('main')
        saved = self.publisher.repository.pending('repo')
        repository = self.publisher.repository
        repository.update('repo', saved['operation_id'], stage='superseded', reason='input changed')
        for manifest, current in ((json.loads(saved['manifest']), lambda: False),
                                  ({'different': 'input'}, lambda: True)):
            with self.subTest(manifest=manifest):
                with self.assertRaises(RuntimeError):
                    repository.record('repo', saved['operation_id'], manifest, current=current)
                row = dict(self.connection.execute('SELECT * FROM requirement_correction_publications').fetchone())
                self.assertEqual((row['stage'], row['commit_oid'], row['reason']),
                                 ('superseded', saved['commit_oid'], 'input changed'))
