import json
from unittest import mock

import loop_manager
from loop_manager_internal.maintenance import code_map_artifact, maintenance_file_publication, tiered_review
from loop_manager_internal.storage import maintenance_file_ledger, maintenance_state
from tests.support.maintenance_planning_case import MaintenancePlanningCase, git


class TieredPublicationTest(MaintenancePlanningCase):
    def prepare(self):
        context = self._begin_barrier()
        self.connection.execute("INSERT INTO execplan_catalog_revisions (repo, revision_sha, catalog_fingerprint) "
                                "VALUES (?, ?, 'verified')", (str(self.repo), self.base_sha))
        self.connection.commit()
        repository = tiered_review.TieredReviewRepository(self.connection)
        selection = repository.select(str(self.repo), self.repo, "main", self.base_sha, 20, 1)
        assessment = repository.prepare(str(self.repo), context.cycle_revision, selection)
        for unit in repository.units(assessment):
            value = {"digest": unit["digest"], "reviews": [{"module": part["module"],
                "summary": "The fixture modules have no executable implementation or structural problem.",
                "citations": [{"path": next(iter(part["records"])), "excerpt": "# tracked module"}]}
                for part in unit["parts"]], "findings": []}
            repository.complete(str(self.repo), unit, tiered_review.RESULT_PREFIX + json.dumps(value),
                                lambda path: (self.repo / path).read_bytes())
        evidence = maintenance_file_publication.collect(self.connection, str(self.repo), self.repo, "main", context.cycle_revision, self.base_sha)
        result = code_map_artifact.prepare(self.connection, str(self.repo), context, self.repo, evidence, None)
        return context, repository, evidence, result

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
        cycle = loop_manager.publish_maintenance_planning_batch(self.connection, str(self.repo), self.repo, "main", result, commit)
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
