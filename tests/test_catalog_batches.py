import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
import loop_manager
from loop_manager_internal.application.contracts import PlanIdAllocation
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
from loop_manager_internal.planning import planner_publication
from loop_manager_internal.planning import planner_validation
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from tests.support.database import AutomationCatalogFixture
from tests.support.catalog import make_catalog_root, plan_text, write_execplan, planner_git_observations


class CatalogBatchesTest(AutomationCatalogFixture, unittest.TestCase):
    def test_execplan_catalog_is_deterministic_and_indexed_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-003", "Active")
            archived = write_execplan(root, "G-001", "Historical")
            archive_path = root / "docs/plans/archive" / archived.name
            archive_path.parent.mkdir()
            archived.rename(archive_path)

            catalog = loop_manager.load_execplan_catalog(root)
            snapshot = loop_manager.load_execplan_snapshot(root)

        self.assertEqual(list(catalog), ["G-003"])
        self.assertEqual(list(snapshot.all), ["G-001", "G-003"])
        self.assertEqual(snapshot.next_plan_id, "G-004")

    def test_compact_catalog_excludes_merged_bodies_and_has_only_allowed_fields(self):
        connection = self._connection()
        self._insert_plan(connection, "G-010", "MERGED", priority="critical")
        self._insert_plan(
            connection,
            "G-002",
            "READY",
            priority="high",
            depends_on=("G-001",),
        )

        catalog = json.loads(loop_manager.compact_execplan_catalog(connection, "repo"))
        prompt = loop_manager.planner_prompt(None, json.dumps(catalog))

        self.assertEqual([item["id"] for item in catalog], ["G-002"])
        self.assertEqual(
            set(catalog[0]),
            {"id", "title", "status", "sources", "depends_on", "priority"},
        )
        self.assertNotIn("G-010", prompt)
        self.assertIn("G-002", prompt)
        self.assertIn("기존 docs/plans 전체를 기본 입력으로 읽지 마라", prompt)

    def test_batch_validation_normalizes_new_plan_ids_and_structural_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing", priority=None)
            second = write_execplan(repo, "G-005", "Second", priority="high")
            second.write_text(
                second.read_text(encoding="utf-8") + "\nPlanning note: G-999 remains literal prose.\n",
                encoding="utf-8",
            )
            third = write_execplan(
                repo, "G-006",
                "Third",
                priority="critical",
                depends_on=("G-005",),
            )

            with planner_git_observations({existing.relative_to(repo).as_posix(): existing.read_text(encoding='utf-8')}):
                plans = loop_manager.validate_planner_result(
                    repo, 'base',
                    [second.relative_to(repo).as_posix(), third.relative_to(repo).as_posix()],
                )

            self.assertEqual([plan["id"] for _path, plan in plans.plans], ["G-002", "G-003"])
            self.assertEqual(
                [allocation.source_id for allocation in plans.id_allocations],
                ["G-005", "G-006"],
            )
            self.assertEqual(plans.plans[1][1]["depends_on"], ["G-002"])
            self.assertIn(
                "Planning note: G-999 remains literal prose.",
                (repo / "docs/plans/[G-002] Second.md").read_text(encoding="utf-8"),
            )

        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            gap = write_execplan(repo, "G-003", "Gap")

            with planner_git_observations({existing.relative_to(repo).as_posix(): existing.read_text(encoding='utf-8')}):
                result = loop_manager.validate_planner_result(
                    repo,
                    'base',
                    [gap.relative_to(repo).as_posix()],
                )
            self.assertEqual([plan["id"] for _path, plan in result.plans], ["G-002"])
            self.assertFalse(gap.exists())
            self.assertTrue((repo / "docs/plans/[G-002] Gap.md").is_file())

    def test_batch_normalization_rewrites_planned_snapshot_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            candidate = write_execplan(repo, "G-005", "Candidate")
            base_text = existing.read_text(encoding="utf-8")
            current_text = plan_text("G-001", "Existing", depends_on=("G-005",))
            existing.write_text(current_text, encoding="utf-8")
            existing_path = existing.relative_to(repo).as_posix()
            snapshot_plan = ReadyPlanSnapshot(
                "G-001", existing_path, "Existing", ("docs/PRODUCT.md",), (),
                "low", "low", "normal",
                hashlib.sha256(base_text.encode("utf-8")).hexdigest(),
                "a" * 40, base_text,
            )
            snapshot = MutableReadySnapshot(
                str(repo), "base", (snapshot_plan,),
                mutable_snapshot_token(str(repo), "base", (snapshot_plan,)),
            )

            with planner_git_observations({existing_path: base_text}):
                result = loop_manager.validate_planner_result(
                    repo, "base",
                    [existing_path, candidate.relative_to(repo).as_posix()],
                    mutable_snapshot=snapshot,
                )

            self.assertEqual(result.plans[0][1]["id"], "G-002")
            self.assertEqual(result.revisions[0].plan["id"], "G-001")
            self.assertEqual(result.revisions[0].plan["depends_on"], ["G-002"])
            self.assertEqual(loop_manager._parse_execplan(repo, existing)["depends_on"], ["G-002"])

    def test_unknown_snapshot_dependency_cannot_be_captured_by_assigned_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            candidate = write_execplan(repo, "G-005", "Candidate")
            candidate_bytes = candidate.read_bytes()
            base_text = existing.read_text(encoding="utf-8")
            existing.write_text(
                plan_text("G-001", "Existing", depends_on=("G-002",)),
                encoding="utf-8",
            )
            existing_path = existing.relative_to(repo).as_posix()
            snapshot_plan = ReadyPlanSnapshot(
                "G-001", existing_path, "Existing", ("docs/PRODUCT.md",), (),
                "low", "low", "normal",
                hashlib.sha256(base_text.encode("utf-8")).hexdigest(),
                "a" * 40, base_text,
            )
            snapshot = MutableReadySnapshot(
                str(repo), "base", (snapshot_plan,),
                mutable_snapshot_token(str(repo), "base", (snapshot_plan,)),
            )

            with planner_git_observations({existing_path: base_text}), self.assertRaisesRegex(
                loop_manager.PlannerValidationError, "dependency 대상이 없습니다"
            ):
                loop_manager.validate_planner_result(
                    repo, "base",
                    [existing_path, candidate.relative_to(repo).as_posix()],
                    mutable_snapshot=snapshot,
                )

            self.assertEqual(candidate.read_bytes(), candidate_bytes)
            self.assertFalse((repo / "docs/plans/[G-002] Candidate.md").exists())

    def test_batch_normalization_recovers_original_after_interrupted_target_install(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            candidate = write_execplan(repo, "G-005", "Candidate")
            original = candidate.read_bytes()
            real_link = os.link
            link_calls = 0

            def interrupt_after_sources_are_removed(source, target, *args, **kwargs):
                nonlocal link_calls
                link_calls += 1
                if link_calls == 1:
                    raise KeyboardInterrupt("simulated normalization interruption")
                real_link(source, target, *args, **kwargs)

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }), mock.patch.object(
                planner_validation.os, "link", side_effect=interrupt_after_sources_are_removed,
            ), self.assertRaisesRegex(KeyboardInterrupt, "simulated normalization interruption"):
                loop_manager.validate_planner_result(
                    repo, "base", [candidate.relative_to(repo).as_posix()],
                )

            self.assertEqual(link_calls, 1)
            self.assertTrue(candidate.is_file())
            self.assertEqual(candidate.read_bytes(), original)
            self.assertFalse((repo / "docs/plans/[G-002] Candidate.md").exists())
            self.assertFalse(planner_validation._plan_id_normalization_journal_path(repo).exists())

    def test_validation_resumes_with_dirty_paths_from_interrupted_normalization(self):
        from loop_manager_internal.maintenance.maintenance_planning import (
            MaintenancePlanningContext, MaintenancePlanMode,
        )

        for maintenance in (False, True):
            with self.subTest(maintenance=maintenance), tempfile.TemporaryDirectory() as directory:
                repo = make_catalog_root(directory)
                existing = write_execplan(repo, "G-001", "Existing")
                first = write_execplan(repo, "G-005", "First")
                second = write_execplan(repo, "G-006", "Second", depends_on=("G-005",))
                documents = {existing.relative_to(repo).as_posix(): existing.read_text()}
                originals = {path: path.read_bytes() for path in (first, second)}
                real_link = os.link
                calls = 0

                def interrupt_second_install(source, target, *args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise KeyboardInterrupt("process stopped")
                    return real_link(source, target, *args, **kwargs)

                # Leave the durable journal and staged files as a killed process would.
                with planner_git_observations(documents), mock.patch.object(
                    planner_validation, "_recover_interrupted_plan_id_normalization",
                    return_value=set(),
                ), mock.patch.object(planner_validation.os, "link", side_effect=interrupt_second_install):
                    with self.assertRaises(KeyboardInterrupt):
                        loop_manager.validate_planner_result(
                            repo, "base", [path.relative_to(repo).as_posix() for path in originals],
                        )
                journal = planner_validation._plan_id_normalization_journal_path(repo)
                self.assertTrue(journal.exists())
                dirty = [path.relative_to(repo).as_posix() for path in (repo / "docs/plans").iterdir()
                         if path != existing]
                self.assertTrue(any(path.endswith(".tmp") for path in dirty))
                self.assertIn("docs/plans/[G-002] First.md", dirty)

                def changed_files(worktree, since=None):
                    self.assertEqual(worktree, repo)
                    if since is not None:
                        self.assertEqual(since, "base")
                        # No commits were made: base..HEAD excludes restored untracked plans.
                        return []
                    return [path.relative_to(repo).as_posix()
                            for path in (repo / "docs/plans").iterdir()
                            if path != existing]

                with planner_git_observations(documents), mock.patch.object(
                    loop_manager, "changed_files", side_effect=changed_files,
                ) as observed_changes, mock.patch.object(
                    loop_manager, "_assert_maintenance_context_current",
                ), mock.patch.object(loop_manager, "_git_tree_entries", return_value={
                    path.encode(): (b"blob", b"observed") for path in documents
                }):
                    if maintenance:
                        context = MaintenancePlanningContext(
                            str(repo), MaintenancePlanMode.BARRIER, "base", 1, "assessment", (),
                        )
                        result = loop_manager.validate_maintenance_planner_result(
                            self._connection(), context, repo, dirty,
                            refactoring_plan_ids=(), allow_general_plans=True,
                        )
                    else:
                        result = loop_manager.validate_planner_result(repo, "base", dirty)
                    self.assertIsNotNone(result)
                    observed_changes.assert_any_call(repo, "base")
                    observed_changes.assert_any_call(repo)
                    # An unrelated stale path has no recovery receipt and stays out of scope.
                    unrelated = ["docs/plans/.plan-id-normalize-unowned.tmp"]
                    with self.assertRaises(RuntimeError):
                        if maintenance:
                            loop_manager.validate_maintenance_planner_result(
                                self._connection(), context, repo, unrelated,
                                refactoring_plan_ids=(), allow_general_plans=True,
                            )
                        else:
                            loop_manager.validate_planner_result(repo, "base", unrelated)
                self.assertEqual([plan["id"] for _, plan in result.plans], ["G-002", "G-003"])
                self.assertEqual(result.plans[1][1]["depends_on"], ["G-002"])
                self.assertEqual([item.source_id for item in result.id_allocations], ["G-005", "G-006"])
                self.assertFalse(journal.exists())
                self.assertFalse(list((repo / "docs/plans").glob(".plan-id-normalize-*.tmp")))
                self.assertFalse(first.exists())
                self.assertFalse(second.exists())

    def test_batch_normalization_recovers_original_after_partial_stage_write(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            candidate = write_execplan(repo, "G-005", "Candidate")
            original = candidate.read_bytes()
            real_write = planner_validation._write_plan_normalization_temp
            write_calls = 0

            def partially_write_first_temp(path, contents):
                nonlocal write_calls
                write_calls += 1
                if write_calls == 1:
                    with path.open("xb") as stream:
                        stream.write(contents[:7])
                        stream.flush()
                    raise OSError("simulated partial normalization write")
                real_write(path, contents)

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }), mock.patch.object(
                planner_validation, "_write_plan_normalization_temp",
                side_effect=partially_write_first_temp,
            ), self.assertRaisesRegex(RuntimeError, "원자적으로 정규화"):
                loop_manager.validate_planner_result(
                    repo, "base", [candidate.relative_to(repo).as_posix()],
                )

            self.assertGreaterEqual(write_calls, 2)
            self.assertEqual(candidate.read_bytes(), original)
            self.assertFalse((repo / "docs/plans/[G-002] Candidate.md").exists())
            self.assertFalse(planner_validation._plan_id_normalization_journal_path(repo).exists())

    def test_new_plan_id_collision_skips_archived_registered_and_cancelled_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            archived = write_execplan(repo, "G-004", "Archived")
            archive_path = repo / "docs/plans/archive" / archived.name
            archived_text = archived.read_text(encoding="utf-8")
            archive_path.parent.mkdir()
            archived.rename(archive_path)
            existing_text = existing.read_text(encoding="utf-8") + (
                "\n## User Decision\n\n"
                "Decision: approve\n"
                f"Revision: {'a' * 64}\n"
                "Answer: Keep the existing behavior and history.\n"
            )
            existing.write_text(existing_text, encoding="utf-8")
            candidate = write_execplan(repo, "G-001", "Decision")
            draft = plan_text("G-001", "Decision").replace(
                "risk: low\n", "risk: low\ndraft: true\n"
            ) + (
                "\n## Draft Decision\n\n"
                "Question: Which approach should this goal use?\n"
                "Facts: Both preserve the current behavior.\n"
                "Options: A keeps history; B keeps only the latest value.\n"
                "Impact: The choice changes recovery behavior.\n"
            )
            candidate.write_text(draft, encoding="utf-8")
            connection = self._connection(str(repo))
            self._insert_plan(connection, "G-001", "READY", repo=str(repo), title="Existing")
            existing_path = existing.relative_to(repo).as_posix()
            snapshot_plan = ReadyPlanSnapshot(
                "G-001", existing_path, "Existing", ("docs/PRODUCT.md",), (),
                "low", "low", "normal",
                hashlib.sha256(existing_text.encode("utf-8")).hexdigest(),
                "a" * 40, existing_text,
            )
            mutable_snapshot = MutableReadySnapshot(
                str(repo), "base", (snapshot_plan,),
                mutable_snapshot_token(str(repo), "base", (snapshot_plan,)),
            )
            connection.execute(
                "INSERT INTO execplan_cancellations "
                "(repo, plan_id, path, title, sources, depends_on, complexity, risk, "
                "priority, content_hash, plan_sha, deletion_commit) "
                "VALUES (?, 'G-009', 'docs/plans/[G-009] Cancelled.md', 'Cancelled', "
                "'[\"docs/PRODUCT.md\"]', '[]', 'low', 'low', 'normal', 'hash', 'sha', 'commit')",
                (str(repo),),
            )
            connection.commit()

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing_text,
            }):
                result = loop_manager.validate_planner_result(
                    repo, "base", [candidate.relative_to(repo).as_posix()],
                    connection=connection, repo=str(repo), mutable_snapshot=mutable_snapshot,
                )
                resumed = loop_manager.validate_planner_result(
                    repo, "base", ["docs/plans/[G-010] Decision.md"],
                    connection=connection, repo=str(repo), mutable_snapshot=mutable_snapshot,
                )

            target = repo / "docs/plans/[G-010] Decision.md"
            target_text = target.read_text(encoding="utf-8")
            self.assertEqual([path for path, _plan in result.plans], ["docs/plans/[G-010] Decision.md"])
            self.assertEqual(result.id_allocations[0].source_id, "G-001")
            self.assertEqual(result.id_allocations[0].target_id, "G-010")
            self.assertEqual([plan["id"] for _path, plan in resumed.plans], ["G-010"])
            self.assertIs(loop_manager._parse_execplan(repo, target)["draft"], True)
            self.assertIn("Question: Which approach should this goal use?", target_text)
            self.assertEqual(target_text.replace("id: G-010", "id: G-001"), draft)
            self.assertEqual(existing.read_text(encoding="utf-8"), existing_text)
            self.assertEqual(archive_path.read_text(encoding="utf-8"), archived_text)

            from loop_manager_internal.catalog import execplan_catalog, execplan_drafts
            from loop_manager_internal.storage import execplan_draft_state
            published_plan = result.plans[0][1]
            observed_plan = loop_manager._parse_execplan(repo, target)
            self.assertEqual(published_plan["draft_scope"], observed_plan["draft_scope"])
            execplan_draft_state.ensure_schema(connection)
            content_hash = execplan_catalog.plan_content_hash(target_text)
            inputs = execplan_drafts.input_digests(repo, published_plan)
            execplan_draft_state.observe(
                connection, str(repo), result.plans[0][0], published_plan,
                content_hash, "b" * 40, inputs=inputs, agent=True,
            )
            published_revision = execplan_draft_state.evidence(
                connection, str(repo), "G-010",
            )["revision"]
            execplan_draft_state.observe(
                connection, str(repo), result.plans[0][0], observed_plan,
                content_hash, "b" * 40,
                inputs=execplan_drafts.input_digests(repo, observed_plan),
            )
            self.assertEqual(
                execplan_draft_state.evidence(connection, str(repo), "G-010")["revision"],
                published_revision,
            )
            approved_text = target_text.replace("draft: true\n", "") + (
                "\n## User Decision\n\n"
                "Decision: approve\n"
                f"Revision: {published_revision}\n"
                "Answer: Keep the existing behavior and history.\n"
            )
            approved_plan = execplan_catalog.parse_execplan_text(
                result.plans[0][0], approved_text, root=repo,
                require_priority=True, allow_archive_fallback=False,
            )
            execplan_draft_state.observe(
                connection, str(repo), result.plans[0][0], approved_plan,
                execplan_catalog.plan_content_hash(approved_text), "c" * 40,
                inputs=execplan_drafts.input_digests(repo, approved_plan),
            )
            self.assertFalse(
                execplan_draft_state.evidence(connection, str(repo), "G-010")["pending"]
            )

    def test_unknown_dependency_cannot_be_captured_by_reallocated_plan_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            first = write_execplan(repo, "G-005", "First")
            second = write_execplan(
                repo, "G-006", "Second", depends_on=("G-002",),
            )
            originals = {path: path.read_bytes() for path in (first, second)}

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }), self.assertRaisesRegex(
                loop_manager.PlannerValidationError, "dependency 대상이 없습니다"
            ):
                loop_manager.validate_planner_result(
                    repo, "base", [
                        first.relative_to(repo).as_posix(),
                        second.relative_to(repo).as_posix(),
                    ],
                )

            self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_frontmatter_whitespace_is_supported_for_remapped_and_consecutive_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            first = write_execplan(repo, "G-005", "First")
            second = write_execplan(
                repo, "G-006", "Second", depends_on=("G-005",),
            )
            first.write_text(
                first.read_text(encoding="utf-8").replace("id: G-005", "id : G-005"),
                encoding="utf-8",
            )
            second.write_text(
                second.read_text(encoding="utf-8")
                .replace("id: G-006", "id : G-006")
                .replace("depends_on:\n", "depends_on: \n"),
                encoding="utf-8",
            )

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }):
                result = loop_manager.validate_planner_result(
                    repo, "base", [
                        first.relative_to(repo).as_posix(),
                        second.relative_to(repo).as_posix(),
                    ],
                )

            self.assertEqual(
                [plan["id"] for _path, plan in result.plans], ["G-002", "G-003"],
            )
            self.assertEqual(result.plans[1][1]["depends_on"], ["G-002"])
            self.assertIn("id : G-002", (repo / result.plans[0][0]).read_text(encoding="utf-8"))
            self.assertIn(
                "depends_on: \n  - G-002",
                (repo / result.plans[1][0]).read_text(encoding="utf-8"),
            )

        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            consecutive = write_execplan(repo, "G-002", "Consecutive")
            consecutive.write_text(
                consecutive.read_text(encoding="utf-8")
                .replace("id: G-002", "id : G-002")
                .replace("depends_on: []", "depends_on: "),
                encoding="utf-8",
            )

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }):
                result = loop_manager.validate_planner_result(
                    repo, "base", [consecutive.relative_to(repo).as_posix()],
                )

            self.assertEqual(result.plans[0][1]["id"], "G-002")
            self.assertIn("id : G-002", consecutive.read_text(encoding="utf-8"))

    def test_batch_path_renames_free_destination_paths_before_assigning_them(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-010", "Existing")
            first = write_execplan(repo, "G-001", "Shared title")
            first.write_text(
                first.read_text(encoding="utf-8").replace("Test plan.", "First goal."),
                encoding="utf-8",
            )
            second = write_execplan(
                repo, "G-011", "Shared title", depends_on=("G-001",),
            )
            second.write_text(
                second.read_text(encoding="utf-8").replace("Test plan.", "Second goal."),
                encoding="utf-8",
            )

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }):
                result = loop_manager.validate_planner_result(
                    repo, "base", [
                        first.relative_to(repo).as_posix(),
                        second.relative_to(repo).as_posix(),
                    ],
                )

            self.assertEqual(
                [path for path, _plan in result.plans],
                ["docs/plans/[G-011] Shared title.md", "docs/plans/[G-012] Shared title.md"],
            )
            self.assertEqual(result.plans[1][1]["depends_on"], ["G-011"])
            self.assertFalse(first.exists())
            self.assertIn("First goal.", (repo / result.plans[0][0]).read_text(encoding="utf-8"))
            self.assertIn("Second goal.", (repo / result.plans[1][0]).read_text(encoding="utf-8"))

    def test_ambiguous_batch_ids_and_collision_dependencies_are_preserved(self):
        for dependency in ((), ("G-001",)):
            with self.subTest(dependency=dependency), tempfile.TemporaryDirectory() as directory:
                repo = make_catalog_root(directory)
                existing = write_execplan(repo, "G-001", "Existing")
                first = write_execplan(repo, "G-001", "First")
                second = write_execplan(
                    repo, "G-001" if not dependency else "G-008", "Second",
                    depends_on=dependency,
                )
                originals = {path: path.read_bytes() for path in (first, second)}
                documents = {existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8")}
                with planner_git_observations(documents), self.assertRaisesRegex(
                    loop_manager.PlannerValidationError, "모호"
                ):
                    loop_manager.validate_planner_result(
                        repo, "base", [path.relative_to(repo).as_posix() for path in (first, second)],
                    )
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_remapped_id_in_plan_body_requires_planner_clarification(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            candidate = write_execplan(repo, "G-005", "Candidate")
            original = candidate.read_text(encoding="utf-8")
            candidate.write_text(original + "\nThe G-005 implementation note is historical.\n", encoding="utf-8")
            original = candidate.read_bytes()

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }), self.assertRaisesRegex(loop_manager.PlannerValidationError, "본문 참조"):
                loop_manager.validate_planner_result(
                    repo, "base", [candidate.relative_to(repo).as_posix()],
                )

            self.assertEqual(candidate.read_bytes(), original)

    def test_new_plan_symlink_is_rejected_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            candidate = write_execplan(repo, "G-005", "Candidate")
            target = Path(directory) / "outside.md"
            target_text = plan_text("G-005", "Candidate")
            target.write_text(target_text, encoding="utf-8")
            candidate.unlink()
            try:
                candidate.symlink_to(target)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlink setup unavailable: {error}")

            with planner_git_observations({
                existing.relative_to(repo).as_posix(): existing.read_text(encoding="utf-8"),
            }), self.assertRaisesRegex(loop_manager.PlannerValidationError, "일반 파일"):
                loop_manager.validate_planner_result(
                    repo, "base", [candidate.relative_to(repo).as_posix()],
                )

            self.assertTrue(candidate.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), target_text)

    def test_id_allocation_mapping_round_trips_in_publication_manifest(self):
        snapshot = MutableReadySnapshot(
            "repo", "base", (), mutable_snapshot_token("repo", "base", ()),
        )
        result = loop_manager.ValidatedPlanningResult(
            plans=(("docs/plans/[G-003] Candidate.md", {"id": "G-003", "title": "Candidate"}),),
            publication_paths=("docs/plans/[G-003] Candidate.md",),
            mutable_snapshot=snapshot,
            id_allocations=(PlanIdAllocation(
                "docs/plans/[G-001] Candidate.md", "G-001",
                "docs/plans/[G-003] Candidate.md", "G-003",
            ),),
        )
        manifest = loop_manager._planning_result_manifest(result)
        restored = loop_manager._planning_result_from_manifest(manifest)

        self.assertEqual(restored.id_allocations, result.id_allocations)

    def test_id_allocation_commit_check_binds_target_path_and_contents(self):
        target = "docs/plans/[G-003] Candidate.md"
        contents = plan_text("G-003", "Candidate")
        parsed = loop_manager.published_execplan(target, contents)
        snapshot = MutableReadySnapshot(
            "repo", "base", (), mutable_snapshot_token("repo", "base", ()),
        )
        result = loop_manager.ValidatedPlanningResult(
            plans=((target, parsed),),
            publication_paths=(target,),
            publication_hashes=((target, hashlib.sha256(contents.encode()).hexdigest()),),
            mutable_snapshot=snapshot,
            id_allocations=(PlanIdAllocation(
                "docs/plans/[G-001] Candidate.md", "G-001", target, "G-003",
            ),),
        )

        def git(_worktree, *args, **_kwargs):
            if args == ("rev-parse", "HEAD"):
                return SimpleNamespace(stdout="commit\n")
            if args[:2] == ("rev-list", "--parents"):
                return SimpleNamespace(stdout="commit base\n")
            if args[:2] == ("cat-file", "-e"):
                return SimpleNamespace(returncode=int(args[2] != f"commit:{target}"))
            raise AssertionError(args)

        effects = planner_publication.ValidateGeneralPlanningCommitEffects(
            _nul_git_paths=lambda *_args: (target,),
            git=git,
            git_bytes=lambda _worktree, _operation, _path: contents.encode(),
        )
        planner_publication._validate_general_planning_commit(effects, Path("/repo"), result, "commit")

        tampered = plan_text("G-004", "Candidate")
        bad_result = loop_manager.ValidatedPlanningResult(
            plans=result.plans,
            publication_paths=result.publication_paths,
            publication_hashes=((target, hashlib.sha256(tampered.encode()).hexdigest()),),
            mutable_snapshot=snapshot,
            id_allocations=result.id_allocations,
        )
        bad_effects = planner_publication.ValidateGeneralPlanningCommitEffects(
            _nul_git_paths=lambda *_args: (target,),
            git=git,
            git_bytes=lambda _worktree, _operation, _path: tampered.encode(),
        )
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "유효하지 않습니다"):
            planner_publication._validate_general_planning_commit(
                bad_effects, Path("/repo"), bad_result, "commit",
            )

    def test_batch_registration_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            second_path = write_execplan(root, "G-002", "Second", priority="high")
            third_path = write_execplan(root, "G-003", "Third", priority="normal")
            plans = [
                (second_path.relative_to(root).as_posix(), loop_manager._parse_execplan(root, second_path)),
                (third_path.relative_to(root).as_posix(), loop_manager._parse_execplan(root, third_path)),
            ]
            connection = self._connection(str(root))
            connection.execute("UPDATE projects SET phase = 'PLANNING' WHERE repo = ?", (str(root),))
            self._insert_plan(connection, "G-003", "MERGED", repo=str(root))

            with self.assertRaisesRegex(RuntimeError, "충돌"):
                loop_manager.register_execplans(
                    connection,
                    str(root),
                    root,
                    plans,
                    "published-sha",
                    "fingerprint",
                )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                    (str(root),),
                ).fetchone()
            )

            connection.execute(
                "DELETE FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
                (str(root),),
            )
            connection.commit()
            loop_manager.register_execplans(
                connection,
                str(root),
                root,
                plans,
                "published-sha",
                "fingerprint",
            )
            loop_manager.register_execplans(
                connection,
                str(root),
                root,
                plans,
                "published-sha",
                "fingerprint",
            )
            rows = connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                (str(root),),
            ).fetchall()
            project = connection.execute(
                "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                (str(root),),
            ).fetchone()

        self.assertEqual([tuple(row) for row in rows], [("G-002", "READY"), ("G-003", "READY")])
        self.assertEqual(tuple(project), (None, "fingerprint"))
