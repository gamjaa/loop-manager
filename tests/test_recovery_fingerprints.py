from tests.support.recovery import BoundedRecoveryFixture
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, commit_and_push, isolated_git_test
from tests.support.worker import worker_result


@isolated_git_test
class RecoveryFingerprintsTest(BoundedRecoveryFixture, unittest.TestCase):

    def test_failure_fingerprints_normalize_noise_and_preserve_issue_identity(self):
        first_root = Path("/tmp/run-one/worktree")
        second_root = Path("/tmp/run-two/worktree")
        first = "\n".join(
            (
                "2026-09-19T10:20:30Z FAIL tests/test_b.py::test_b",
                f"{first_root}/tests/test_a.py:42: assertion failed",
                "FAIL tests/test_a.py::test_a in 0.23s",
            )
        )
        second = "\n".join(
            (
                "FAIL tests/test_a.py::test_a in 9.91s",
                f"{second_root}/tests/test_a.py:42: assertion failed",
                "2027-01-01 22:11:09 FAIL tests/test_b.py::test_b",
            )
        )

        first_fingerprint, first_normalized = loop_manager.validation_failure_fingerprint(
            "verify", first, first_root
        )
        second_fingerprint, second_normalized = loop_manager.validation_failure_fingerprint(
            "verify", second, second_root
        )
        changed_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "verify",
            second.replace("test_b", "test_c"),
            second_root,
        )
        review_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "review",
            "ISSUE_ID: tests/test_a.py::test-a::blocking\nVERDICT: FAIL",
            second_root,
        )
        same_temp_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/run-one/test_same.py:42"
        )
        same_temp_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/run-two/test_same.py:42"
        )
        different_temp_file, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/run-three/test_other.py:42"
        )
        temp_directory_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "workspace cleanup failed: /tmp/tmpABC"
        )
        temp_directory_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "workspace cleanup failed: /tmp/tmpXYZ"
        )
        extensionless_file_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/inputA"
        )
        extensionless_file_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/inputB"
        )
        prefixed_file_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/tmpInputA"
        )
        prefixed_file_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/tmpInputB"
        )

        self.assertEqual(first_normalized, second_normalized)
        self.assertEqual(first_fingerprint, second_fingerprint)
        self.assertNotEqual(first_fingerprint, changed_fingerprint)
        self.assertNotEqual(first_fingerprint, review_fingerprint)
        self.assertEqual(same_temp_first, same_temp_second)
        self.assertNotEqual(same_temp_first, different_temp_file)
        self.assertEqual(temp_directory_first, temp_directory_second)
        self.assertNotEqual(extensionless_file_first, extensionless_file_second)
        self.assertNotEqual(prefixed_file_first, prefixed_file_second)

    def test_verify_fingerprint_ignores_execution_telemetry(self):
        failure = "Traceback (most recent call last):\nAttributeError: 'NoneType' object has no attribute 'split'"
        outputs = []
        for run, elapsed in (("first", 12.3), ("second", 98.7)):
            outputs.append(f"2026-10-21 11:34:14+00:00 WARN [run={run} project=repo] verify ended\n"
                f"verify 시간 진단: /state/logs/verify/{run}/summary.json\n"
                "완료 결과: {}\nfixture 결과: {}\n"
                f"미완료 {elapsed}s: verify_process\n{failure}\n"
                f"Python bytecode: enabled, created={elapsed}\n"
                f"느린 테스트 {elapsed}s: test_{run}\n"
                f"[VERIFY_DIAGNOSTIC] /state/logs/verify/{run}/summary.json")
        fingerprints = [loop_manager.validation_failure_fingerprint("verify", output)[0]
                        for output in outputs]
        self.assertEqual(fingerprints[0], fingerprints[1])
        self.assertNotEqual(fingerprints[0], loop_manager.validation_failure_fingerprint(
            "verify", outputs[1].replace("AttributeError", "ValueError"))[0])

    def test_review_fingerprints_use_stable_issue_ids_not_explanatory_prose(self):
        first = "\n".join(
            (
                "ISSUE_ID: loop_manager.py::run_project::session-recovery",
                "[P1] line 120: the saved session was not resumed.",
                "ISSUE_ID: tests/test_loop_manager.py::retry-case::missing-coverage",
                "Add a regression test near line 3000.",
                "VERDICT: FAIL",
            )
        )
        reworded = "\n".join(
            (
                "ISSUE_ID: tests/test_loop_manager.py::retry-case::missing-coverage",
                "The scenario is still untested at a different location.",
                "ISSUE_ID: LOOP_MANAGER.PY::RUN_PROJECT::SESSION-RECOVERY",
                "Session continuation remains broken, phrased differently.",
                "VERDICT: FAIL",
            )
        )
        changed = reworded.replace("SESSION-RECOVERY", "USAGE-LIMIT")

        first_fingerprints = loop_manager.validation_failure_fingerprints(
            "review", first
        )
        reworded_fingerprints = loop_manager.validation_failure_fingerprints(
            "review", reworded
        )
        changed_fingerprints = loop_manager.validation_failure_fingerprints(
            "review", changed
        )
        with self.assertRaisesRegex(
            loop_manager.ReviewerProtocolError, "유효한 ISSUE_ID가 없습니다"
        ):
            loop_manager.validation_failure_fingerprints(
                "review", "first free-form explanation\nVERDICT: FAIL"
            )
        with self.assertRaisesRegex(
            loop_manager.ReviewerProtocolError, "형식이 유효하지 않습니다"
        ):
            loop_manager.validation_failure_fingerprints(
                "review", "ISSUE_ID: app.py:10 prose\nVERDICT: FAIL"
            )

        self.assertEqual(first_fingerprints, reworded_fingerprints)
        self.assertEqual(
            tuple(normalized for _fingerprint, normalized in first_fingerprints),
            (
                "ISSUE_ID:loop_manager.py::run_project::session-recovery",
                "ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage",
            ),
        )
        first_by_issue = {
            normalized: fingerprint
            for fingerprint, normalized in first_fingerprints
        }
        changed_by_issue = {
            normalized: fingerprint
            for fingerprint, normalized in changed_fingerprints
        }
        self.assertEqual(
            first_by_issue[
                "ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage"
            ],
            changed_by_issue[
                "ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage"
            ],
        )
        self.assertEqual(
            set(first_by_issue) & set(changed_by_issue),
            {"ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage"},
        )

    def test_review_issue_id_accepts_actual_execplan_path(self):
        review = (
            "ISSUE_ID: docs/plans/[G-025] Bootstrap Remaining Completed "
            "Requirement Archive.md::Decision-Log::archive-eligibility-evidence\n"
            "eligibility evidence does not match the source plans\n"
            "VERDICT: FAIL"
        )

        verdict, issue_ids = loop_manager.parse_reviewer_result(review)

        self.assertEqual(verdict, "FAIL")
        self.assertEqual(
            issue_ids,
            (
                "ISSUE_ID:docs/plans/[g-025] bootstrap remaining completed "
                "requirement archive.md::decision-log::archive-eligibility-evidence",
            ),
        )

    def test_specification_document_fingerprint_tracks_architecture_revision(self):
        import json

        evidence = {
            'kind': 'missing',
            'locations': [
                {'path': 'docs/ARCHITECTURE.md', 'section': 'Migration policy'},
            ],
            'problem': 'The migration decision is absent',
            'implementation_choice': 'Which migration to use',
            'question': 'Which migration should be used?',
        }
        review = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"

        def git(_repo, _command, revision_path, **_kwargs):
            revision = revision_path.split(':', 1)[0]
            return mock.Mock(returncode=0, stdout=(
                'Old policy' if revision in {'base', 'unrelated'} else 'New policy'
            ))

        with mock.patch.object(loop_manager, 'git', side_effect=git) as shown:
            base = loop_manager._revision_review_issue_fingerprint(
                Path('/repo'), 'base', review,
            )
            unrelated = loop_manager._revision_review_issue_fingerprint(
                Path('/repo'), 'unrelated', review,
            )
            changed = loop_manager._revision_review_issue_fingerprint(
                Path('/repo'), 'changed', review,
            )
        self.assertEqual(base, unrelated)
        self.assertNotEqual(base, changed)
        self.assertTrue(all(
            call.args[2].endswith(':docs/ARCHITECTURE.md')
            for call in shown.call_args_list
        ))

    def test_remote_checkpoint_reviewer_specification_skips_fail_fingerprint(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        prior = 'ISSUE_ID: app.py::review::blocking\nReview failure\nVERDICT: FAIL'
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence=prior, release_fingerprint='prior-input',
            plan_values={
                'review_result': prior,
                'worker_result': worker_result(),
                'publication_remote_oid': 'head',
            }, clear_projection=True,
        )
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        evidence = {
            'kind': 'missing',
            'locations': [
                {'path': 'docs/ARCHITECTURE.md', 'section': 'Migration policy'},
            ],
            'problem': 'The decision is absent',
            'implementation_choice': 'Which migration to use',
            'question': 'Which migration should be used?',
        }
        review = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"
        snapshot = mock.Mock()
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
            mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', return_value=(0, review)),
            mock.patch.object(loop_manager, '_remote_checkpoint_has_new_failure') as fingerprint,
        ):
            result = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertEqual(result, (False, review, 'specification'))
        fingerprint.assert_not_called()

    def test_review_issue_id_rejects_unsafe_repo_relative_paths(self):
        invalid_paths = (
            "/docs/plans/plan.md",
            "docs/../plan.md",
            "docs/./plan.md",
            "docs//plan.md",
            "docs\\plans\\plan.md",
            "c:/repo/plan.md",
            "docs/plans/control\x01.md",
            "docs/ plan.md",
        )
        for path in invalid_paths:
            review = (
                f"ISSUE_ID: {path}::stable-symbol::blocking\n"
                "VERDICT: FAIL"
            )
            with self.subTest(path=path), self.assertRaisesRegex(
                loop_manager.ReviewerProtocolError,
                "ISSUE_ID 형식이 유효하지 않습니다",
            ):
                loop_manager.parse_reviewer_result(review)

        self.assertEqual(
            loop_manager.parse_reviewer_result(
                "ISSUE_ID: docs/plans/plan.md ::stable-symbol::blocking\nVERDICT: FAIL"
            ),
            ("FAIL", ("ISSUE_ID:docs/plans/plan.md::stable-symbol::blocking",)),
        )

    def test_verify_fingerprint_uses_full_output_before_feedback_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            scripts = worktree / "scripts"
            scripts.mkdir()
            (scripts / "verify.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (scripts / "verify.ps1").write_text("exit 0\n", encoding="utf-8")
            lines = [
                f"FAIL tests/test_{index:04}.py::test_case assertion {index}"
                for index in range(400)
            ]
            ordered = "\n".join(lines)
            reordered = "\n".join(reversed(lines))
            results = [(False, False, ordered), (False, False, reordered)]
            with mock.patch("loop_manager.run_verify_process", side_effect=results):
                first_passed, first_output = loop_manager.verify(worktree)
                second_passed, second_output = loop_manager.verify(worktree)

        self.assertFalse(first_passed)
        self.assertFalse(second_passed)
        self.assertLessEqual(len(first_output), 8000)
        self.assertLessEqual(len(second_output), 8000)
        self.assertNotEqual(first_output, second_output)
        first_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "verify", first_output, worktree
        )
        second_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "verify", second_output, worktree
        )
        self.assertEqual(first_fingerprint, second_fingerprint)

    def test_legacy_review_block_uses_base_and_worktree_fingerprints_across_rebase(self):
        from loop_manager_internal.storage import runtime_state

        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._active_worker_project(directory)
            loop_manager.migrate_legacy_execplans(
                connection, target_repo=str(repo)
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            worktree = Path(project["worktree"])
            plan_path = str(plan["path"])
            plan_file = worktree / plan_path
            plan_file.write_text(
                plan_file.read_text(encoding="utf-8")
                + "\n## Progress\n\n- [x] Worker checkpoint\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", plan_path)
            git_command(worktree, "commit", "-m", "Record Worker checkpoint")
            base_sha = str(project["active_sha"])
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'REVIEWING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        reviewer_session_id = 'reviewer-session'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        base_sha,
                        str(project["branch"]),
                        str(worktree),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET phase = 'REVIEWING', validated_base_sha = ?,
                        reviewer_session_id = 'reviewer-session'
                    WHERE repo = ?
                    """,
                    (base_sha, str(repo)),
                )

            output = (
                "ISSUE_ID: docs/PLANS.md::stable-contract::blocking\n"
                "Contract is still missing.\nVERDICT: FAIL"
            )

            def record_legacy_review_block() -> None:
                active = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                ).fetchone()
                blocked_input, blocked_worktree_input = (
                    loop_manager._blocked_validation_input_fingerprints(
                        connection, repo, str(repo), worktree, active,
                    )
                )
                blocked_revision = loop_manager._blocked_validation_base_revision(
                    connection, str(repo), active,
                )
                runtime_state.RuntimeStateRepository(connection).block_plan(
                    str(repo), "G-001", reason_code="review_retry_exhausted",
                    evidence=output, release_fingerprint=blocked_input,
                    plan_values={
                        "blocked_fingerprint": blocked_input,
                        "blocked_worktree_fingerprint": blocked_worktree_input,
                        "blocked_review_fingerprint": (
                            loop_manager._revision_review_issue_fingerprint(
                                repo, blocked_revision, output,
                            )
                        ),
                        "agent_result_snapshot": (
                            loop_manager.capture_git_snapshot(worktree).serialize()
                        ),
                    },
                    clear_projection=True,
                )

            for _ in range(4):
                decision = loop_manager.transition_validation_failure(
                    connection, str(repo), worktree, "review", output
                )
            self.assertFalse(decision.blocked)
            record_legacy_review_block()

            blocked = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            base_fingerprint = loop_manager._revision_execplan_input_fingerprint(
                repo, base_sha, blocked
            )
            worktree_fingerprint = (
                loop_manager._worktree_execplan_input_fingerprint(
                    worktree, blocked
                )
            )
            self.assertEqual(blocked["blocked_fingerprint"], base_fingerprint)
            self.assertEqual(
                blocked["blocked_worktree_fingerprint"],
                worktree_fingerprint,
            )
            self.assertEqual(
                blocked["blocked_review_fingerprint"],
                loop_manager._revision_review_issue_fingerprint(
                    repo, base_sha, output
                ),
            )
            self.assertNotEqual(base_fingerprint, worktree_fingerprint)

            self.assertFalse(
                loop_manager.resume_blocked_execplan_if_inputs_changed(
                    connection, repo, str(repo), "main", state_dir
                )
            )
            unchanged = connection.execute(
                "SELECT status, review_failure_history, reviewer_session_id "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(unchanged["status"], "PLAN_BLOCKED")
            self.assertEqual(
                next(iter(json.loads(unchanged["review_failure_history"]).values()))[
                    "occurrences"
                ],
                4,
            )
            self.assertEqual(
                unchanged["reviewer_session_id"], "reviewer-session"
            )

            with connection:
                connection.execute(
                    "UPDATE exec_plans SET blocked_review_fingerprint = NULL "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                )
            (repo / "README.md").write_text(
                "# Unrelated remote change\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change unrelated remote input")
            self.assertFalse(
                loop_manager.resume_blocked_execplan_if_inputs_changed(
                    connection, repo, str(repo), "main", state_dir
                )
            )

            (repo / "docs/PLANS.md").write_text(
                "# Plans\n\nDocument the missing maintenance contract.\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Resolve structured review input")
            latest_sha = git_command(repo, "rev-parse", "origin/main")
            with mock.patch.object(loop_manager, 'pr_for_branch', return_value=None):
                self.assertTrue(
                    loop_manager.resume_blocked_execplan_if_inputs_changed(
                        connection, repo, str(repo), "main", state_dir
                    )
                )
            resumed = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed["status"], "READY")
            self.assertIsNone(resumed["blocked_fingerprint"])
            self.assertIsNone(resumed["blocked_worktree_fingerprint"])
            self.assertIsNone(resumed["blocked_review_fingerprint"])
            self.assertEqual(json.loads(resumed["review_failure_history"]), {})
            self.assertEqual(
                git_command(worktree, "merge-base", "--is-ancestor", latest_sha, "HEAD"),
                "",
            )

            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'REVIEWING', active_sha = ?,
                        reviewer_session_id = 'reviewer-session'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (latest_sha, str(repo)),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET phase = 'REVIEWING', active_sha = ?,
                        validated_base_sha = ?, branch = ?, worktree = ?,
                        plan_path = ?, reviewer_session_id = 'reviewer-session'
                    WHERE repo = ?
                    """,
                    (
                        latest_sha,
                        latest_sha,
                        str(project["branch"]),
                        str(worktree),
                        plan_path,
                        str(repo),
                    ),
                )
            for _ in range(4):
                decision = loop_manager.transition_validation_failure(
                    connection, str(repo), worktree, "review", output
                )
            self.assertFalse(decision.blocked)
            record_legacy_review_block()
            refreshed = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                refreshed["blocked_fingerprint"],
                loop_manager._revision_execplan_input_fingerprint(
                    repo, latest_sha, refreshed
                ),
            )
            connection.close()
