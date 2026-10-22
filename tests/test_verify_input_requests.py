"""Unit tests for remote missing-list requests and source authorization gates."""
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.catalog import execplan_catalog as catalog, execplan_drafts, manager_sources
from loop_manager_internal.catalog.requirement_archive import stored_requirement_sources
from loop_manager_internal.execution import plan_validation
from loop_manager_internal.git.command_adapter import revision_document_or_none
from loop_manager_internal.planning.verify_input_request import instruction
from loop_manager_internal.storage import verify_input_requests

SHA = 'a' * 40


def plan_text(request, *, plan_id='G-001', purpose=None, source=None, priority='normal'):
    return ('---\nid: ' + plan_id + '\ntitle: Inputs\nsources:\n  - ' + (source or manager_sources.SOURCE)
            + '\ndepends_on: []\ncomplexity: low\nrisk: medium\n'
            + ('' if priority is None else 'priority: ' + priority + '\n')
            + '---\n\n## Purpose\n\n'
            + (purpose or manager_sources.RULE) + '\n\n## Decision Log\n\n'
            + manager_sources.PREFIX + json.dumps(request) + '\n')


class VerifyInputRequestTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute('CREATE TABLE exec_plans (repo TEXT, plan_id TEXT, title TEXT, sources TEXT, status TEXT)')
        self.repo = str(Path('/project').resolve())
        self.blobs = {}
        self.entries = {}
        self.revision = SHA
        self.git_calls = []

    def git(self, repo, *args):
        self.git_calls.append(args)
        self.assertEqual(args, ('rev-parse', '--verify', 'origin/main^{commit}'))
        return SimpleNamespace(stdout=self.revision)

    def git_bytes(self, repo, *args):
        if args[0] == 'ls-tree':
            self.assertEqual(args[:4], ('ls-tree', '-r', '-z', '--full-tree'))
            self.assertEqual(args[4], self.revision)
            self.assertIn(args[5:], [(), ('--', 'docs/requirements')])
            return b''.join(mode + b' blob ' + oid.encode() + b'\t' + path.encode() + b'\0'
                            for path, (mode, oid) in self.entries.items())
        return self.blobs[args[-1]]

    def remote_file(self, path, raw, mode=b'100644'):
        oid = hashlib.sha256(raw).hexdigest()
        self.entries[path] = (mode, oid)
        self.blobs[oid] = raw

    def instruction(self, active=SHA):
        return instruction(self.connection, self.repo, 'main', active, git=self.git, git_bytes=self.git_bytes)

    def request(self):
        text = self.instruction()
        return manager_sources.declaration(text)

    def test_missing_request_is_registered_once_and_bound_to_project(self):
        first = self.request()
        self.assertEqual(first, self.request())
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM verify_input_requests').fetchone()[0], 1)
        verify_input_requests.validate(self.connection, self.repo, plan_text(first), [manager_sources.SOURCE])
        with self.assertRaisesRegex(RuntimeError, 'unregistered'):
            verify_input_requests.validate(self.connection, '/other', plan_text(first), [manager_sources.SOURCE])

    def test_new_request_requires_critical_after_request_identity_and_scope_validation(self):
        request = self.request()
        for priority in ('critical', 'high', 'normal', 'low'):
            with self.subTest(priority=priority):
                text = plan_text(request, priority=priority)
                metadata = catalog.parse_execplan_text('docs/plans/[G-001] Inputs.md', text)
                if priority == 'critical':
                    verify_input_requests.validate(
                        self.connection, self.repo, text, metadata['sources'],
                        is_new=True, metadata=metadata,
                    )
                else:
                    with self.assertRaises(verify_input_requests.InvalidNewVerifyInputPriority):
                        verify_input_requests.validate(
                            self.connection, self.repo, text, metadata['sources'],
                            is_new=True, metadata=metadata,
                        )

        with self.assertRaises(verify_input_requests.InvalidNewVerifyInputPriority):
            verify_input_requests.validate_new_priority([manager_sources.SOURCE], None)
        with self.assertRaisesRegex(RuntimeError, '새 ExecPlan에는 priority가 필요합니다'):
            catalog.parse_execplan_text(
                'docs/plans/[G-001] Inputs.md', plan_text(request, priority=None),
                require_priority=True,
            )

        invalid = dict(request, id='f' * 64)
        invalid_text = plan_text(invalid, priority='normal')
        invalid_metadata = catalog.parse_execplan_text('docs/plans/[G-001] Inputs.md', invalid_text)
        with self.assertRaisesRegex(RuntimeError, 'unregistered'):
            verify_input_requests.validate(
                self.connection, self.repo, invalid_text, invalid_metadata['sources'],
                is_new=True, metadata=invalid_metadata,
            )
        wrong_scope = plan_text(request, priority='normal', purpose='unrelated product work')
        wrong_scope_metadata = {'sources': [manager_sources.SOURCE], 'priority': 'normal'}
        with self.assertRaisesRegex(RuntimeError, 'Purpose exceeds'):
            verify_input_requests.validate(
                self.connection, self.repo, wrong_scope, wrong_scope_metadata['sources'],
                is_new=True, metadata=wrong_scope_metadata,
            )

    def test_latest_remote_manifest_prevents_old_branch_request(self):
        self.remote_file('scripts/verify-inputs.json', b'{"version":1,"paths":["src/**"]}')
        self.assertEqual(self.instruction(), '')
        self.revision = 'b' * 40
        self.assertEqual(self.instruction(active=SHA), '')
        self.assertEqual(self.git_calls[-1], ('rev-parse', '--verify', 'origin/main^{commit}'))

    def test_invalid_or_symlink_remote_manifest_requests_creation(self):
        for raw, mode in ((b'invalid', b'100644'), (b'{}', b'120000'),
                          (b'{"version":1,"paths":["absent.py"]}', b'100644')):
            self.remote_file('scripts/verify-inputs.json', raw, mode)
            self.assertIn(manager_sources.PREFIX, self.instruction())

    def test_existing_unfinished_pr_and_failed_plan_suppress_duplicate(self):
        for status in ('READY', 'IMPLEMENTING', 'PLAN_BLOCKED', 'PR_OPEN'):
            with self.subTest(status=status):
                self.connection.execute('DELETE FROM exec_plans')
                self.connection.execute('INSERT INTO exec_plans VALUES (?,?,?,?,?)',
                    (self.repo, 'G-001', 'Inputs', json.dumps([manager_sources.SOURCE]), status))
                self.assertEqual(self.instruction(), '')

    def test_remote_draft_and_product_backed_related_plan_suppress_duplicate(self):
        request = self.request()
        text = plan_text(request)
        self.remote_file('docs/plans/[G-001] Inputs.md', text.encode())
        self.assertEqual(self.instruction(), '')
        text = text.replace('  - manager:verify-inputs', '  - docs/PRODUCT.md')
        text = text.replace('priority: normal', 'priority: normal\ndraft: true')
        text += '\n## Acceptance Criteria\n\n- Create scripts/verify-inputs.json\n\n## Draft Decision\nQuestion: choice?\nFacts: known\nOptions: A or B\nImpact: both\n'
        self.remote_file('docs/plans/[G-001] Inputs.md', text.encode())
        self.assertEqual(self.instruction(), '')

    def test_invalid_remote_declaration_defers_only_missing_list_request(self):
        request = {'id': 'f' * 64, 'revision': SHA,
                   'purpose': manager_sources.PURPOSE, 'version': 1}
        invalid = plan_text(request, source='docs/PRODUCT.md').replace('risk: medium', 'risk: unsupported')
        for raw in (invalid.encode(), b'\xff'):
            with self.subTest(raw=raw):
                self.remote_file('docs/plans/[G-001] Inputs.md', raw)
                self.assertEqual(self.instruction(), '')
                self.assertEqual(self.connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='verify_input_requests'"
                ).fetchone()[0], 0)

    def test_git_failure_is_not_absence(self):
        with mock.patch.object(self, 'git_bytes', side_effect=RuntimeError('unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                self.instruction()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='verify_input_requests'").fetchone()[0], 0)

    def test_plan_blob_query_failure_is_not_a_declaration_error(self):
        self.remote_file('docs/plans/[G-001] Inputs.md', b'preserved remote plan')
        original = self.git_bytes
        def unreadable(repo, *args):
            if args[0] == 'cat-file':
                raise RuntimeError('plan blob unavailable')
            return original(repo, *args)
        with mock.patch.object(self, 'git_bytes', side_effect=unreadable):
            with self.assertRaisesRegex(RuntimeError, 'plan blob unavailable'):
                self.instruction()

    def test_unregistered_reserved_unknown_and_scope_mismatch_are_rejected(self):
        request = self.request()
        text = plan_text(request)
        parsed = catalog.parse_execplan_text('docs/plans/[G-001] Inputs.md', text)
        self.assertEqual(parsed['sources'], [manager_sources.SOURCE])
        for candidate in (plan_text(request, source='manager:unknown'),
                          plan_text(request, purpose='Implement unrelated product work'),
                          text.replace('  - manager:verify-inputs', '  - manager:verify-inputs\n  - docs/PRODUCT.md'),
                          text.replace(manager_sources.PREFIX, 'OTHER: ')):
            with self.assertRaises(RuntimeError):
                catalog.parse_execplan_text('docs/plans/[G-001] Inputs.md', candidate)
        invalid = dict(request, id='f' * 64)
        with self.assertRaisesRegex(RuntimeError, 'unregistered'):
            verify_input_requests.validate(self.connection, self.repo, plan_text(invalid), [manager_sources.SOURCE])
        with self.assertRaises(RuntimeError):
            verify_input_requests.validate(self.connection, self.repo, plan_text(dict(request, version=2)), [manager_sources.SOURCE])

    def test_general_planner_rejects_source_without_actual_request(self):
        from tempfile import TemporaryDirectory
        from loop_manager_internal.planning.planner_validation import validate_planner_result
        from loop_manager_internal.planning.planning_catalog import PlannerValidationError
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = 'docs/plans/[G-001] Inputs.md'
            (root / path).parent.mkdir(parents=True)
            request = {'id':'f' * 64, 'revision':SHA, 'purpose':manager_sources.PURPOSE, 'version':1}
            (root / path).write_text(plan_text(request))
            effects = SimpleNamespace(changed_files=lambda *args: [path],
                mutable_snapshot_token=lambda *args: 'token',
                git=lambda *args, **kwargs: SimpleNamespace(stdout='', returncode=0),
                _parse_execplan=catalog.parse_execplan_file)
            with self.assertRaisesRegex(PlannerValidationError, 'unregistered'):
                validate_planner_result(effects, root, SHA, [path], connection=self.connection, repo=self.repo)
            # The upstream effects contract removed contains_guidance_sections.
            # A registered source must reach the next gate with that contract.
            request = verify_input_requests.register(self.connection, self.repo, SHA)
            (root / path).write_text(plan_text(request, priority='critical'))
            with mock.patch.object(execplan_drafts, 'validate_agent_change',
                                   side_effect=RuntimeError('next validation gate')) as next_gate:
                with self.assertRaisesRegex(PlannerValidationError, 'next validation gate'):
                    validate_planner_result(effects, root, SHA, [path], connection=self.connection, repo=self.repo)
            next_gate.assert_called_once()
            (root / path).write_text(plan_text(request, priority='normal'))
            with mock.patch.object(execplan_drafts, 'validate_agent_change') as next_gate:
                with self.assertRaises(PlannerValidationError) as raised:
                    validate_planner_result(effects, root, SHA, [path], connection=self.connection, repo=self.repo)
            self.assertIsInstance(raised.exception.__cause__, verify_input_requests.InvalidNewVerifyInputPriority)
            next_gate.assert_not_called()

    def test_duplicate_request_catalog_is_rejected(self):
        request = self.request()
        with self.assertRaisesRegex(RuntimeError, '중복'):
            catalog.snapshot_from_documents({'docs/plans/[G-001] Inputs.md': plan_text(request),
                'docs/plans/[G-002] Inputs.md': plan_text(request, plan_id='G-002')})

    def test_manager_source_avoids_document_queries_and_retirement(self):
        git = mock.Mock(side_effect=AssertionError('reserved source must not query a file'))
        self.assertEqual(revision_document_or_none(git, Path('.'), SHA, manager_sources.SOURCE),
                         manager_sources.source_content(manager_sources.SOURCE))
        self.assertEqual(stored_requirement_sources([{'plan_id':'G-001', 'sources':json.dumps([manager_sources.SOURCE])}]), ())
        effects = plan_validation.RevisionExecplanSourceFingerprintEffects(
            git=git, git_bytes=git,
            _execplan_input_fingerprint=lambda entries: hashlib.sha256(repr(entries).encode()).hexdigest(),
        )
        first = plan_validation._revision_execplan_source_fingerprint(effects, Path('.'), SHA,
            {'sources':json.dumps([manager_sources.SOURCE])})
        with mock.patch.object(manager_sources, 'RULE', 'Changed rule'):
            self.assertNotEqual(first, plan_validation._revision_execplan_source_fingerprint(effects, Path('.'), SHA,
                {'sources':json.dumps([manager_sources.SOURCE])}))
        git.assert_not_called()

    def test_manager_draft_digest_and_existing_archive_fallback(self):
        digests = execplan_drafts.input_digests(Path('/absent'), {'sources':[manager_sources.SOURCE]})
        self.assertIsNotNone(digests[manager_sources.SOURCE])
        text = plan_text(self.request(), source='docs/requirements/Old.md')
        snapshot = catalog.snapshot_from_documents({'docs/plans/[G-001] Inputs.md':text}, validate_sources=True,
            available_regular_paths=frozenset({'docs/requirements/archive/Old.md'}))
        self.assertIn('G-001', snapshot.active)
        with self.assertRaises(RuntimeError):
            catalog.snapshot_from_documents({'docs/plans/[G-001] Inputs.md':text}, validate_sources=True,
                allow_archive_fallback=False, available_regular_paths=frozenset({'docs/requirements/archive/Old.md'}))

    def test_manual_definition_fingerprints_a_renamed_requirement(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.execution.manual_candidate import ManualCandidate
        from loop_manager_internal.storage.manual_work_state import digest

        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        contents = b"# [R-001] Renamed\n\nCurrent requirement.\n"
        plan_path = "docs/plans/[G-003] Inputs.md"
        plan = {"id": "G-003", "title": "Inputs", "sources": [source],
                "depends_on": [], "complexity": "low", "risk": "medium",
                "priority": "normal"}
        documents = [{"path": plan_path, "text": "plan", "plan": plan}]
        group = {"kind": "start", "documents": json.dumps(documents)}

        def git(_repo, *args, **_kwargs):
            if args == ("rev-parse", "HEAD"):
                return SimpleNamespace(stdout=SHA, returncode=0)
            raise AssertionError(args)

        def git_bytes(_repo, command, *args):
            if command == "ls-tree":
                return f"100644 blob {'1' * 40}\t{renamed}\0".encode()
            if command == "cat-file":
                return contents
            raise AssertionError((command, args))

        candidate = ManualCandidate(self.connection, self.repo, "G-003", git, git_bytes)
        context = SimpleNamespace(worktree=Path("/candidate"))
        with mock.patch.object(candidate, "group", return_value=group), \
             mock.patch.object(candidate, "documents", return_value=documents), \
             mock.patch.object(common_guidance, "plan_content_hash", return_value="plan-guidance"), \
             mock.patch.object(common_guidance, "worker_content_hash", return_value="worker-guidance"):
            snapshot = candidate.definition(context, None, SHA)

        self.assertEqual(snapshot.sources, (source,))
        self.assertEqual(snapshot.source_fingerprint, digest([(source, contents.decode("utf-8"))]))


class ManagerSourceCatalogGateTest(unittest.TestCase):
    def setUp(self):
        from tests.support.catalog import catalog_reconciliation_fixture
        self.fixture = catalog_reconciliation_fixture(self)
        self.connection = self.fixture.connection
        self.request = verify_input_requests.register(self.connection, '/repo', SHA)
        self.documents = {
            'docs/plans/[G-001] Old.md': self.fixture._text('G-001', 'Old'),
            'docs/plans/[G-002] Dependency.md': self.fixture._text('G-002', 'Dependency'),
            'docs/plans/[G-003] Inputs.md': plan_text(self.request, plan_id='G-003', priority='critical'),
        }

    def reconcile(self, isolated=False):
        from tests.support.catalog import FakeDefinitionPort
        from loop_manager_internal.storage import execplan_catalog_state as state
        snapshot = catalog.snapshot_from_documents(self.documents, revision_sha=SHA, isolate_errors=isolated)
        pinned = state.PinnedCatalog(SHA, snapshot, frozenset({'docs/PRODUCT.md'}))
        return state.CatalogReconciliationService(self.fixture.repository, FakeDefinitionPort(pinned)).reconcile('/repo')

    def test_registered_request_can_register_remote_plan(self):
        result = self.reconcile()
        self.assertTrue(result.usable)
        row = self.connection.execute("SELECT sources,status FROM exec_plans WHERE plan_id='G-003'").fetchone()
        self.assertEqual(tuple(row), (json.dumps([manager_sources.SOURCE]), 'READY'))

    def test_new_noncritical_manager_plan_is_withheld_without_row_or_draft(self):
        for priority in ('high', 'normal', 'low'):
            with self.subTest(priority=priority):
                text = plan_text(
                    self.request, plan_id='G-003', priority=priority,
                )
                if priority == 'normal':
                    text = text.replace('priority: normal', 'priority: normal\ndraft: true')
                    text += ('\n## Acceptance Criteria\n\n- Keep the input list complete.\n'
                             '\n## Draft Decision\n\nQuestion: Which inputs are in scope?\n'
                             'Facts: The current list is incomplete.\nOptions: Update or defer.\n'
                             'Impact: The verify input list changes.\n')
                self.documents['docs/plans/[G-003] Inputs.md'] = text
                result = self.reconcile(isolated=True)
                self.assertTrue(result.usable)
                self.assertIn('G-003', result.withheld_plan_ids)
                self.assertIsNone(self.connection.execute(
                    "SELECT 1 FROM exec_plans WHERE repo='/repo' AND plan_id='G-003'"
                ).fetchone())
                self.assertIsNone(self.connection.execute(
                    "SELECT 1 FROM execplan_drafts WHERE repo='/repo' AND plan_id='G-003'"
                ).fetchone())

    def test_existing_normal_manager_plan_and_archive_fallback_are_preserved(self):
        from loop_manager_internal.storage import execplan_catalog_state as state

        path = 'docs/plans/[G-003] Inputs.md'
        current_text = self.documents[path]
        current_plan = catalog.parse_execplan_text(path, current_text)
        current_hash = catalog.plan_content_hash(current_text)
        state.insert_published_plan_row(
            self.connection, '/repo', path, current_plan, current_hash, SHA,
        )
        legacy_text = plan_text(self.request, plan_id='G-003', priority='normal')
        legacy_hash = catalog.plan_content_hash(legacy_text)
        self.connection.execute(
            "UPDATE exec_plans SET priority='normal', content_hash=?, publication_content_hash=? "
            "WHERE repo='/repo' AND plan_id='G-003'",
            (legacy_hash, legacy_hash),
        )
        self.documents[path] = legacy_text
        self.assertTrue(self.reconcile().usable)
        row = self.connection.execute(
            "SELECT priority,status FROM exec_plans WHERE repo='/repo' AND plan_id='G-003'"
        ).fetchone()
        self.assertEqual(tuple(row), ('normal', 'READY'))

        from loop_manager_internal.storage import execplan_draft_state
        draft_text = legacy_text.replace('priority: normal', 'priority: normal\ndraft: true')
        draft_text += ('\n## Acceptance Criteria\n\n- Keep the input list complete.\n'
                       '\n## Draft Decision\n\nQuestion: Which inputs are in scope?\n'
                       'Facts: The current list is incomplete.\nOptions: Update or defer.\n'
                       'Impact: The verify input list changes.\n')
        draft_metadata = catalog.parse_execplan_text(path, draft_text)
        draft_hash = catalog.plan_content_hash(draft_text)
        self.connection.execute(
            "UPDATE exec_plans SET content_hash=?, publication_content_hash=? "
            "WHERE repo='/repo' AND plan_id='G-003'",
            (draft_hash, draft_hash),
        )
        execplan_draft_state.observe(
            self.connection, '/repo', path, draft_metadata, draft_hash, SHA, agent=True,
        )
        self.documents[path] = draft_text
        self.assertTrue(self.reconcile().usable)
        self.assertIn('G-003', execplan_draft_state.pending_ids(self.connection, '/repo'))
        draft_row = self.connection.execute(
            "SELECT priority,status FROM exec_plans WHERE repo='/repo' AND plan_id='G-003'"
        ).fetchone()
        self.assertEqual(tuple(draft_row), ('normal', 'READY'))

        archive_path = 'docs/plans/archive/[G-004] Inputs.md'
        archive_text = plan_text(self.request, plan_id='G-004', priority='normal')
        archived = catalog.snapshot_from_documents({archive_path: archive_text}).archived['G-004']
        state.insert_document_fallback(self.connection, '/repo', archived, SHA)
        archive_row = self.connection.execute(
            "SELECT priority,status FROM exec_plans WHERE repo='/repo' AND plan_id='G-004'"
        ).fetchone()
        self.assertEqual(tuple(archive_row), ('normal', 'MERGED'))

    def test_existing_normal_manager_pr_row_preserves_owned_state(self):
        from loop_manager_internal.storage import execplan_catalog_state as state

        path = 'docs/plans/[G-003] Inputs.md'
        text = self.documents[path]
        metadata = catalog.parse_execplan_text(path, text)
        state.insert_published_plan_row(
            self.connection, '/repo', path, metadata, catalog.plan_content_hash(text), SHA,
        )
        legacy_text = plan_text(self.request, plan_id='G-003', priority='normal')
        legacy_hash = catalog.plan_content_hash(legacy_text)
        self.connection.execute(
            "UPDATE exec_plans SET priority='normal',content_hash=?,publication_content_hash=?,"
            "status='PR_OPEN',branch='feature/old-inputs',worktree='/old-worktree',"
            "pr_url='https://example.invalid/pr/285',worker_session_id='old-session',workspace_owned=1 "
            "WHERE repo='/repo' AND plan_id='G-003'",
            (legacy_hash, legacy_hash),
        )
        self.documents[path] = legacy_text

        self.assertTrue(self.reconcile().usable)
        row = self.connection.execute(
            "SELECT priority,status,branch,worktree,pr_url,worker_session_id,workspace_owned "
            "FROM exec_plans WHERE repo='/repo' AND plan_id='G-003'"
        ).fetchone()
        self.assertEqual(tuple(row), (
            'normal', 'PR_OPEN', 'feature/old-inputs', '/old-worktree',
            'https://example.invalid/pr/285', 'old-session', 1,
        ))

    def test_unregistered_request_is_held_without_registering(self):
        self.connection.execute('DELETE FROM verify_input_requests')
        result = self.reconcile(isolated=True)
        self.assertTrue(result.usable)
        self.assertIn('G-003', result.withheld_plan_ids)
        self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-003'").fetchone())

    def test_unregistered_request_strict_catalog_is_rejected(self):
        self.connection.execute('DELETE FROM verify_input_requests')
        result = self.reconcile()
        self.assertFalse(result.usable)
        self.assertEqual(result.diagnostic.code, 'unregistered-manager-source')


class VerifyInputPlanningPromptTest(unittest.TestCase):
    setUp = VerifyInputRequestTest.setUp
    git = VerifyInputRequestTest.git
    git_bytes = VerifyInputRequestTest.git_bytes
    remote_file = VerifyInputRequestTest.remote_file

    def test_isolated_remote_plan_error_preserves_general_and_maintenance_prompts(self):
        from loop_manager_internal.planning.validation_ports import _PlanningValidationPorts
        from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
        self.connection.execute('CREATE TABLE projects (repo TEXT, base_branch TEXT)')
        self.connection.execute('INSERT INTO projects VALUES (?, ?)', (self.repo, 'main'))
        request = {'id': 'f' * 64, 'revision': SHA,
                   'purpose': manager_sources.PURPOSE, 'version': 1}
        invalid = plan_text(request, source='docs/PRODUCT.md').replace('risk: medium', 'risk: unsupported')
        self.remote_file('docs/plans/[G-001] Inputs.md', invalid.encode())
        effects = SimpleNamespace(planner_prompt=lambda *args: 'normal planning',
            is_loop_manager_repository=lambda repo: False, git=self.git, git_bytes=self.git_bytes,
            maintenance_planner_prompt=lambda *args, **kwargs: 'maintenance planning')
        port = _PlanningValidationPorts(effects, self.connection)
        snapshot = MutableReadySnapshot(self.repo, SHA, (), 'token')
        self.assertEqual(port.planner_prompt(None, 'isolated catalog', snapshot), 'normal planning')
        context = SimpleNamespace(repo=self.repo, active_sha=SHA)
        self.assertEqual(port.maintenance_prompt(context, 'isolated catalog', '', None,
                                                include_general_planning=True), 'maintenance planning')
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='verify_input_requests'"
        ).fetchone()[0], 0)

    def test_general_and_combined_planning_register_request_but_maintenance_only_does_not(self):
        from loop_manager_internal.planning.validation_ports import _PlanningValidationPorts
        from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
        self.connection.execute('CREATE TABLE projects (repo TEXT, base_branch TEXT)')
        self.connection.execute('INSERT INTO projects VALUES (?, ?)', (self.repo, 'main'))
        effects = SimpleNamespace(planner_prompt=lambda *args: 'normal planning',
            is_loop_manager_repository=lambda repo: False, git=self.git, git_bytes=self.git_bytes,
            maintenance_planner_prompt=lambda *args, **kwargs: 'maintenance planning')
        port = _PlanningValidationPorts(effects, self.connection)
        snapshot = MutableReadySnapshot(self.repo, SHA, (), 'token')
        self.assertIn(manager_sources.PREFIX, port.planner_prompt(None, 'catalog', snapshot))
        self.assertIn('priority는 critical', port.planner_prompt(None, 'catalog', snapshot))
        context = SimpleNamespace(repo=self.repo, active_sha=SHA)
        calls = len(self.git_calls)
        self.assertEqual(port.maintenance_prompt(context, 'catalog', '', None, include_general_planning=False),
                         'maintenance planning')
        self.assertEqual(len(self.git_calls), calls)
        self.assertIn(manager_sources.PREFIX, port.maintenance_prompt(context, 'catalog', '', None,
                                                                    include_general_planning=True))
        self.assertIn('priority는 critical', port.maintenance_prompt(
            context, 'catalog', '', None, include_general_planning=True,
        ))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM verify_input_requests').fetchone()[0], 1)


class ManagerSourceManualDefinitionTest(unittest.TestCase):
    setUp = ManagerSourceCatalogGateTest.setUp

    def test_manual_completion_definition_uses_registered_rules_without_source_file_query(self):
        from loop_manager_internal.execution.manual_candidate import ManualCandidate
        from loop_manager_internal.storage.manual_work_state import digest
        path = 'docs/plans/[G-003] Inputs.md'
        text = self.documents[path]
        plan = catalog.parse_execplan_text(path, text)
        documents = [{'path': path, 'text': text, 'plan': plan}]
        calls = []
        def git(repo, *args, **kwargs):
            calls.append(args)
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout=SHA, returncode=0)
            if args == ('show', f'{SHA}:{path}'):
                return SimpleNamespace(stdout=text, returncode=0)
            raise AssertionError('unexpected Git file query: ' + repr(args))
        candidate = ManualCandidate(self.connection, '/repo', 'G-003', git,
            lambda *args: f'100644 blob oid\t{path}\0'.encode())
        group = {'kind': 'start', 'state': 'AUTOMATIC', 'request_id': 'manual-completion',
                 'documents': json.dumps(documents)}
        context = SimpleNamespace(worktree=Path('/candidate'))
        with mock.patch.object(candidate, 'group', return_value=group):
            snapshot = candidate.definition(context, None, SHA)
            self.assertEqual(snapshot.sources, (manager_sources.SOURCE,))
            self.assertEqual(snapshot.source_fingerprint,
                             digest([(manager_sources.SOURCE, manager_sources.source_content(manager_sources.SOURCE))]))
            self.connection.execute('DELETE FROM verify_input_requests')
            with self.assertRaisesRegex(RuntimeError, 'unregistered'):
                candidate.definition(context, None, SHA)
        self.assertFalse(any(manager_sources.SOURCE in arg for args in calls for arg in args))


