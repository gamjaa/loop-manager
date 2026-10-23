import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager
from loop_manager_internal.execution import plan_validation
from loop_manager_internal.git.command_adapter import ExternalObservationError, revision_document_or_none
from loop_manager_internal.planning.planner_validation import (
    FutureIdeaItemsEffects, _future_idea_items, validate_planner_result,
    _ready_plan_snapshot_at_revision,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.runtime.contracts import GitLockError
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, isolated_git_test
from tests.support.worker import dummy_worker_sessions


class RevisionDocumentObservationTests(unittest.TestCase):
    def test_body_hash_confirmed_absence_retains_input_error(self):
        git = mock.Mock(side_effect=[RuntimeError('missing'), SimpleNamespace(returncode=0, stdout='')])
        with self.assertRaises(RuntimeError) as raised:
            plan_validation._revision_execplan_body_hash(
                plan_validation.RevisionExecplanBodyHashEffects(git=git),
                Path('/repo'), 'base', 'docs/plans/plan.md',
            )
        self.assertNotIsInstance(raised.exception, ExternalObservationError)

    def test_review_fingerprint_requires_confirmed_observation(self):
        path = 'docs/PRODUCT.md'
        for tree, expected in (
            (SimpleNamespace(returncode=0, stdout=''), 'absent'),
            (SimpleNamespace(returncode=0, stdout=f'100644 blob abc\t{path}\0'), 'error'),
            (SimpleNamespace(returncode=128, stdout='unavailable'), 'error'),
        ):
            for failure in (RuntimeError('different diagnostic'), SimpleNamespace(returncode=128, stdout='failed')):
                with self.subTest(tree=tree, failure=failure):
                    fingerprint = mock.Mock(return_value='absent')
                    effects = plan_validation.RevisionReviewIssueFingerprintEffects(
                        _execplan_input_fingerprint=fingerprint,
                        _review_issue_paths=lambda result: [path],
                        git=mock.Mock(side_effect=[failure, tree]),
                    )
                    if expected == 'error':
                        with self.assertRaises(ExternalObservationError):
                            plan_validation._revision_review_issue_fingerprint(effects, Path('/repo'), 'base', {})
                        fingerprint.assert_not_called()
                    else:
                        self.assertEqual(plan_validation._revision_review_issue_fingerprint(effects, Path('/repo'), 'base', {}), 'absent')
                        fingerprint.assert_called_once_with([(path, None)])

    def test_review_fingerprint_success_preserves_contents(self):
        effects = plan_validation.RevisionReviewIssueFingerprintEffects(
            _execplan_input_fingerprint=plan_validation._execplan_input_fingerprint,
            _review_issue_paths=lambda result: ['docs/PRODUCT.md'],
            git=mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='unchanged')),
        )
        self.assertEqual(
            plan_validation._revision_review_issue_fingerprint(effects, Path('/repo'), 'base', {}),
            plan_validation._execplan_input_fingerprint([('docs/PRODUCT.md', 'unchanged')]),
        )
        effects.git.assert_called_once()

    def test_git_lock_keeps_its_retry_category(self):
        git = mock.Mock(side_effect=GitLockError('different lock text'))
        with self.assertRaises(GitLockError):
            revision_document_or_none(git, Path('/repo'), 'base', 'docs/PRODUCT.md')
        self.assertEqual(git.call_count, 1)

    def test_failed_show_requires_confirmed_tree_absence(self):
        calls = []
        def git(_repo, operation, *args, **kwargs):
            calls.append(operation)
            if operation == "show":
                return SimpleNamespace(returncode=128, stdout="changed diagnostic")
            return SimpleNamespace(returncode=0, stdout="100644 blob deadbeef\tdocs/FUTURE_IDEAS.md\0")

        with self.assertRaises(ExternalObservationError):
            revision_document_or_none(git, Path("/repo"), "base", "docs/FUTURE_IDEAS.md")
        self.assertEqual(calls, ["show", "ls-tree"])

    def test_confirmed_absence_does_not_invent_previous_future_ideas(self):
        def git(_repo, operation, *args, **kwargs):
            return SimpleNamespace(returncode=128 if operation == "show" else 0, stdout="")
        effects = FutureIdeaItemsEffects(
            _future_idea_items_from_text=lambda value: ((value,),) if value else (),
            git=git,
        )
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(_future_idea_items(effects, Path(directory), "base"), ())

    def test_future_idea_file_read_error_remains_operational(self):
        effects = FutureIdeaItemsEffects(
            _future_idea_items_from_text=lambda value: (), git=mock.Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'docs/FUTURE_IDEAS.md'
            path.parent.mkdir()
            path.write_text('idea', encoding='utf-8')
            with mock.patch.object(Path, 'read_text', side_effect=OSError('unreadable')):
                with self.assertRaises(ExternalObservationError):
                    _future_idea_items(effects, Path(directory), 'base')
        effects.git.assert_not_called()

    def test_planner_tree_failure_does_not_become_agent_correction(self):
        effects = SimpleNamespace(
            changed_files=lambda *args: [],
            mutable_snapshot_token=lambda *args: 'snapshot',
            git=mock.Mock(side_effect=RuntimeError('different failure text')),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ExternalObservationError):
                validate_planner_result(
                    effects, Path(directory), 'base',
                    ['docs/plans/[G-001] Proposed.md'],
                )

    def test_ready_revision_read_failure_remains_operational(self):
        effects = SimpleNamespace(git_bytes=mock.Mock(side_effect=RuntimeError('Git lock')))
        with self.assertRaises(ExternalObservationError):
            _ready_plan_snapshot_at_revision(
                effects, Path('/repo'), 'base', {'path': 'docs/plans/plan.md'}
            )

    def test_confirmed_missing_ready_revision_keeps_safety_decision(self):
        git_bytes = mock.Mock(side_effect=[RuntimeError('missing'), b''])
        effects = SimpleNamespace(git_bytes=git_bytes)
        with self.assertRaises(MaintenancePlanningError):
            _ready_plan_snapshot_at_revision(
                effects, Path('/repo'), 'base', {'path': 'docs/plans/plan.md'}
            )


@isolated_git_test
class PlannerValidationRecoveryTest(unittest.TestCase):
    @dummy_worker_sessions
    def test_first_planner_validation_retry_resumes_session_and_clears_state(self):
        planner_calls = []

        def agent(worktree, _state, _config, role_name, prompt, **kwargs):
            if role_name == "planner":
                planner_calls.append((kwargs["session_id"], prompt))
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                if len(planner_calls) == 1:
                    write_execplan(
                        worktree,
                        "G-001",
                        "Generated",
                        depends_on=("G-999",),
                    )
                else:
                    write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                first = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(first["planning_retry_count"], 0)
                self.assertEqual(first["planner_session_id"], "planner-session")
                self.assertIn("존재하지 않는 depends_on ID", first["planning_validation_error"])
                self.assertIsNone(first["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            final = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls[0][0], None)
            self.assertEqual(planner_calls[1][0], "planner-session")
            self.assertIn(first["planning_validation_error"], planner_calls[1][1])
            self.assertEqual(final["phase"], "WORKING")
            self.assertEqual(final["planning_retry_count"], 0)
            self.assertIsNone(final["planner_session_id"])
            self.assertIsNone(final["planning_validation_error"])
            self.assertIsNone(final["planning_error"])

    @dummy_worker_sessions
    def test_interrupted_first_planner_with_result_resumes_before_validation(self):
        planner_sessions = []
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            nonlocal planner_calls
            if role_name != "planner":
                return loop_manager.CodexResult(1, "stop after planning")
            planner_calls += 1
            planner_sessions.append(kwargs["session_id"])
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Generated")
            if planner_calls == 1:
                # The CLI starts a session and changes the worktree, then the
                # process dies before it can report a completed result.
                kwargs['on_session_started']('planner-session')
                loop_manager.update_project(
                    connection,
                    str(repo),
                    planner_session_id="planner-session",
                )
                raise RuntimeError("process interrupted after Planner edit")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch("loop_manager.run_codex", side_effect=agent):
                with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertEqual(interrupted["planning_result_completed"], 0)
                self.assertEqual(interrupted["planner_session_id"], "planner-session")
                self.assertTrue(
                    (Path(interrupted["worktree"]) / "docs/plans/[G-001] Generated.md").is_file()
                )

                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_sessions, [None, "planner-session"])
            self.assertEqual(resumed["phase"], "WORKING")

    @dummy_worker_sessions
    def test_interrupted_planner_correction_before_edit_resumes_with_invalid_diff(self):
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            nonlocal planner_calls
            self.assertEqual(role_name, "planner")
            planner_calls += 1
            if planner_calls == 1:
                (worktree / "unexpected.txt").write_text(
                    "invalid\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(0, "", "planner-session")
            if planner_calls == 2:
                saved = connection.execute(
                    "SELECT planning_result_pending_validation FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(saved["planning_result_pending_validation"], 1)
                raise RuntimeError("process interrupted before Planner correction")
            self.assertEqual(_kwargs["session_id"], "planner-session")
            return loop_manager.CodexResult(1, "planner timeout", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertEqual(interrupted["planning_retry_count"], 0)
                self.assertTrue((Path(interrupted["worktree"]) / "unexpected.txt").is_file())
                plan_sha = git_command(repo, "rev-parse", "origin/main")
                git_command(
                    Path(directory) / "remote.git",
                    "update-ref",
                    "refs/heads/planner-correction-update",
                    plan_sha,
                )

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 6)
            self.assertEqual(resumed["planning_retry_count"], 0)
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_error"])
            self.assertIsNotNone(resumed["planning_validation_error"])
            self.assertIn(
                loop_manager.TRANSIENT_AGENT_ERROR_PREFIX,
                resumed["last_error"],
            )
            self.assertEqual(
                git_command(
                    repo,
                    "rev-parse",
                    "refs/remotes/origin/planner-correction-update",
                ),
                plan_sha,
            )

    @dummy_worker_sessions
    def test_planner_cli_failure_does_not_consume_validation_retry(self):
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            nonlocal planner_calls
            if role_name != "planner":
                return loop_manager.CodexResult(1, "stop after planning")
            planner_calls += 1
            invalid = worktree / "unexpected.txt"
            if planner_calls == 1:
                invalid.write_text("invalid\n", encoding="utf-8")
                return loop_manager.CodexResult(0, "", "planner-session")
            if 2 <= planner_calls <= 5:
                saved_retry_count = connection.execute(
                    "SELECT planning_retry_count FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["planning_retry_count"]
                self.assertEqual(saved_retry_count, 0)
                return loop_manager.CodexResult(1, "planner timeout", "planner-session")
            invalid.unlink()
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Generated")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    0,
                )
                failed = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(failed["planning_retry_count"], 0)
                self.assertIsNotNone(failed["planning_validation_error"])
                self.assertIsNone(failed["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            final = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 6)
            self.assertEqual(final["phase"], "WORKING")
            self.assertEqual(final["planning_retry_count"], 0)
            self.assertIsNone(final["planning_validation_error"])

    @dummy_worker_sessions
    def test_saved_third_retry_is_validated_without_a_fourth_planner_call(self):
        def invalid_planner(worktree, *_args, **_kwargs):
            (worktree / "unexpected.txt").write_text("invalid\n", encoding="utf-8")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=invalid_planner
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                loop_manager.update_project(
                    connection,
                    str(repo),
                    planning_retry_count=loop_manager.MAX_PLANNER_VALIDATION_RETRIES,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner.call_count, 1)
            self.assertEqual(row["planning_retry_count"], 3)
            self.assertIsNotNone(row["planning_error"])


class RequirementRecoveryUnitTests(unittest.TestCase):
    """Real parser, files and SQLite; no Git or Agent process is needed."""

    def setUp(self):
        import json
        from tests.support.database import runtime_connection
        from tests.support.catalog import plan_text
        from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        from loop_manager_internal.catalog import remote_deletions
        from loop_manager_internal.planning.planner_validation import planning_execution_state
        self.json = json
        self.recovery = remote_deletions
        self.connection = runtime_connection('repo')
        self.addCleanup(self.connection.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.worktree = Path(self.directory.name)
        self.sources = ['docs/requirements/[R-001] First.md', 'docs/requirements/[R-002] Second.md']
        self.text = plan_text('G-047', 'Runtime', implementation='- Keep the boundary explicit.').replace(
            '  - docs/PRODUCT.md', '\n'.join('  - ' + source for source in self.sources))
        self.path = self.worktree / 'docs/plans/[G-047] Runtime.md'
        self.path.parent.mkdir(parents=True)
        self.path.write_text(self.text, encoding='utf-8')
        self.connection.execute("UPDATE exec_plans SET ever_started=1, workspace_owned=0, worker_result='done', sources=?",
                                (json.dumps(self.sources, separators=(',', ':')),))
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.proofs = {source: {'text': '# [R-001] Deleted\nold acceptance criteria',
            'revision': 'original-revision', 'deletion_revision': 'deleted-revision',
            'row': dict(row), 'affected': ['G-047']} for source in self.sources}
        self.connection.execute('INSERT INTO execplan_catalog_holds (repo, plan_id, revision_sha, evidence) VALUES (?, ?, ?, ?)',
            ('repo', 'G-047', 'deleted-revision', json.dumps({'deleted_sources': self.proofs})))
        self.connection.commit()
        original = ReadyPlanSnapshot('G-047', 'docs/plans/[G-047] Runtime.md', 'Runtime',
            tuple(self.sources), (), 'low', 'low', 'normal', 'hash', 'plan-sha', self.text,
            execution_state=planning_execution_state(row))
        self.snapshot = MutableReadySnapshot('repo', 'base', (original,), 'token',
            remote_deletions.recovery_goals(self.connection, 'repo'))
        self.decisions = [{'id': 'G-047', 'source': source, 'outcome': 'withdrawn',
                           'reason': 'All remaining product and shared goals were examined.'} for source in self.sources]
        self.record(self.decisions)

    def record(self, decisions=None, *, raw=None):
        self.connection.execute('UPDATE projects SET planning_result=?',
            (raw if raw is not None else 'REQUIREMENT_RECOVERY: ' + self.json.dumps(decisions),))
        self.connection.commit()

    def prepare(self, **kwargs):
        return self.recovery.prepare_withdrawals(self.connection, 'repo', self.worktree, self.snapshot, **kwargs)

    def test_partial_conflicting_or_uncertain_withdrawal_keeps_original_and_has_no_receipt(self):
        for decisions in (self.decisions[:1], [self.decisions[0], dict(self.decisions[1], outcome='retained')],
                          [self.decisions[0], dict(self.decisions[1], outcome='uncertain')]):
            with self.subTest(decisions=decisions):
                self.record(decisions)
                with self.assertRaises(self.recovery.PlannerValidationError):
                    self.prepare()
                self.assertEqual(self.path.read_text(encoding='utf-8'), self.text)
                evidence = self.connection.execute('SELECT evidence FROM execplan_catalog_holds').fetchone()[0]
                self.assertNotIn('prepared_withdrawal', self.json.loads(evidence))

    def test_strict_json_rejects_duplicate_marker_keys_and_invalid_fields(self):
        valid = self.json.dumps(self.decisions)
        invalid = [
            'REQUIREMENT_RECOVERY: ' + valid + '\nREQUIREMENT_RECOVERY: ' + valid,
            'REQUIREMENT_RECOVERY: ' + valid.replace('"outcome": "withdrawn"', '"outcome": "retained", "outcome": "withdrawn"', 1),
        ]
        for field, value in (('outcome', []), ('reason', ' '), ('id', 'G-999'),
                             ('source', self.sources[0] + '.wrong'), ('outcome', 'partial')):
            items = [dict(self.decisions[0], **{field: value}), self.decisions[1]]
            invalid.append('REQUIREMENT_RECOVERY: ' + self.json.dumps(items))
        invalid.append('REQUIREMENT_RECOVERY: ' + self.json.dumps(self.decisions + self.decisions[:1]))
        for raw in invalid:
            with self.subTest(raw=raw):
                self.record(raw=raw)
                with self.assertRaises(self.recovery.PlannerValidationError):
                    self.prepare()
                self.assertTrue(self.path.exists())

    def test_source_text_and_last_revision_are_snapshot_bound_before_preparation(self):
        from loop_manager_internal.planning.planning_catalog import mutable_snapshot_token

        source_goal = next(item for item in self.json.loads(self.snapshot.prompt_payload())['deleted_goals']
                           if item['path'] == self.sources[0])
        self.assertEqual(source_goal['text'], self.proofs[self.sources[0]]['text'])
        self.assertEqual(source_goal['source_revision'], self.proofs[self.sources[0]]['revision'])
        self.assertEqual(source_goal['deletion_revision'], self.proofs[self.sources[0]]['deletion_revision'])
        self.assertEqual(source_goal['affected_ids'], ['G-047'])
        token = mutable_snapshot_token('repo', 'base', self.snapshot.plans, self.snapshot.recovery_goals)
        for field in ('text', 'revision', 'deletion_revision'):
            changed_goal = self.json.loads(self.snapshot.recovery_goals[0])
            changed_evidence = self.json.loads(changed_goal['evidence'])
            changed_evidence[field] = 'changed proof'
            changed_goal['evidence'] = self.json.dumps(changed_evidence, sort_keys=True)
            changed_goals = (self.json.dumps(changed_goal, sort_keys=True), *self.snapshot.recovery_goals[1:])
            with self.subTest(token_field=field):
                self.assertNotEqual(token, mutable_snapshot_token('repo', 'base', self.snapshot.plans, changed_goals))

        for field in ('text', 'revision', 'deletion_revision'):
            with self.subTest(field=field):
                proofs = {source: dict(proof) for source, proof in self.proofs.items()}
                proofs[self.sources[0]][field] = 'changed'
                self.connection.execute('UPDATE execplan_catalog_holds SET evidence=?',
                    (self.json.dumps({'deleted_sources': proofs}),))
                self.connection.commit()
                with self.assertRaisesRegex(self.recovery.PlannerValidationError, '근거가 변경'):
                    self.prepare()
                self.assertTrue(self.path.exists())

    def test_preservation_receipt_resumes_before_and_after_unlink_with_all_judgments(self):
        with mock.patch.object(Path, 'unlink', side_effect=OSError('interrupted before unlink')):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertTrue(self.path.exists())
        receipt = self.json.loads(self.connection.execute('SELECT evidence FROM execplan_catalog_holds').fetchone()[0])['prepared_withdrawal']
        self.assertEqual(receipt['preserved_text'], self.text)
        self.assertEqual(len(receipt['judgments']), 2)
        prepared = self.prepare()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.prepare(), prepared)
        self.assertEqual(prepared['G-047'], receipt)

    def test_modified_original_or_writer_change_prevents_preparation(self):
        self.path.write_text(self.text + '\nchanged goal', encoding='utf-8')
        with self.assertRaisesRegex(self.recovery.PlannerValidationError, '원문'):
            self.prepare()
        self.path.write_text(self.text, encoding='utf-8')
        self.connection.execute("UPDATE exec_plans SET agent_role='worker'")
        self.connection.commit()
        with self.assertRaisesRegex(self.recovery.PlannerValidationError, 'writer'):
            self.prepare()
        self.assertTrue(self.path.exists())

    def test_actual_pr_check_runs_before_any_receipt_or_unlink_and_on_resume(self):
        from dataclasses import replace
        from loop_manager_internal.planning import planner_validation
        self.connection.execute("UPDATE exec_plans SET branch='topic'")
        self.connection.commit()
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        original = replace(self.snapshot.plans[0], current_metadata_recorded=True,
            execution_state=planner_validation.planning_execution_state(row))
        self.snapshot = replace(self.snapshot, plans=(original,))
        pr = mock.Mock(return_value={'url': 'existing PR'})
        effects = planner_validation.AssertMutableReadySnapshotCurrentEffects(
            loop_manager._execplan_metadata, loop_manager._row_is_mutable_ready, pr)
        def check(connection, snapshot):
            planner_validation._assert_mutable_ready_snapshot_current(effects, connection, snapshot)
        with self.assertRaisesRegex(planner_validation.MutableReadyPRConflict, 'PR'):
            self.prepare(assert_current=check)
        self.assertTrue(self.path.exists())
        pr.assert_called_once_with(Path('repo'), 'topic')
        pr.return_value = None
        self.prepare(assert_current=check)
        pr.return_value = {'url': 'PR on resume'}
        with self.assertRaisesRegex(planner_validation.MutableReadyPRConflict, 'PR'):
            self.prepare(assert_current=check)

    def test_user_decision_and_manual_preparation_intent_cannot_be_withdrawn(self):
        from loop_manager_internal.storage import execplan_draft_state
        execplan_draft_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO execplan_drafts VALUES ('repo', 'G-047', 1, '{}')")
        self.connection.commit()
        with self.assertRaisesRegex(self.recovery.PlannerValidationError, 'draft'):
            self.prepare()
        self.connection.execute('DELETE FROM execplan_drafts')
        self.connection.execute("""INSERT INTO manual_work_groups
            (repo, group_id, kind, source_branch, requirements, source_head, base_sha, state, members, documents, owner_id)
            VALUES ('repo', 'group', 'start', 'topic', '[]', 'head', 'base', 'STARTING', '["G-047"]', '[]', 'G-047')""")
        self.connection.execute("INSERT INTO manual_work_members VALUES ('repo', 'G-047', 'group')")
        self.connection.commit()
        with self.assertRaisesRegex(self.recovery.PlannerValidationError, '수동'):
            self.prepare()
        self.assertTrue(self.path.exists())

    def test_retained_goals_keep_shared_checklist_files_validation_and_dependency(self):
        from loop_manager_internal.catalog.execplan_catalog import parse_execplan_text
        self.record([dict(item, outcome='retained', reason='PRODUCT shared goal remains') for item in self.decisions])
        revised = self.text.replace('\n'.join('  - ' + source for source in self.sources), '  - docs/PRODUCT.md')
        revised = revised.replace('depends_on: []', 'depends_on:\n  - G-048')
        revised += '\nShared implementation: shared.py; validation: shared_test.py (PRODUCT).\n'
        self.path.write_text(revised, encoding='utf-8')
        plan = parse_execplan_text(self.snapshot.plans[0].path, revised, validate_sources=False)
        self.recovery.validate_recovery(self.worktree, self.snapshot, {self.snapshot.plans[0].path: plan},
            {}, connection=self.connection, repo='repo')
        self.assertEqual(self.prepare(), {})
        self.assertEqual(plan['sources'], ['docs/PRODUCT.md'])
        self.assertEqual(plan['depends_on'], ['G-048'])
        self.assertIn('Keep the boundary explicit.', self.path.read_text(encoding='utf-8'))
        self.assertIn('shared_test.py', self.path.read_text(encoding='utf-8'))

    def test_unresolved_source_stays_held_and_resolutions_rollback_with_batch(self):
        self.record([dict(item, outcome='retained') for item in self.decisions])
        self.recovery.validate_recovery(self.worktree, self.snapshot, {}, {}, connection=self.connection, repo='repo')
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.connection:
                self.recovery.resolve_recovery(self.connection, self.snapshot, 'published', {'G-047'})
                raise RuntimeError('rollback')
        self.assertEqual(self.recovery.recovery_goals(self.connection, 'repo'), self.snapshot.recovery_goals)
        with self.connection:
            self.recovery.resolve_recovery(self.connection, self.snapshot, 'published', {'G-047'})
        self.assertEqual(self.recovery.recovery_goals(self.connection, 'repo'), ())
        with self.connection:
            self.recovery.resolve_recovery(self.connection, self.snapshot, 'published', {'G-047'})
        self.assertEqual(self.recovery.recovery_goals(self.connection, 'repo'), ())

    def test_repeated_source_proofs_union_affected_and_reject_stale_conflicts(self):
        other = {source: dict(proof, affected=['G-048']) for source, proof in self.proofs.items()}
        self.connection.execute('INSERT INTO execplan_catalog_holds (repo, plan_id, revision_sha, evidence) VALUES (?, ?, ?, ?)',
            ('repo', 'G-048', 'deleted-revision', self.json.dumps({'deleted_sources': other})))
        goals = [self.json.loads(raw) for raw in self.recovery.recovery_goals(self.connection, 'repo')]
        self.assertTrue(all(goal['affected_ids'] == ['G-047', 'G-048'] for goal in goals))
        for field in ('text', 'revision', 'deletion_revision'):
            with self.subTest(field=field):
                conflicting = {source: dict(proof, affected=['G-048']) for source, proof in self.proofs.items()}
                conflicting[self.sources[0]][field] = 'conflicting proof'
                self.connection.execute("UPDATE execplan_catalog_holds SET evidence=? WHERE plan_id='G-048'",
                    (self.json.dumps({'deleted_sources': conflicting}),))
                with self.assertRaisesRegex(self.recovery.PlannerValidationError, '상충'):
                    self.recovery.recovery_goals(self.connection, 'repo')

    def test_requirement_deletions_union_only_identical_active_and_cancelled_proofs(self):
        from loop_manager_internal.git import command_adapter
        from loop_manager_internal.catalog.execplan_catalog import CatalogIssue

        source = self.sources[0]
        original_revision = 'a' * 40
        current_revision = 'c' * 40
        self.connection.execute('UPDATE exec_plans SET sources=?, plan_sha=? WHERE plan_id=?',
            (self.json.dumps([source]), original_revision, 'G-047'))
        self.connection.execute('''INSERT INTO exec_plans
            (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha)
            VALUES ('repo', 'G-048', 'docs/plans/[G-048] Active.md', 'Active', ?, '[]',
                    'normal', 'active-hash', 'READY', ?)''',
            (self.json.dumps([source]), original_revision))
        self.connection.execute('''INSERT INTO execplan_cancellations
            (repo, plan_id, path, title, sources, depends_on, complexity, risk, priority,
             content_hash, plan_sha, deletion_commit, recovery_evidence)
            VALUES ('repo', 'G-049', 'docs/plans/[G-049] Cancelled.md', 'Cancelled', ?, '[]',
                    'low', 'low', 'normal', 'cancelled-hash', ?, 'cancelled-at', '{}')''',
            (self.json.dumps([source]), original_revision))
        self.connection.commit()

        class Rows:
            def __init__(self, items):
                self.items = items

            def fetchall(self):
                return self.items

            def fetchone(self):
                return self.items[0] if self.items else None

        class ReorderedConnection:
            def __init__(self, connection, reverse):
                self.connection = connection
                self.reverse = reverse

            def execute(self, statement, parameters=()):
                cursor = self.connection.execute(statement, parameters)
                if statement.startswith('SELECT * FROM exec_plans WHERE repo=') or statement.startswith(
                        'SELECT * FROM execplan_cancellations WHERE repo='):
                    rows = cursor.fetchall()
                    if self.reverse:
                        rows.reverse()
                    return Rows(rows)
                return cursor

        contents = '# [R-001] First\n## Acceptance Criteria\n- Original goal\n'
        git = mock.Mock(return_value=SimpleNamespace(returncode=0))

        def documents(_git_bytes, _repository, revision):
            return {} if revision == current_revision else {source: contents}

        pinned = SimpleNamespace(revision_sha=current_revision, snapshot=SimpleNamespace(issues=(
            CatalogIssue('G-047', 'docs/plans/[G-047] Runtime.md', 'missing-source',
                'source is absent', source),
        )))

        with mock.patch.object(command_adapter, 'revision_requirement_documents', side_effect=documents):
            forward = self.recovery.requirement_deletions(self.connection, 'repo', self.worktree,
                pinned, git=git, git_bytes=mock.Mock())
            reversed_rows = self.recovery.requirement_deletions(ReorderedConnection(self.connection, True),
                'repo', self.worktree, pinned, git=git, git_bytes=mock.Mock())
        self.assertEqual(forward, reversed_rows)
        self.assertEqual(forward[source]['affected'], ['G-047', 'G-048', 'G-049'])
        self.assertEqual(forward[source]['revision'], original_revision)
        self.assertEqual(forward[source]['deletion_revision'], current_revision)
        self.assertEqual(forward[source]['row']['plan_id'], 'G-047')

        self.connection.execute("UPDATE execplan_cancellations SET plan_sha=? WHERE plan_id='G-049'", ('b' * 40,))
        self.connection.commit()

        for conflict in ('revision', 'text'):
            def changed_documents(_git_bytes, _repository, revision):
                if revision == current_revision:
                    return {}
                body = contents + '- changed\n' if conflict == 'text' and revision != original_revision else contents
                return {source: body}

            for reverse in (False, True):
                with self.subTest(conflict=conflict, reverse=reverse), mock.patch.object(
                        command_adapter, 'revision_requirement_documents', side_effect=changed_documents):
                    with self.assertRaisesRegex(self.recovery.PlannerValidationError, '상충'):
                        self.recovery.requirement_deletions(ReorderedConnection(self.connection, reverse),
                            'repo', self.worktree, pinned,
                            git=git, git_bytes=mock.Mock())
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM exec_plans WHERE repo='repo'").fetchone()[0], 2)
        self.assertEqual(self.connection.execute(
            "SELECT recovery_evidence FROM execplan_cancellations WHERE plan_id='G-049'").fetchone()[0], '{}')

        self.connection.execute("DELETE FROM execplan_cancellations WHERE plan_id='G-049'")
        self.connection.execute("UPDATE exec_plans SET plan_sha=? WHERE plan_id='G-048'", ('b' * 40,))
        self.connection.commit()

        def conflicting_active_documents(_git_bytes, _repository, revision):
            if revision == current_revision:
                return {}
            return {source: contents + '- changed\n' if revision != original_revision else contents}

        with mock.patch.object(command_adapter, 'revision_requirement_documents',
                               side_effect=conflicting_active_documents):
            with self.assertRaisesRegex(self.recovery.PlannerValidationError, '상충'):
                self.recovery.requirement_deletions(self.connection, 'repo', self.worktree,
                    pinned, git=git, git_bytes=mock.Mock())

    def test_requirement_deletions_ignore_conflicting_cancellations_without_active_impact(self):
        from loop_manager_internal.git import command_adapter

        deleted_source, active_source = self.sources
        current_revision = 'd' * 40
        self.connection.execute("UPDATE exec_plans SET sources=?, plan_sha=? WHERE plan_id='G-047'",
            (self.json.dumps([active_source]), 'c' * 40))
        self.connection.execute("DELETE FROM execplan_catalog_holds WHERE repo='repo'")
        expected_cancellations = []
        for plan_id, revision in (('G-048', 'a' * 40), ('G-049', 'b' * 40)):
            path = f'docs/plans/[{plan_id}] Cancelled.md'
            self.connection.execute('''INSERT INTO execplan_cancellations
                (repo, plan_id, path, title, sources, depends_on, complexity, risk, priority,
                 content_hash, plan_sha, deletion_commit)
                VALUES ('repo', ?, ?, 'Cancelled', ?, '[]', 'low', 'low', 'normal', ?, ?, ?)''',
                (plan_id, path, self.json.dumps([deleted_source]), f'hash-{plan_id}', revision,
                 'e' * 40))
            expected_cancellations.append((plan_id, self.json.dumps([deleted_source]), revision, 'e' * 40))
        self.connection.commit()

        contents_by_revision = {
            current_revision: {active_source: '# [R-002] Active\n## Acceptance Criteria\n- Keep this goal\n'},
            'a' * 40: {deleted_source: '# [R-001] Historical A\n## Acceptance Criteria\n- Original goal\n'},
            'b' * 40: {deleted_source: '# [R-001] Historical B\n## Acceptance Criteria\n- Original goal\n'},
        }
        pinned = SimpleNamespace(revision_sha=current_revision, snapshot=SimpleNamespace(issues=()))
        git = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with mock.patch.object(command_adapter, 'revision_requirement_documents',
                               side_effect=lambda _git_bytes, _repository, revision:
                                   contents_by_revision.get(revision, {})):
            proofs = self.recovery.requirement_deletions(self.connection, 'repo', self.worktree,
                pinned, git=git, git_bytes=mock.Mock())

        self.assertEqual(self.recovery.recovery_goals(self.connection, 'repo'), ())
        self.assertEqual(proofs, {})
        ready = self.connection.execute("SELECT status, sources FROM exec_plans WHERE plan_id='G-047'").fetchone()
        self.assertEqual(ready['status'], 'READY')
        self.assertEqual(self.json.loads(ready['sources']), [active_source])
        cancellations = self.connection.execute(
            "SELECT plan_id, sources, plan_sha, deletion_commit FROM execplan_cancellations ORDER BY plan_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in cancellations], expected_cancellations)

    def test_requirement_deletions_ignore_verified_merged_archive_rows(self):
        from loop_manager_internal.git import command_adapter
        from loop_manager_internal.storage import execplan_catalog_state

        deleted_source, active_source = self.sources
        current_revision = 'd' * 40
        self.connection.execute("UPDATE exec_plans SET sources=?, plan_sha=? WHERE plan_id='G-047'",
            (self.json.dumps([active_source]), 'c' * 40))
        self.connection.execute("DELETE FROM execplan_catalog_holds WHERE repo='repo'")
        for plan_id, revision in (('G-048', 'a' * 40), ('G-049', 'b' * 40)):
            original_path = f'docs/plans/[{plan_id}] Historical.md'
            archived_path = f'docs/plans/archive/[{plan_id}] Historical.md'
            content_hash = f'archive-hash-{plan_id}'
            self.connection.execute('''INSERT INTO exec_plans
                (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha)
                VALUES ('repo', ?, ?, 'Historical', ?, '[]', 'normal', ?, 'MERGED', ?)''',
                (plan_id, archived_path, self.json.dumps([deleted_source]), content_hash, revision))
            self.connection.execute('''INSERT INTO execplan_catalog_history
                (repo, plan_id, original_path, observed_path, original_content_hash, final_content_hash,
                 plan_sha, merge_confirmed, identity_verified, currently_archived, proof_kind, proof_revision)
                VALUES ('repo', ?, ?, ?, ?, ?, ?, 1, 1, 1, 'stored-and-git', ?)''',
                (plan_id, original_path, archived_path, f'original-{plan_id}', content_hash,
                 revision, 'e' * 40))
        self.connection.commit()
        rows = self.connection.execute("SELECT * FROM exec_plans WHERE repo='repo'").fetchall()
        self.assertEqual(execplan_catalog_state.verified_merged_archive_execution_ids(
            self.connection, 'repo', rows), frozenset({'G-048', 'G-049'}))
        self.assertEqual(self.recovery.recovery_goals(self.connection, 'repo'), ())

        contents_by_revision = {
            current_revision: {active_source: '# [R-002] Active\n## Acceptance Criteria\n- Keep this goal\n'},
            'a' * 40: {deleted_source: '# [R-001] Historical A\n## Acceptance Criteria\n- Original goal\n'},
            'b' * 40: {deleted_source: '# [R-001] Historical B\n## Acceptance Criteria\n- Original goal\n'},
        }
        git = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with mock.patch.object(command_adapter, 'revision_requirement_documents',
                               side_effect=lambda _git_bytes, _repository, revision:
                                   contents_by_revision.get(revision, {})):
            proofs = self.recovery.requirement_deletions(self.connection, 'repo', self.worktree,
                SimpleNamespace(revision_sha=current_revision), git=git, git_bytes=mock.Mock())

        self.assertEqual(proofs, {})
        ready = self.connection.execute("SELECT status, sources FROM exec_plans WHERE plan_id='G-047'").fetchone()
        self.assertEqual(ready['status'], 'READY')
        self.assertEqual(self.json.loads(ready['sources']), [active_source])
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM execplan_catalog_holds WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone())

    def test_repeated_absence_keeps_first_proof_receipt_and_resume_phase(self):
        from loop_manager_internal.catalog.execplan_catalog import CatalogIssue, ExecPlanCatalogSnapshot
        from loop_manager_internal.storage.execplan_catalog_state import (
            DefinitionStateRepository, PinnedCatalog,
        )

        source = self.sources[0]
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', sources=? WHERE plan_id='G-047'",
            (self.json.dumps([source]),))
        self.connection.execute("DELETE FROM execplan_catalog_holds WHERE repo='repo' AND plan_id='G-047'")
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-047'").fetchone()
        first_revision, repeated_revision = 'a' * 40, 'b' * 40
        first = {'text': '# Original Requirement\n', 'revision': 'plan-revision',
            'deletion_revision': first_revision, 'row': dict(row), 'affected': ['G-047']}

        def apply(revision, proof):
            issue = CatalogIssue(
                'G-047', 'docs/plans/[G-047] Runtime.md', 'missing-source', 'source is absent', source)
            snapshot = ExecPlanCatalogSnapshot({}, {}, revision, True, (issue,),
                (('G-047', 'docs/plans/[G-047] Runtime.md', 'hash'),))
            repository = DefinitionStateRepository(self.connection)
            expected = repository.capture('repo')
            return repository.apply('repo', PinnedCatalog(revision, snapshot,
                requirement_deletions={source: proof}), expected, revision_is_current=lambda: True)

        apply(first_revision, first)
        held_row = self.connection.execute("SELECT evidence, resume_status FROM execplan_catalog_holds WHERE plan_id='G-047'").fetchone()
        evidence = self.json.loads(held_row['evidence'])
        evidence['deleted_sources'][source]['resolved'] = {'G-047': 'published-repair'}
        evidence['prepared_withdrawal'] = {'id': 'G-047', 'source': source, 'outcome': 'withdrawn',
            'reason': 'previously prepared', 'preserved_text': self.text}
        self.connection.execute("UPDATE execplan_catalog_holds SET evidence=? WHERE plan_id='G-047'",
            (self.json.dumps(evidence),))
        self.connection.commit()

        repeated = dict(first, deletion_revision=repeated_revision)
        apply(repeated_revision, repeated)
        saved = self.connection.execute(
            "SELECT revision_sha, evidence, resume_status FROM execplan_catalog_holds WHERE plan_id='G-047'").fetchone()
        saved_evidence = self.json.loads(saved['evidence'])
        saved_proof = saved_evidence['deleted_sources'][source]
        self.assertEqual(saved['revision_sha'], repeated_revision)
        self.assertEqual(saved['resume_status'], 'IMPLEMENTING')
        self.assertEqual(saved_proof['deletion_revision'], first_revision)
        self.assertEqual(saved_proof['resolved'], {'G-047': 'published-repair'})
        self.assertEqual(saved_evidence['prepared_withdrawal']['reason'], 'previously prepared')

        expanded = dict(repeated, deletion_revision='e' * 40, affected=['G-047', 'G-048'])
        apply('e' * 40, expanded)
        expanded_row = self.connection.execute(
            "SELECT revision_sha, evidence, resume_status FROM execplan_catalog_holds WHERE plan_id='G-047'").fetchone()
        expanded_evidence = self.json.loads(expanded_row['evidence'])
        expanded_proof = expanded_evidence['deleted_sources'][source]
        self.assertEqual(expanded_row['revision_sha'], 'e' * 40)
        self.assertEqual(expanded_row['resume_status'], 'IMPLEMENTING')
        self.assertEqual(expanded_proof['deletion_revision'], first_revision)
        self.assertEqual(expanded_proof['affected'], ['G-047', 'G-048'])
        self.assertEqual(expanded_proof['resolved'], {'G-047': 'published-repair'})
        self.assertNotIn('prepared_withdrawal', expanded_evidence)

        changed = dict(expanded, text='# Changed Requirement\n')
        with self.assertRaisesRegex(self.recovery.PlannerValidationError, '근거가 변경'):
            apply('d' * 40, changed)
        after_error = self.connection.execute(
            "SELECT revision_sha, evidence, resume_status FROM execplan_catalog_holds WHERE plan_id='G-047'").fetchone()
        self.assertEqual(after_error['revision_sha'], 'e' * 40)
        self.assertEqual(after_error['evidence'], expanded_row['evidence'])
        self.assertEqual(after_error['resume_status'], 'IMPLEMENTING')

    def test_new_hold_reuses_first_deletion_proof_and_recovery_goals(self):
        from loop_manager_internal.catalog.execplan_catalog import CatalogIssue, ExecPlanCatalogSnapshot
        from loop_manager_internal.storage.execplan_catalog_state import (
            DefinitionStateRepository, PinnedCatalog,
        )

        first_source, second_source = self.sources
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', sources=? WHERE plan_id='G-047'",
            (self.json.dumps([first_source]),))
        self.connection.execute("DELETE FROM execplan_catalog_holds WHERE repo='repo'")
        self.connection.commit()
        first_row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-047'").fetchone()
        first_revision, second_revision = 'a' * 40, 'b' * 40

        def apply(revision, issues, withheld, proofs):
            snapshot = ExecPlanCatalogSnapshot({}, {}, revision, True, tuple(issues), tuple(withheld))
            repository = DefinitionStateRepository(self.connection)
            expected = repository.capture('repo')
            return repository.apply('repo', PinnedCatalog(revision, snapshot,
                requirement_deletions=proofs), expected, revision_is_current=lambda: True)

        first_proof = {'text': '# First Requirement\n', 'revision': '1' * 40,
            'deletion_revision': first_revision, 'row': dict(first_row), 'affected': ['G-047']}
        first_path = 'docs/plans/[G-047] Runtime.md'
        apply(first_revision,
            [CatalogIssue('G-047', first_path, 'missing-source', 'source is absent', first_source)],
            [('G-047', first_path, 'hash-047')], {first_source: first_proof})
        first_hold = self.json.loads(self.connection.execute(
            "SELECT evidence FROM execplan_catalog_holds WHERE repo='repo' AND plan_id='G-047'").fetchone()[0])
        first_hold['deleted_sources'][first_source]['resolved'] = {'G-047': 'published-repair'}
        self.connection.execute("UPDATE execplan_catalog_holds SET evidence=? WHERE repo='repo' AND plan_id='G-047'",
            (self.json.dumps(first_hold),))

        self.connection.execute('''INSERT INTO exec_plans
            (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha)
            VALUES ('repo', 'G-048', 'docs/plans/[G-048] Related.md', 'Related', ?, '[]',
                    'normal', 'hash-048', 'READY', ?)''',
            (self.json.dumps([second_source]), '2' * 40))
        self.connection.commit()
        second_row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-048'").fetchone()
        second_path = 'docs/plans/[G-048] Related.md'
        current_first_proof = dict(first_proof, deletion_revision=second_revision)
        second_proof = {'text': '# Second Requirement\n', 'revision': '2' * 40,
            'deletion_revision': second_revision, 'row': dict(second_row), 'affected': ['G-048']}
        apply(second_revision,
            [CatalogIssue('G-047', first_path, 'missing-source', 'source is absent', first_source),
             CatalogIssue('G-048', second_path, 'missing-source', 'source is absent', second_source)],
            [('G-047', first_path, 'hash-047'), ('G-048', second_path, 'hash-048')],
            {first_source: current_first_proof, second_source: second_proof})

        held = {row['plan_id']: self.json.loads(row['evidence']) for row in self.connection.execute(
            "SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo='repo'")}
        goals = [self.json.loads(raw) for raw in self.recovery.recovery_goals(self.connection, 'repo')]
        for plan_id in ('G-047', 'G-048'):
            proofs = held[plan_id]['deleted_sources']
            self.assertEqual(proofs[first_source]['deletion_revision'], first_revision)
            self.assertEqual(proofs[first_source]['resolved'], {'G-047': 'published-repair'})
            self.assertEqual(proofs[second_source]['deletion_revision'], second_revision)
        goals_by_source = {goal['path']: goal for goal in goals if goal.get('kind') == 'missing-source'}
        self.assertEqual(set(goals_by_source), {second_source})
        self.assertEqual(goals_by_source[second_source]['deletion_revision'], second_revision)
        self.assertEqual(goals_by_source[second_source]['affected_ids'], ['G-048'])

    def test_feedback_marker_does_not_authorize_requirement_withdrawal_or_release_draft(self):
        from loop_manager_internal.storage import validation_feedback_state, execplan_draft_state
        validation_feedback_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO validation_feedback_migrations (repo, plan_id) VALUES ('repo', 'G-047')")
        self.connection.commit()
        self.record([])
        self.assertEqual(self.prepare(), {})
        self.assertTrue(self.path.exists())
        self.record(self.decisions)
        execplan_draft_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO execplan_drafts VALUES ('repo', 'G-047', 1, '{}')")
        self.connection.commit()
        with self.assertRaisesRegex(self.recovery.PlannerValidationError, 'draft'):
            self.prepare()
        self.assertTrue(self.path.exists())
        self.assertIn('G-047', execplan_draft_state.pending_ids(self.connection, 'repo'))

    def test_manifest_roundtrip_and_atomic_cancellation_preserve_every_source_and_history(self):
        from dataclasses import replace
        from loop_manager_internal.planning.planning_catalog import PlanDeletion, mutable_snapshot_token
        from loop_manager_internal.storage.planning_publication_state import apply_mutable_catalog_changes
        judgment = self.prepare()['G-047']
        snapshot = replace(self.snapshot, token=mutable_snapshot_token('repo', 'base', self.snapshot.plans, self.snapshot.recovery_goals))
        result = loop_manager.ValidatedPlanningResult(plans=(), deletions=(PlanDeletion(snapshot.plans[0], judgment),),
            publication_paths=(snapshot.plans[0].path,), mutable_snapshot=snapshot)
        manifest = loop_manager._planning_result_manifest(result)
        restored = loop_manager._planning_result_from_manifest(manifest)
        self.assertEqual(restored, result)
        def apply():
            apply_mutable_catalog_changes(self.connection, 'repo', deletions=restored.deletions, revisions=(),
                snapshot_token=snapshot.token, plan_commit='published', content_hash=lambda path: 'unused')
            self.recovery.resolve_recovery(self.connection, snapshot, 'published', {'G-047'})
        with self.assertRaisesRegex(RuntimeError, 'interrupted DB apply'):
            with self.connection:
                apply()
                raise RuntimeError('interrupted DB apply')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM exec_plans').fetchone()[0], 1)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM execplan_cancellations').fetchone()[0], 0)
        with self.connection:
            apply()
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM exec_plans').fetchone()[0], 0)
        audit = self.json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.assertEqual(audit['text'], self.text)
        self.assertEqual(audit['row']['worker_result'], 'done')
        self.assertEqual(audit['row']['ever_started'], 1)
        self.assertEqual(audit['judgment']['judgments'], judgment['judgments'])
        self.assertEqual(self.recovery.recovery_goals(self.connection, 'repo'), ())

    def test_pending_decision_added_after_preparation_still_blocks_db_cancellation(self):
        from loop_manager_internal.planning.planning_catalog import PlanDeletion
        from loop_manager_internal.storage import execplan_draft_state, planning_publication_state
        judgment = self.prepare()['G-047']
        execplan_draft_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO execplan_drafts VALUES ('repo', 'G-047', 1, '{}')")
        self.connection.commit()
        with self.assertRaisesRegex(self.recovery.PlannerValidationError, 'draft'):
            planning_publication_state.apply_mutable_catalog_changes(self.connection, 'repo',
                deletions=(PlanDeletion(self.snapshot.plans[0], judgment),), revisions=(), snapshot_token='token',
                plan_commit='published', content_hash=lambda path: 'unused')
        self.assertIsNotNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM execplan_cancellations').fetchone()[0], 0)

    def test_dependency_recovery_rejects_duplicate_json_keys_and_preserves_replacement_gate(self):
        from dataclasses import replace
        self.record([])
        goal = {'id': 'G-046', 'path': 'docs/plans/[G-046] Deleted.md', 'affected_ids': ['G-047']}
        snapshot = replace(self.snapshot, recovery_goals=(self.json.dumps(goal),))
        plan = {'id': 'G-047', 'sources': ['docs/PRODUCT.md'], 'depends_on': ['G-048']}
        decision = {'deleted_id': 'G-046', 'outcome': 'replaced', 'replacement_ids': ['G-048'],
                    'sources': ['docs/PRODUCT.md'], 'reason': 'Shared prerequisite is still needed.'}
        with mock.patch.object(self.recovery, 'recovery_goals', return_value=snapshot.recovery_goals):
            for raw, catalog, valid in (
                (self.json.dumps(decision), {'G-048': {}}, True),
                (self.json.dumps(decision), {}, False),
                (self.json.dumps(decision).replace('"outcome": "replaced"', '"outcome": "obsolete", "outcome": "replaced"'), {'G-048': {}}, False),
            ):
                with self.subTest(raw=raw, catalog=catalog):
                    self.path.write_text(self.text + '\nDEPENDENCY_RECOVERY: ' + raw, encoding='utf-8')
                    if valid:
                        self.recovery.validate_recovery(self.worktree, snapshot, {self.snapshot.plans[0].path: plan},
                            catalog, connection=self.connection, repo='repo')
                    else:
                        with self.assertRaises(self.recovery.PlannerValidationError):
                            self.recovery.validate_recovery(self.worktree, snapshot, {self.snapshot.plans[0].path: plan},
                                catalog, connection=self.connection, repo='repo')

    def test_partial_ac_change_is_an_ordinary_specification_input_change(self):
        from loop_manager_internal.planning.input_fingerprints import _specification_input_digest
        source = b'docs/requirements/[R-003] Shared.md'
        before = b'# [R-003] Shared\n## Acceptance Criteria\n- Shared behavior\n- Withdrawn condition\n'
        after = before.replace(b'- Withdrawn condition\n', b'')
        self.assertNotEqual(
            _specification_input_digest([(source, b'blob', before)], guidance_hash='guidance'),
            _specification_input_digest([(source, b'blob', after)], guidance_hash='guidance'))
        self.record([])
        from dataclasses import replace
        empty = replace(self.snapshot, recovery_goals=())
        self.connection.execute('DELETE FROM execplan_catalog_holds')
        self.connection.commit()
        self.assertEqual(self.recovery.prepare_withdrawals(self.connection, 'repo', self.worktree, empty), {})
        self.assertEqual(self.path.read_text(encoding='utf-8'), self.text)

    def test_deleted_renamed_source_uses_preserved_identity_and_observation_errors_are_not_withdrawal(self):
        from loop_manager_internal.git import command_adapter
        revision = 'a' * 40
        current = 'b' * 40
        source = self.sources[0]
        renamed = 'docs/requirements/[R-001] Renamed.md'
        contents = '# [R-001] Renamed\n## Acceptance Criteria\n- Keep original goal\n'
        self.connection.execute('UPDATE exec_plans SET plan_sha=?, sources=?',
            (revision, self.json.dumps([source])))
        self.connection.commit()
        git = mock.Mock(return_value=SimpleNamespace(returncode=0))
        for current_documents, expected in (({}, True), ({renamed: contents}, False),
                ({'docs/requirements/archive/[R-001] Renamed.md': contents}, False)):
            with self.subTest(documents=current_documents):
                def documents(_git_bytes, _repository, observed_revision):
                    return current_documents if observed_revision == current else {renamed: contents}
                with mock.patch.object(command_adapter, 'revision_requirement_documents', side_effect=documents):
                    result = self.recovery.requirement_deletions(self.connection, 'repo', self.worktree,
                        SimpleNamespace(revision_sha=current), git=git, git_bytes=mock.Mock())
                self.assertEqual(bool(result), expected)
                if expected:
                    self.assertEqual(result[source]['text'], contents)
                    self.assertEqual(result[source]['revision'], revision)
                    self.assertEqual(result[source]['affected'], ['G-047'])
        with mock.patch.object(command_adapter, 'revision_requirement_documents', side_effect=RuntimeError('lookup failed')):
            with self.assertRaisesRegex(RuntimeError, 'lookup failed'):
                self.recovery.requirement_deletions(self.connection, 'repo', self.worktree,
                    SimpleNamespace(revision_sha=current), git=git, git_bytes=mock.Mock())
