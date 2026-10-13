from tests.support.fixed_samples import use_fixed_guidance
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.catalog import catalog_service
from loop_manager_internal.planning.planning_runtime import (
    PlanningContext, PlanningLifecycleCoordinator, PlanningOutcomeKind,
)
from loop_manager_internal.storage import maintenance_state, planning_input_state, runtime_state
from tests.support.catalog import write_execplan
from tests.support.git import (
    git_command, make_remote_repository, commit_and_push, isolated_git_test,
)
from tests.support.planning_case import FakePorts, planning_snapshot, confirm_final_review
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class PlanningFingerprintTest(unittest.TestCase):
    def setUp(self):
        use_fixed_guidance(self)
        super().setUp()

    def test_input_kind_distinguishes_documents_plans_and_code(self):
        classify = lambda path: catalog_service.classify_planning_input(
            path, loop_manager._is_active_requirement,
        )
        self.assertEqual(classify(b"docs/PRODUCT.md"), catalog_service.PlanningInputKind.DOCUMENT)
        self.assertEqual(classify(b"AGENTS.md"), catalog_service.PlanningInputKind.EXCLUDED)
        self.assertEqual(classify(b"docs/plans/[G-130] Plan.md"), catalog_service.PlanningInputKind.ACTIVE_PLAN)
        self.assertEqual(classify(b"src/app.py"), catalog_service.PlanningInputKind.OTHER)
        self.assertEqual(classify(b"docs/FUTURE_IDEAS.md"), catalog_service.PlanningInputKind.EXCLUDED)

    def test_planning_input_path_boundary(self):
        cases = {
            b"AGENTS.md": False,
            b"docs/PRODUCT.md": True,
            b"docs/ARCHITECTURE.md": True,
            b"docs/requirements/Active.md": True,
            b"README.md": True,
            b"docs/requirements/.md": True,
            b".github/workflows/test.yml": True,
            b"src/app.py": True,
            b"docs/PLANS.md": False,
            b"docs/REQUIREMENT.template.md": False,
            b"docs/FUTURE_IDEAS.md": False,
            b"docs/plans/[G-999] Active.md": True,
            b"docs/plans/archive/[G-999] Archived.md": False,
            b"docs/plans/nested/[G-999] Nested.md": False,
            b"docs/requirements/archive/Old.md": False,
            b"docs/requirements/nested/Requirement.md": False,
            b"docs/requirements/NotMarkdown.txt": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(loop_manager._is_planning_input(path), expected)

    def test_planning_input_digest_is_ordered_and_unambiguous(self):
        entries = [
            (b"src/b.py", b"blob", b"two"),
            (b"src/a.py", b"blob", b"one"),
        ]

        fingerprint = loop_manager._planning_input_digest(entries)

        self.assertEqual(fingerprint, loop_manager._planning_input_digest(list(reversed(entries))))
        self.assertEqual(len(fingerprint), 64)
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"a", b"blob", b"bc")]),
            loop_manager._planning_input_digest([(b"ab", b"blob", b"c")]),
        )
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"src/a.py", b"blob", b"one")]),
            loop_manager._planning_input_digest(
                [(b"src/renamed.py", b"blob", b"one")]
            ),
        )
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"vendor/lib", b"blob", b"abc")]),
            loop_manager._planning_input_digest([(b"vendor/lib", b"commit", b"abc")]),
        )

    def test_planning_input_fingerprint_uses_origin_tree_and_input_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            initial = loop_manager.planning_input_fingerprint(repo, "main")

            (repo / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)
            (repo / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

            (repo / "docs/notes.md").write_text("# Changed notes\n", encoding="utf-8")
            commit_and_push(repo, "Change excluded documentation")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)

            (repo / "docs/requirements/archive/Old.md").write_text(
                "# Changed archived requirement\n", encoding="utf-8"
            )
            commit_and_push(repo, "Change archived requirement")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)

            (repo / "docs/PRODUCT.md").write_text("# Changed product\n", encoding="utf-8")
            commit_and_push(repo, "Change product")
            changed = loop_manager.planning_input_fingerprint(repo, "main")
            self.assertNotEqual(changed, initial)

            git_command(repo, "checkout", "--detach", "HEAD^")
            self.assertNotEqual(
                git_command(repo, "rev-parse", "HEAD"),
                git_command(repo, "rev-parse", "origin/main"),
            )
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), changed)

            git_command(repo, "checkout", "main")
            (repo / "src/app.py").rename(repo / "src/renamed.py")
            commit_and_push(repo, "Rename source")
            self.assertNotEqual(loop_manager.planning_input_fingerprint(repo, "main"), changed)

    def test_active_plan_changes_are_remote_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            initial = loop_manager.planning_input_fingerprint(repo, "main")
            other = loop_manager._nonplan_input_fingerprint(repo, "origin/main")
            plan = repo / "docs/plans/[G-999] New.md"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text("new\n", encoding="utf-8")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)
            commit_and_push(repo, "Add plan")
            added = loop_manager.planning_input_fingerprint(repo, "main")
            self.assertNotEqual(added, initial)
            self.assertEqual(loop_manager._nonplan_input_fingerprint(repo, "origin/main"), other)

            plan.write_text("revised\n", encoding="utf-8")
            commit_and_push(repo, "Revise plan")
            revised = loop_manager.planning_input_fingerprint(repo, "main")
            self.assertNotEqual(revised, added)

            renamed = plan.with_name("[G-999] Renamed.md")
            plan.rename(renamed)
            commit_and_push(repo, "Rename plan")
            renamed_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            self.assertNotEqual(renamed_fingerprint, revised)

            renamed.unlink()
            commit_and_push(repo, "Delete plan")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)

    def test_planning_input_fingerprint_includes_gitlink_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            first_target = git_command(repo, "rev-parse", "HEAD")
            git_command(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{first_target},vendor/library",
            )
            git_command(repo, "commit", "-m", "Add tracked submodule commit")
            git_command(repo, "push", "origin", "HEAD:main")
            first = loop_manager.planning_input_fingerprint(repo, "main")

            second_target = git_command(repo, "rev-parse", "HEAD")
            git_command(
                repo,
                "update-index",
                "--cacheinfo",
                f"160000,{second_target},vendor/library",
            )
            git_command(repo, "commit", "-m", "Update tracked submodule commit")
            git_command(repo, "push", "origin", "HEAD:main")

            self.assertNotEqual(
                loop_manager.planning_input_fingerprint(repo, "main"), first
            )

    def test_planner_no_change_saves_fingerprint_and_skip_gate_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            loop_manager.notifications.ensure_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            connection.commit()

            # A completed no-change Planner turn persists the observed input
            # through the same production storage adapter used by publication.
            loop_manager.clear_active(
                connection, str(repo), processed_sha="base",
                specification_fingerprint="specification-1",
                planning_fingerprint="planning-1",
            )
            planning_input_state.record_general(connection, str(repo), "base", {})
            confirm_final_review(connection, str(repo), "base", "planning-1", "specification-1")
            state = loop_manager._PlanningStatePorts(
                connection,
                runtime_state.RuntimeStateRepository(connection),
                maintenance_state.MaintenanceRepository(connection),
                state_dir,
            )
            ports = FakePorts(planning_snapshot(repo=str(repo)))
            ports.state = state
            ports.git.fingerprint = "planning-1"
            ports.git.specification_fingerprints["base"] = "specification-1"
            context = PlanningContext(
                repo=str(repo), repo_path=repo, state_dir=state_dir,
                base_branch="main", base_sha="base", provider="codex",
                config_path=Path("unused.ini"), registered_only=False,
                refactoring_review_interval=5,
            )

            with mock.patch(
                "subprocess.run", side_effect=AssertionError("unexpected Git process")
            ), mock.patch(
                "subprocess.Popen", side_effect=AssertionError("unexpected Git process")
            ):
                outcome = PlanningLifecycleCoordinator(ports).run(context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.FINGERPRINT_SKIPPED)
            self.assertEqual(ports.agent.run_count, 0)
            self.assertEqual(
                ports.events.messages[0],
                ("info", "계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다."),
            )
            saved = connection.execute(
                "SELECT processed_sha, specification_fingerprint, planning_fingerprint "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                tuple(saved), ("base", "specification-1", "planning-1")
            )
            self.assertEqual(
                [call[1] for call in ports.git.calls],
                [
                    ("recorded_base_fetch", "main"),
                    ("rev-parse", "--verify", "refs/remotes/origin/main^{commit}"),
                ],
            )

    @dummy_worker_sessions
    def test_requirement_archive_move_does_not_run_planner(self):
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
                "loop_manager.run_codex", return_value=(0, "")
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                completed_fingerprint = connection.execute(
                    "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["planning_fingerprint"]
                completed = connection.execute(
                    "SELECT processed_sha, specification_fingerprint FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                maintenance_state.MaintenanceRepository(connection).initialize_file_assessment_start(
                    str(repo), source_sha=completed["processed_sha"], expected_merged_count=0,
                )
                confirm_final_review(connection, str(repo), completed["processed_sha"],
                                     completed_fingerprint, completed["specification_fingerprint"])
                connection.commit()
                planner.reset_mock()

                active = repo / "docs/requirements/Active.md"
                archived = repo / "docs/requirements/archive/Active.md"
                active.rename(archived)
                commit_and_push(repo, "Archive completed requirement")

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                planner.assert_not_called()

            row = connection.execute(
                "SELECT processed_sha, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(row["processed_sha"], git_command(repo, "rev-parse", "origin/main"))
            self.assertEqual(row["planning_fingerprint"], completed_fingerprint)
            self.assertNotEqual(
                row["planning_fingerprint"],
                loop_manager.planning_input_fingerprint(repo, "main"),
            )

    @dummy_worker_sessions
    def test_planner_failure_and_invalid_result_do_not_save_fingerprint(self):
        def fail_planner(_worktree, *_args, **_kwargs):
            return 1, "planner failed"

        def write_invalid_file(worktree, *_args, **_kwargs):
            (worktree / "unexpected.txt").write_text("invalid\n", encoding="utf-8")
            return 0, ""

        for name, planner_effect in (
            ("failure", fail_planner),
            ("invalid-result", write_invalid_file),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
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
                    "loop_manager.run_codex", side_effect=planner_effect
                ) as planner:
                    for _attempt in range(2):
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

                row = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(planner.call_count, 2)
                self.assertEqual(row["phase"], "PLANNING")
                self.assertIsNone(row["planning_fingerprint"])

    @dummy_worker_sessions
    def test_new_execplan_saves_cataloged_input_fingerprint(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return 0, ""
            return 1, "stop after planning"

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            expected_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")

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

            row = connection.execute(
                "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(row["phase"], "WORKING")
            self.assertEqual(row["planning_fingerprint"], expected_fingerprint)


class CommonGuidanceFingerprintTest(unittest.TestCase):
    def test_common_content_changes_all_planning_inputs_but_not_revision_or_location(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.planning import input_fingerprints
        entries = [(b'AGENTS.md', b'blob', b'project')]
        functions = (input_fingerprints._planning_input_digest,
                     input_fingerprints._nonplan_input_digest,
                     input_fingerprints._specification_input_digest)
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='a' * 64):
            initial = [function(entries) for function in functions]
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='b' * 64):
            changed = [function(entries) for function in functions]
            pinned = [function(entries, guidance_hash='a' * 64) for function in functions]
        self.assertEqual(initial, pinned)
        self.assertTrue(all(before != after for before, after in zip(initial, changed)))

    def test_cli_guidance_content_does_not_change_planning_inputs(self):
        tree = {b'src/app.py': (b'blob', b'code'),
                b'docs/PRODUCT.md': (b'blob', b'product')}
        functions = (loop_manager._planning_input_fingerprint,
                     loop_manager._nonplan_input_fingerprint,
                     loop_manager._specification_input_fingerprint)
        def read(_repo, *arguments):
            self.assertIn(arguments[-1], {'code', 'product'})
            return arguments[-1].encode()
        with mock.patch.object(loop_manager, '_git_tree_entries', return_value=tree), \
             mock.patch.object(loop_manager, 'git_bytes', side_effect=read):
            baseline = [function(Path('/repo'), 'revision', guidance_hash='pinned-runtime')
                        for function in functions]
            for object_id in (b'original', b'changed', b'invalid-utf8'):
                tree[b'AGENTS.md'] = (b'blob', object_id)
                self.assertEqual([function(Path('/repo'), 'revision', guidance_hash='pinned-runtime')
                                  for function in functions], baseline)

    def test_same_remote_sha_cannot_hide_common_change_behind_archive_or_nonplan_comparison(self):
        from types import SimpleNamespace
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage import planning_input_state
        from loop_manager_internal.planning.selection_service import PlanningSelectionService
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='a' * 64):
            planning_input_state.record_general(connection, 'repo', 'revision', {})
            self.assertFalse(planning_input_state.guidance_changed(connection, 'repo'))
        git = SimpleNamespace(runtime_guidance_changed=lambda _path: planning_input_state.guidance_changed(connection, 'repo'),
                              nonplan_fingerprint=mock.Mock(return_value='same'),
                              specification_fingerprint=mock.Mock(return_value='same'),
                              requirement_archive_only=mock.Mock(return_value=True))
        service = PlanningSelectionService(SimpleNamespace(git=git))
        context = SimpleNamespace(repo_path=Path('repo'))
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='b' * 64):
            self.assertTrue(service._general_planning_required(None, context, 'same-sha', 'current'))
            self.assertTrue(service._specification_planning_required(None, context, 'same-sha', 'current', 'current'))
        git.nonplan_fingerprint.assert_not_called()
        git.requirement_archive_only.assert_not_called()

    def test_owned_planning_workspace_keeps_version_across_runtime_and_remote_changes(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.agents.agent_invocation import InvocationContext
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE projects (repo TEXT, phase TEXT, worktree TEXT, planning_attempt_fingerprint TEXT)')
        connection.execute("INSERT INTO projects VALUES ('repo', 'PLANNING', '/worktree', 'attempt-one')")
        context = common_guidance.independent_context(connection, InvocationContext('repo', None), Path('/worktree'))
        repository = GuidanceRepository(connection)
        repository.save(common_guidance.task_identity(context, 'planner', Path('/worktree')),
                        'repo', 'a' * 64, 'b' * 40, '/snapshot')
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='c' * 64):
            self.assertEqual(common_guidance.planning_content_hash(connection, 'repo'), 'a' * 64)
            connection.execute("UPDATE projects SET worktree = '/rebased-worktree'")
            self.assertEqual(common_guidance.planning_content_hash(connection, 'repo'), 'a' * 64)
            connection.execute("UPDATE projects SET phase = NULL")
            self.assertIsNone(common_guidance.planning_content_hash(connection, 'repo'))