class ManagerSourceFinalReviewTest(unittest.TestCase):
    setUp = VerifyInputRequestTest.setUp

    def test_final_followup_validates_registered_manager_request_and_keeps_document_gates(self):
        from tempfile import TemporaryDirectory
        from loop_manager_internal.planning.specification_review import validate_final
        from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
        from loop_manager_internal.planning.validation_ports import _PlanningValidationPorts
        request = verify_input_requests.register(self.connection, self.repo, SHA)
        attempt = {'revision': SHA, 'input_fingerprint': 'input',
                   'specification_fingerprint': 'specification', 'work_cycle': 'cycle'}
        evidence = ['docs/PRODUCT.md', 'docs/ARCHITECTURE.md', 'src/app.py']
        value = dict(attempt, outcome='followup', reason='Create missing verify inputs', evidence=evidence)
        def git_run(repo, *args, **kwargs):
            if '--name-only' in args:
                return SimpleNamespace(stdout='docs/PRODUCT.md\0docs/ARCHITECTURE.md\0')
            paths = args[args.index('--') + 1:]
            self.assertNotIn(manager_sources.SOURCE, paths)
            return SimpleNamespace(stdout=''.join(f'100644 blob oid\t{path}\0' for path in paths))
        git = SimpleNamespace(run=git_run)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = 'docs/plans/[G-001] Inputs.md'
            (root / path).parent.mkdir(parents=True)
            text = plan_text(request)
            (root / path).write_text(text)
            plan = catalog.parse_execplan_text(path, text)
            batch = SimpleNamespace(plans=((path, plan),), revisions=(), deletions=(), restoration_moves=())
            port = _PlanningValidationPorts(SimpleNamespace(), self.connection)
            validator = lambda path, plan: port.validate_manager_source(self.repo, root, path, plan)
            def validate():
                return validate_final(attempt, 'FINAL_SPECIFICATION_REVIEW: ' + json.dumps(value),
                    batch, git, root, manager_source_validator=validator)
            self.assertEqual(validate(), 'followup')
            plan['draft'] = True
            value['outcome'] = 'needs_decision'
            self.assertEqual(validate(), 'needs_decision')
            value['outcome'] = 'followup'
            for sources in ([], ['manager:unknown'], [manager_sources.SOURCE, 'docs/PRODUCT.md'],
                            ['docs/requirements/Missing.md']):
                plan['sources'] = sources
                with self.assertRaises(PlanningResultInvalid):
                    validate()
            plan['sources'] = [manager_sources.SOURCE]
            with self.assertRaises(PlanningResultInvalid):
                validate_final(attempt, 'FINAL_SPECIFICATION_REVIEW: ' + json.dumps(value), batch, git, root)
            self.connection.execute('DELETE FROM verify_input_requests')
            with self.assertRaisesRegex(PlanningResultInvalid, 'unregistered'):
                validate()
