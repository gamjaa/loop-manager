import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.maintenance import code_map_artifact, maintenance_file_publication, tiered_review
from loop_manager_internal.maintenance import maintenance_file_inventory, maintenance_file_review
from loop_manager_internal.storage import maintenance_file_ledger, maintenance_state
from loop_manager_internal.planning.state_ports import _PlanningStatePorts
from tests.support.maintenance_planning_case import MaintenancePlanningCase, git
from tests.support.maintenance import root_results_for
from tests.support.catalog import plan_text


class TieredPublicationTest(MaintenancePlanningCase):
    def prepare(self, *, root_candidate=False, collect_evidence=True):
        context = self._begin_barrier()
        self.connection.execute("INSERT INTO execplan_catalog_revisions (repo, revision_sha, catalog_fingerprint) "
                                "VALUES (?, ?, 'verified')", (str(self.repo), self.base_sha))
        self.connection.commit()
        repository = tiered_review.TieredReviewRepository(self.connection)
        selection = repository.select(str(self.repo), self.repo, "main", self.base_sha, 20, 1)
        assessment = repository.prepare(str(self.repo), context.cycle_revision, selection)
        for unit in repository.units(assessment):
            if unit.get("kind") == "root_placement":
                inventory = maintenance_file_inventory.tracked_source_inventory(
                    self.repo, "main", self.base_sha, include_uncertain=True,
                )
                source = unit["source"]
                reviews = list(root_results_for(self.repo, inventory))
                if root_candidate:
                    candidate = next(item for item in reviews if item["path"] == "legacy")
                    candidate.update(category="internal_implementation", destination="src/legacy",
                        references=[{"path": "legacy/module.py", "excerpt": "# tracked module"}],
                        reason="Move the internal legacy tree to its assessed destination.")
                value = {"path": source["path"], "content_hash": source["content_hash"],
                    "mode": source["mode"], "source_revision": source["revision"],
                    "root_reviews": reviews}
                output = maintenance_file_review.RESULT_PREFIX + json.dumps(value)
            else:
                value = {"digest": unit["digest"], "reviews": [{"module": part["module"],
                    "summary": "The fixture modules have no executable implementation or structural problem.",
                    "citations": [{"path": next(iter(part["records"])), "excerpt": "# tracked module"}]}
                    for part in unit["parts"]], "findings": []}
                output = tiered_review.RESULT_PREFIX + json.dumps(value)
            repository.complete(str(self.repo), unit, output, lambda path: (self.repo / path).read_bytes())
        evidence = (maintenance_file_publication.collect(self.connection, str(self.repo), self.repo, "main",
                    context.cycle_revision, self.base_sha) if collect_evidence else None)
        if not collect_evidence:
            return context, repository, evidence, None
        result = code_map_artifact.prepare(self.connection, str(self.repo), context, self.repo, evidence, None)
        return context, repository, evidence, result

    def test_prepared_tiered_promotion_applies_without_observation_and_rejects_audit_race(self):
        context, repository, evidence, _ = self.prepare()
        self.connection.commit()
        def observe(*args):
            self.assertFalse(self.connection.in_transaction)
            return True
        with mock.patch.object(maintenance_file_publication, 'sources_match_remote', side_effect=observe), \
                mock.patch.object(maintenance_file_inventory, 'verify_remote_revision') as remote:
            prepared = maintenance_file_publication.prepare_promotion(
                self.connection, str(self.repo), self.repo, 'main', self.base_sha, evidence)
            remote.assert_called_once()
        with mock.patch.object(maintenance_file_ledger, '_git', side_effect=AssertionError('Git during apply')), \
                mock.patch.object(tiered_review, 'parse_root_review_result',
                                  side_effect=AssertionError('root observation during apply')):
            with self.assertRaisesRegex(RuntimeError, 'rollback'), maintenance_state._transaction(self.connection):
                maintenance_file_publication.apply_promotion(self.connection, prepared)
                self.assertIsNotNone(repository.success(str(self.repo)))
                raise RuntimeError('rollback')
            self.assertIsNone(repository.success(str(self.repo)))
            with self.connection:
                self.connection.execute("UPDATE tiered_maintenance_result_audit SET raw_result='competing audit'")
            with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
                maintenance_file_publication.apply_promotion(self.connection, prepared)
            self.assertIsNone(repository.success(str(self.repo)))
            self.assertEqual(self.connection.execute(
                'SELECT DISTINCT raw_result FROM tiered_maintenance_result_audit').fetchone()[0], 'competing audit')

    def test_artifact_collection_checks_plans_in_the_planning_worktree(self):
        context = self._begin_barrier()
        state = object.__new__(_PlanningStatePorts)
        state._connection = self.connection
        worktree = Path(self.temporary.name) / "planning-worktree"
        evidence = object()
        result = object()
        with mock.patch.object(maintenance_file_publication, "collect", return_value=evidence) as collect, \
                mock.patch.object(code_map_artifact, "prepare", return_value=result) as prepare:
            self.assertIs(state.prepare_file_assessment_artifact(
                str(self.repo), self.repo, "main", context, worktree, object(),
            ), result)
        collect.assert_called_once_with(self.connection, str(self.repo), self.repo, "main",
            context.cycle_revision, context.active_sha, plan_repository=worktree)
        prepare.assert_called_once_with(self.connection, str(self.repo), context, worktree, evidence, mock.ANY)

    def test_module_receipts_and_static_jsonl_publish_atomically_without_file_review_claims(self):
        context, repository, evidence, result = self.prepare()
        self.assertIsNotNone(evidence.tiered)
        self.assertEqual(evidence.reviewed, ())
        self.assertEqual(evidence.code_maps, ())
        target = self.repo / code_map_artifact.ARTIFACT_PATH
        self.assertEqual(json.loads(target.read_text().splitlines()[0])["kind"], "static")
        git(self.repo, "add", code_map_artifact.ARTIFACT_PATH)
        git(self.repo, "commit", "-m", "Publish static maintenance context")
        commit = git(self.repo, "rev-parse", "HEAD")
        loop_manager.record_maintenance_publication_intent(self.connection, str(self.repo), self.repo, "main", result, commit)
        self.assertIsNone(repository.success(str(self.repo)))
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        prepare = maintenance_file_publication.prepare_promotion
        apply = maintenance_file_publication.apply_promotion
        def observe(*args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            return prepare(*args, **kwargs)
        def write(*args, **kwargs):
            self.assertTrue(self.connection.in_transaction)
            with mock.patch.object(maintenance_file_ledger, '_git', side_effect=AssertionError('Git during apply')):
                return apply(*args, **kwargs)
        with mock.patch.object(maintenance_file_publication, 'prepare_promotion', side_effect=observe), \
                mock.patch.object(maintenance_file_publication, 'apply_promotion', side_effect=write):
            cycle = loop_manager.publish_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main", result, commit)
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(repository.success(str(self.repo))["full_merged_count"], 1)
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(self.connection).last_success(str(self.repo)))
        self.assertIsNone(cycle.last_success_file_assessment_merged_count)
        self.assertEqual(loop_manager.publish_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main", result, commit), cycle)
        self.assertFalse(maintenance_state.MaintenanceRepository(self.connection).file_assessment_due(
            str(self.repo), 20, self_repository=True).required)
        with mock.patch.object(maintenance_state.MaintenanceRepository, "_merged_count", return_value=21):
            self.assertTrue(maintenance_state.MaintenanceRepository(self.connection).file_assessment_due(str(self.repo), 20).required)

    def test_root_move_candidate_requires_plan_and_keeps_link_through_publication(self):
        context, repository, _, _ = self.prepare(root_candidate=True, collect_evidence=False)
        with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
            maintenance_file_publication.collect(self.connection, str(self.repo), self.repo, "main",
                context.cycle_revision, self.base_sha)

        relative = "docs/plans/[G-002] Move Legacy Root.md"
        text = plan_text("G-002", "Move Legacy Root",
            purpose=("Move the root `legacy` tree to `src/legacy` while preserving user behavior, "
                     "the public API contract, and storage format."),
            acceptance_criteria=("Move `legacy` to `src/legacy`; preserve import paths, execution commands, "
                "launcher behavior, build and deploy entrypoints, configuration discovery, relative paths, "
                "and documentation links."),
            implementation=("Update import and command references, build/deploy setup, configuration discovery, "
                "relative paths, and documentation links while preserving user behavior, the public API "
                "contract, and storage format."))
        text = text.replace(
            "## Validation\n\n- Run the targeted unit test for this responsibility boundary.\n"
            "- The full verify must preserve existing behavior.",
            "## Validation\n\n- Run targeted unit tests for root placement and publication.\n"
            "- Run the full verify and a read-only Review.",
        )
        plan = self.repo / relative
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(text, encoding="utf-8", newline="\n")
        candidate_root = Path(self.temporary.name) / "planning-worktree"
        candidate_path = candidate_root / relative
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_bytes(plan.read_bytes())

        from loop_manager_internal.storage.maintenance_file_ledger import root_move_candidates, root_review_hash
        assessed = repository.evidence(str(self.repo), context.cycle_revision, self.base_sha)
        candidate = root_move_candidates(assessed["root_reviews"])["legacy"]
        link = {"path": "legacy", "destination": candidate["destination"],
            "root_review_hash": root_review_hash(candidate), "status": "new_plan", "plan_id": "G-002",
            "plan_path": relative, "plan_hash": tiered_review.plan_content_hash(candidate_path.read_bytes()),
            "reason": "The root references were traced and this plan preserves the existing contract."}
        repository.record_root_plan_links(str(self.repo), context.cycle_revision, self.base_sha,
            [link], plan_repository=candidate_root)
        evidence = maintenance_file_publication.collect(self.connection, str(self.repo), self.repo, "main",
            context.cycle_revision, self.base_sha, plan_repository=candidate_root)
        self.assertEqual(evidence.tiered["root_plan_links"][0]["plan_id"], "G-002")

        git(self.repo, "add", relative)
        git(self.repo, "commit", "-m", "Add root move maintenance plan")
        commit = git(self.repo, "rev-parse", "HEAD")
        prepared = maintenance_file_publication.prepare_intent(str(self.repo), context.cycle_revision,
            self.base_sha, commit, evidence, plan_repository=candidate_root)
        with mock.patch.object(maintenance_file_publication, '_verify_root_plan_files',
                               side_effect=AssertionError('root observation during intent apply')):
            with self.assertRaisesRegex(RuntimeError, 'rollback'), maintenance_state._transaction(self.connection):
                maintenance_file_publication.save_intent(self.connection, str(self.repo), context.cycle_revision,
                    self.base_sha, commit, evidence, prepared=prepared)
                raise RuntimeError('rollback')
            self.assertIsNone(self.connection.execute(
                'SELECT 1 FROM file_assessment_publications WHERE repo=?', (str(self.repo),)).fetchone())
            with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
                maintenance_file_publication.save_intent(self.connection, str(self.repo), context.cycle_revision,
                    self.base_sha, commit, evidence, prepared=replace(prepared, publication_commit='other'))
            maintenance_file_publication.save_intent(self.connection, str(self.repo), context.cycle_revision,
                self.base_sha, commit, evidence, prepared=prepared)
        self.assertEqual(maintenance_file_publication.saved_intent(self.connection, str(self.repo),
            context.cycle_revision, self.base_sha, commit), evidence)

        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        self._insert_plan("G-002", "Move Legacy Root", "READY")
        self.connection.execute("UPDATE exec_plans SET status = 'CANCELLED' WHERE repo = ? AND plan_id = ?",
                                (str(self.repo), "G-002"))
        with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
            maintenance_file_publication.promote(self.connection, str(self.repo), self.repo, "main", commit, evidence)
        self.connection.execute("UPDATE exec_plans SET status = 'READY' WHERE repo = ? AND plan_id = ?",
                                (str(self.repo), "G-002"))
        maintenance_file_publication.promote(self.connection, str(self.repo), self.repo, "main", commit, evidence)
        self.assertEqual(repository.success(str(self.repo))["root_reviews"], list(evidence.root_reviews))

    def test_saved_intent_recovers_module_results_after_restart(self):
        context, repository, evidence, result = self.prepare()
        git(self.repo, "add", code_map_artifact.ARTIFACT_PATH)
        git(self.repo, "commit", "-m", "Publish static context")
        commit = git(self.repo, "rev-parse", "HEAD")
        loop_manager.record_maintenance_publication_intent(self.connection, str(self.repo), self.repo, "main", result, commit)
        restored = maintenance_file_publication.saved_intent(self.connection, str(self.repo), context.cycle_revision, self.base_sha, commit)
        self.assertEqual(restored, evidence)
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        cycle = loop_manager.recover_maintenance_planning_batch(self.connection, str(self.repo), self.repo, "main")
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(repository.success(str(self.repo))["digest"], evidence.tiered["digest"])

    def test_published_source_drift_retains_intent_until_restart_cleanup_cas(self):
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.storage.database import GENERAL_PLANNING_PUBLICATIONS_SCHEMA
        context, repository, evidence, result = self.prepare()
        with maintenance_state._transaction(self.connection):
            self.connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
            self.connection.execute("UPDATE projects SET phase='PLANNING', active_sha=?, worktree=?, "
                "planning_workspace_owned=1, planning_attempt_fingerprint='attempt', planner_session_id='saved-session'",
                (self.base_sha, str(self.repo)))
        git(self.repo, 'add', code_map_artifact.ARTIFACT_PATH)
        git(self.repo, 'commit', '-m', 'Publish context before source drift')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, 'main', result, commit)
        git(self.repo, 'push', 'origin', 'HEAD:main')
        (self.repo / 'legacy/module.py').write_text('def changed():\n    return 1\n')
        git(self.repo, 'add', 'legacy/module.py')
        git(self.repo, 'commit', '-m', 'Advance source after context publication')
        remote = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'push', 'origin', 'HEAD:main')
        git(self.repo, 'fetch', 'origin')
        git(self.repo, 'reset', '--hard', commit)
        state = loop_manager._PlanningStatePorts(self.connection, runtime_state.RuntimeStateRepository(self.connection),
            maintenance_state.MaintenanceRepository(self.connection), self.repo / 'state')
        observe = maintenance_file_publication.sources_match_remote
        def race(*args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            value = observe(*args, **kwargs)
            with maintenance_state._transaction(self.connection):
                self.connection.execute("UPDATE projects SET planner_session_id='competitor'")
            return value
        with mock.patch.object(maintenance_file_publication, 'sources_match_remote', side_effect=race), \
                self.assertRaisesRegex(loop_manager.MaintenancePlanningError, '정리 준비 이후'):
            state.recover_maintenance_batch(str(self.repo), self.repo, 'main')
        self.assertIsNone(state.pending_publication_release(str(self.repo)))
        self.assertEqual(state.maintenance_cycle(str(self.repo)), intent)
        with maintenance_state._transaction(self.connection):
            self.connection.execute("UPDATE projects SET planner_session_id='saved-session'")
        def outside(*args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            return observe(*args, **kwargs)
        with mock.patch.object(maintenance_file_publication, 'sources_match_remote', side_effect=outside):
            self.assertEqual(state.recover_maintenance_batch(str(self.repo), self.repo, 'main'), intent)
        release = state.pending_publication_release(str(self.repo))
        self.assertTrue(release.restart)
        self.assertEqual(release.revision, commit)
        self.assertEqual(state.planning(str(self.repo)).planner_session_id, 'saved-session')
        self.assertEqual(maintenance_file_publication.saved_intent(self.connection, str(self.repo),
            context.cycle_revision, self.base_sha, commit), evidence)
        with mock.patch.object(state, 'clear_active_in_transaction', side_effect=RuntimeError('release interrupted')), \
                self.assertRaisesRegex(RuntimeError, 'release interrupted'):
            state.complete_publication_release(release, restart_revision=remote)
        self.assertEqual(state.maintenance_cycle(str(self.repo)), intent)
        self.assertEqual(state.pending_publication_release(str(self.repo)), release)
        with mock.patch.object(maintenance_file_publication, 'sources_match_remote', side_effect=AssertionError('repeat observation')):
            state.complete_publication_release(release, restart_revision=remote)
        restarted = state.maintenance_cycle(str(self.repo))
        self.assertEqual(restarted.revision, intent.revision + 1)
        self.assertEqual(restarted.phase, intent.phase)
        self.assertEqual(restarted.cycle_base_sha, remote)
        self.assertIsNone(restarted.publication_stage)
        self.assertIsNone(state.planning(str(self.repo)).phase)
        self.assertIsNone(state.pending_publication_release(str(self.repo)))
        self.assertIsNone(repository.success(str(self.repo)))

    def test_source_change_before_promotion_never_advances_global_baseline(self):
        context, repository, evidence, result = self.prepare()
        (self.repo / "legacy/module.py").write_text("def changed():\n    return 1\n")
        git(self.repo, "add", "legacy/module.py")
        git(self.repo, "commit", "-m", "Change source")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
            maintenance_file_publication.promote(self.connection, str(self.repo), self.repo, "main",
                                               git(self.repo, "rev-parse", "HEAD"), evidence)
        self.assertIsNone(repository.success(str(self.repo)))

    def test_promoted_facts_roll_back_with_publication_transaction(self):
        context, repository, evidence, result = self.prepare()
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with maintenance_state._transaction(self.connection):
                repository.promote(str(self.repo), evidence.tiered)
                raise RuntimeError("rollback")
        self.assertIsNone(repository.success(str(self.repo)))
