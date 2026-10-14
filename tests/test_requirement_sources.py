import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from loop_manager_internal.catalog import execplan_catalog, requirement_sources
from loop_manager_internal.catalog import execplan_drafts
from loop_manager_internal.execution.plan_validation import (
    RevisionExecplanSourceFingerprintEffects,
    WorktreeExecplanInputFingerprintEffects,
    _revision_execplan_source_fingerprint,
    _worktree_execplan_input_fingerprint,
)
from tests.support.catalog import execplan_header


def requirement(requirement_id: str, title: str) -> str:
    return f"# [{requirement_id}] {title}\n\nRequirement body.\n"


class RequirementSourceResolutionTest(unittest.TestCase):
    def test_exact_path_wins_when_the_original_id_is_duplicated(self):
        source = "docs/requirements/[R-001] Original.md"
        exact = requirement("R-001", "Original")
        result = requirement_sources.resolve_requirement_source(
            source,
            {
                source: exact,
                "docs/requirements/[R-001] Renamed.md": requirement("R-001", "Renamed"),
            },
        )
        self.assertEqual(result.status, requirement_sources.ResolutionStatus.RESOLVED)
        self.assertEqual(result.path, source)
        self.assertEqual(result.contents, exact)

    def test_same_name_archive_precedes_original_id_candidates(self):
        source = "docs/requirements/[R-001] Original.md"
        archive = "docs/requirements/archive/[R-001] Original.md"
        result = requirement_sources.resolve_requirement_source(
            source,
            {
                archive: "# legacy content without an ID\n",
                "docs/requirements/[R-001] Renamed.md": requirement("R-001", "Renamed"),
            },
        )
        self.assertEqual(result.path, archive)

    def test_unique_original_id_resolves_a_renamed_requirement(self):
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] New title.md"
        result = requirement_sources.resolve_requirement_source(
            source, {renamed: requirement("R-001", "New title")},
        )
        self.assertEqual(result.status, requirement_sources.ResolutionStatus.RESOLVED)
        self.assertEqual(result.path, renamed)

    def test_unique_id_uses_shared_frontmatter_identity(self):
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        result = requirement_sources.resolve_requirement_source(source, {
            renamed: "---\nid: R-001\ntitle: Renamed\n---\n\nBody.\n",
        })
        self.assertEqual(result.status, requirement_sources.ResolutionStatus.RESOLVED)
        self.assertEqual(result.path, renamed)

    def test_duplicate_archived_id_candidates_are_ambiguous(self):
        source = "docs/requirements/[R-001] Original.md"
        candidates = {
            "docs/requirements/archive/[R-001] First.md": requirement("R-001", "First"),
            "docs/requirements/archive/[R-001] Second.md": requirement("R-001", "Second"),
        }
        result = requirement_sources.resolve_requirement_source(source, candidates)
        self.assertEqual(result.status, requirement_sources.ResolutionStatus.AMBIGUOUS)
        self.assertEqual(result.candidates, tuple(sorted(candidates)))

    def test_multiple_id_candidates_are_ambiguous_and_missing_id_stays_missing(self):
        source = "docs/requirements/[R-001] Original.md"
        candidates = {
            "docs/requirements/[R-001] First.md": requirement("R-001", "First"),
            "docs/requirements/[R-001] Second.md": requirement("R-001", "Second"),
        }
        ambiguous = requirement_sources.resolve_requirement_source(source, candidates)
        self.assertEqual(ambiguous.status, requirement_sources.ResolutionStatus.AMBIGUOUS)
        self.assertEqual(ambiguous.candidates, tuple(sorted(candidates)))

        unidentifiable = requirement_sources.resolve_requirement_source(
            "docs/requirements/Legacy.md", candidates,
        )
        self.assertEqual(unidentifiable.status, requirement_sources.ResolutionStatus.MISSING)

    def test_invalid_candidate_identity_is_a_lookup_failure(self):
        with self.assertRaises(requirement_sources.RequirementSourceLookupError):
            requirement_sources.resolve_requirement_source(
                "docs/requirements/[R-001] Original.md",
                {"docs/requirements/Unrelated.md": "# no source ID\n"},
            )

    def test_worktree_lookup_rejects_requirement_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / "docs/requirements"
            requirements.mkdir(parents=True)
            target = root / "target.md"
            target.write_text("# [R-001] Target\n", encoding="utf-8")
            (requirements / "[R-001] Link.md").symlink_to(target)
            with self.assertRaises(requirement_sources.RequirementSourceLookupError):
                requirement_sources.worktree_requirement_documents(root)


class ExecPlanRequirementSourceCatalogTest(unittest.TestCase):
    def snapshot(self, source, requirement_documents, *, allow_archive_fallback=True):
        path = "docs/plans/[G-231] Source consumer.md"
        text = (
            f"{execplan_header('G-231', 'Source consumer', sources=(source,))}\n\n"
            "## Purpose\n\nTest source resolution.\n"
        )
        return execplan_catalog.snapshot_from_documents(
            {path: text}, validate_sources=True,
            allow_archive_fallback=allow_archive_fallback,
            requirement_documents=requirement_documents,
        )

    def test_existing_plan_source_survives_requirement_rename(self):
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        snapshot = self.snapshot(
            source, {renamed: requirement("R-001", "Renamed")},
        )
        self.assertIn("G-231", snapshot.active)
        self.assertEqual(snapshot.active["G-231"].sources, (source,))

    def test_worker_source_rename_requires_official_catalog_adoption_and_keeps_body(self):
        from loop_manager_internal.execution import plan_validation
        from loop_manager_internal.application.contracts import ExecPlanMetadataError

        path = "docs/plans/[G-236] 전체와 프로젝트 pause resume의 보존 경계.md"
        old_sources = (
            "docs/requirements/[R-076] Read-Only Operational State CLI.md",
            "docs/requirements/[R-077] Manual Plan Work Start and Completion.md",
        )
        actual_sources = (
            "docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md",
            "docs/requirements/[R-077] G 계획 수동 구현과 R·코드 일괄 제출.md",
        )
        documents = {name: requirement(identifier, name) for name, identifier in
                     zip(actual_sources, ("R-076", "R-077"))}
        body = "\n## Progress\n- 구현·문서·테스트는 checkpoint에 보존됨. 전체 verify 미확인.\n"
        text = execplan_header("G-236", "전체와 프로젝트 pause resume의 보존 경계",
                              sources=old_sources) + body
        def parse(_root, relative):
            return execplan_catalog.parse_execplan_text(
                relative, text, requirement_documents=documents)
        stored = {"plan_id": "G-236", "path": path, "title": "전체와 프로젝트 pause resume의 보존 경계",
                  "sources": json.dumps(old_sources), "depends_on": "[]", "priority": "normal",
                  "current_complexity": "low", "current_risk": "low"}
        effects = plan_validation.ValidateActiveExecplanMetadataEffects(
            validate_execplan=parse,
            _current_execplan_metadata=plan_validation._current_execplan_metadata,
            _execplan_metadata_identity_error=lambda *args: plan_validation._execplan_metadata_identity_error(
                plan_validation.ExecplanMetadataIdentityErrorEffects(
                    _current_execplan_metadata=plan_validation._current_execplan_metadata), *args))
        validate = lambda: plan_validation.validate_active_execplan_metadata(
            effects, Path("/repo"), Path("/worktree"), path, stored)
        self.assertEqual(validate()["sources"], list(old_sources))
        for old, actual in zip(old_sources, actual_sources):
            text = text.replace(old, actual)
        checkpoint_text = text
        with self.assertRaisesRegex(ExecPlanMetadataError, "불일치 필드: sources"):
            validate()
        self.assertEqual(json.loads(stored["sources"]), list(old_sources))
        stored["sources"] = json.dumps(actual_sources)
        self.assertEqual(validate()["sources"], list(actual_sources))
        self.assertEqual(text, checkpoint_text)
        self.assertTrue(text.endswith(body))

        current = parse(None, path)
        for field, value in (("title", "변경된 제목"), ("sources", list(old_sources)),
                             ("depends_on", ["G-235"]), ("complexity", "high"),
                             ("risk", "critical"), ("priority", "high")):
            with self.subTest(field=field):
                changed = {**current, field: value}
                error = effects._execplan_metadata_identity_error(Path("/repo"), changed, stored, "활성 ExecPlan")
                self.assertIn("불일치 필드: " + field, error)

    def test_ambiguous_id_is_reported_separately_from_missing_source(self):
        source = "docs/requirements/[R-001] Original.md"
        with self.assertRaises(execplan_catalog.ExecPlanCatalogError) as caught:
            self.snapshot(source, {
                "docs/requirements/[R-001] First.md": requirement("R-001", "First"),
                "docs/requirements/[R-001] Second.md": requirement("R-001", "Second"),
            })
        self.assertEqual(caught.exception.code, "ambiguous-source")

        with self.assertRaises(execplan_catalog.ExecPlanCatalogError) as missing:
            self.snapshot("docs/requirements/Legacy.md", {})
        self.assertEqual(missing.exception.code, "missing-source")

        path = "docs/plans/[G-231] Source consumer.md"
        text = (
            f"{execplan_header('G-231', 'Source consumer', sources=(source,))}\n\n"
            "## Purpose\n\nTest lookup failure.\n"
        )
        invalid = execplan_catalog.snapshot_from_documents(
            {path: text}, validate_sources=True,
            requirement_documents={"docs/requirements/Unrelated.md": "# no source ID\n"},
            isolate_errors=True,
        )
        self.assertEqual(invalid.issues[0].code, "source-lookup-failed")

    def test_new_plan_does_not_accept_an_archived_source(self):
        source = "docs/requirements/[R-001] Original.md"
        archive = "docs/requirements/archive/[R-001] Original.md"
        with self.assertRaises(execplan_catalog.ExecPlanCatalogError) as caught:
            self.snapshot(
                source, {archive: "# legacy requirement\n"},
                allow_archive_fallback=False,
            )
        self.assertEqual(caught.exception.code, "missing-source")

    def test_new_plan_requires_the_exact_active_source_path(self):
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        with self.assertRaises(execplan_catalog.ExecPlanCatalogError) as caught:
            self.snapshot(
                source, {renamed: requirement("R-001", "Renamed")},
                allow_archive_fallback=False,
            )
        self.assertEqual(caught.exception.code, "missing-source")

        exact = self.snapshot(
            source, {source: requirement("R-001", "Original")},
            allow_archive_fallback=False,
        )
        self.assertIn("G-231", exact.active)


class RequirementSourceFingerprintTest(unittest.TestCase):
    def test_draft_input_digest_tracks_renamed_requirement_contents(self):
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        contents = b"# [R-001] Renamed\n\nUpdated content.\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / renamed).parent.mkdir(parents=True)
            (root / renamed).write_bytes(contents)
            observed = execplan_drafts.input_digests(root, {"sources": [source]})
        self.assertEqual(observed[source], execplan_drafts.content_digest(contents))

    def test_worktree_and_revision_fingerprints_read_the_renamed_document(self):
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        contents = requirement("R-001", "Renamed")
        plan = {"path": "docs/plans/[G-231] Source consumer.md",
                "sources": json.dumps([source])}
        observed = []
        worktree_effects = WorktreeExecplanInputFingerprintEffects(
            _execplan_input_fingerprint=lambda entries: observed.extend(entries) or "worktree",
            _execplan_input_paths=lambda _plan: [plan["path"], source],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / plan["path"]).parent.mkdir(parents=True)
            (root / plan["path"]).write_text("plan", encoding="utf-8")
            (root / renamed).parent.mkdir(parents=True)
            (root / renamed).write_text(contents, encoding="utf-8")
            self.assertEqual(
                _worktree_execplan_input_fingerprint(worktree_effects, root, plan),
                "worktree",
            )
        self.assertIn((source, contents), observed)

        oid = "1" * 40
        git_bytes_calls = []

        def git_bytes(_repo, command, *args):
            git_bytes_calls.append((command, args))
            if command == "ls-tree":
                return f"100644 blob {oid}\t{renamed}\0".encode()
            if command == "cat-file":
                return contents.encode()
            raise AssertionError((command, args))

        revision_entries = []
        revision_effects = RevisionExecplanSourceFingerprintEffects(
            _execplan_input_fingerprint=lambda entries: revision_entries.extend(entries) or "revision",
            git=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""),
            git_bytes=git_bytes,
        )
        self.assertEqual(
            _revision_execplan_source_fingerprint(
                revision_effects, Path("/repo"), "base", plan,
            ),
            "revision",
        )
        self.assertEqual(revision_entries, [(source, contents)])
        self.assertEqual([call[0] for call in git_bytes_calls], ["ls-tree", "cat-file"])


if __name__ == "__main__":
    unittest.main()


class WorkerFrontmatterRestoreTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from loop_manager_internal.execution import plan_validation
        self.validation = plan_validation
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = 'docs/plans/[G-236] Plan.md'
        self.target = self.root / self.path
        self.target.parent.mkdir(parents=True)
        self.original = (execplan_header('G-236', 'Plan') + '\n## Progress\nOriginal body.\n').encode()
        self.body = '\r\n## Progress\r\n구현·테스트 기록 보존\r\n---\r\n'.encode()
        self.changed_header = self.original.split(b'---', 2)[1].replace(b'title: Plan', b'title: Changed')
        self.candidate = b'---' + self.changed_header + b'---\n' + self.body
        self.target.write_bytes(self.candidate)
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute('CREATE TABLE exec_plans (repo TEXT, plan_id TEXT, agent_input_plan TEXT)')
        self.connection.execute('INSERT INTO exec_plans VALUES (?, ?, ?)', ('repo', 'G-236', self.original.decode()))
        self.connection.commit()
        self.plan = {'path': self.path, 'plan_id': 'G-236', 'title': 'Plan',
                     'sources': '["docs/PRODUCT.md"]', 'depends_on': '[]', 'priority': 'normal',
                     'current_complexity': 'low', 'current_risk': 'low'}

    def restore(self):
        return self.validation.restore_worker_frontmatter(
            self.root, self.path, self.plan, repo='repo', revision='a' * 40,
            connection=self.connection, git_bytes=lambda *_: self.original)

    def test_only_frontmatter_is_restored_and_body_bytes_and_audit_are_preserved(self):
        import base64
        import hashlib
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        self.assertTrue(self.restore())
        header = self.original.split(b'---', 2)[0] + b'---' + self.original.split(b'---', 2)[1] + b'---\n'
        self.assertEqual(self.target.read_bytes(), header + self.body)
        records = ExecutionCheckpointRepository(self.connection).get('repo', 'G-236').snapshot['worker_frontmatter_restores']
        self.assertEqual(len(records), 1)
        self.assertEqual(base64.b64decode(records[0]['before']), self.candidate[:-len(self.body)])
        self.assertEqual(records[0]['body_sha256'], hashlib.sha256(self.body).hexdigest())
        self.assertFalse(self.restore())
        self.assertEqual(len(ExecutionCheckpointRepository(self.connection).get('repo', 'G-236').snapshot['worker_frontmatter_restores']), 1)
        checkpoints = ExecutionCheckpointRepository(self.connection)
        checkpoint = checkpoints.get('repo', 'G-236')
        checkpoints.record('repo', 'G-236', expected_generation=checkpoint.generation, operation=None,
                           expected_head='next', target_base_oid=None, observed_remote_oid=None,
                           snapshot={'next_stage': 'keep'})
        self.assertEqual(checkpoints.get('repo', 'G-236').snapshot['worker_frontmatter_restores'], records)

    def test_unsafe_boundaries_prior_changes_and_owned_operations_do_not_overwrite(self):
        from loop_manager_internal.application.contracts import ExecPlanMetadataError
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        damaged = b'---\nid: G-236\n## body without delimiter\n'
        self.target.write_bytes(damaged)
        with self.assertRaises(ExecPlanMetadataError):
            self.restore()
        self.assertEqual(self.target.read_bytes(), damaged)
        self.target.write_bytes(self.candidate)
        self.connection.execute('UPDATE exec_plans SET agent_input_plan = ?', (self.candidate.decode(),))
        self.assertFalse(self.restore())
        self.assertEqual(self.target.read_bytes(), self.candidate)
        changed_again = self.candidate.replace(b'title: Changed', b'title: ChangedAgain')
        self.target.write_bytes(changed_again)
        with self.assertRaisesRegex(RuntimeError, '시작 이전'):
            self.restore()
        self.assertEqual(self.target.read_bytes(), changed_again)
        self.target.write_bytes(self.candidate)
        self.connection.execute('UPDATE exec_plans SET agent_input_plan = ?', (self.original.decode(),))
        checkpoints = ExecutionCheckpointRepository(self.connection)
        checkpoints.record('repo', 'G-236', expected_generation=0, operation='rebase', expected_head='head',
                           target_base_oid='base', observed_remote_oid='base', snapshot={'existing': 'keep'})
        with self.assertRaisesRegex(RuntimeError, 'Git operation'):
            self.restore()
        self.assertEqual(self.target.read_bytes(), self.candidate)
        self.assertEqual(checkpoints.get('repo', 'G-236').snapshot, {'existing': 'keep'})

    def test_edit_during_atomic_write_is_preserved_and_temporary_is_removed(self):
        from unittest import mock
        original_chmod = self.validation.os.chmod
        external = b'external edit must survive\n'
        def edit(temporary, mode):
            original_chmod(temporary, mode)
            self.target.write_bytes(external)
        with mock.patch.object(self.validation.os, 'chmod', side_effect=edit):
            with self.assertRaisesRegex(RuntimeError, '다시 변경'):
                self.restore()
        self.assertEqual(self.target.read_bytes(), external)
        self.assertFalse(list(self.target.parent.glob('.frontmatter-*')))
