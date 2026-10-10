"""Pinned archive eligibility from validated declarations and state."""

import unittest

from loop_manager_internal.catalog import requirement_archive as archive


SOURCE = "docs/requirements/Feature.md"
OTHER = "docs/requirements/Other.md"
STATUSES = frozenset({"READY", "IMPLEMENTING", "MERGED"})


class ArchiveSnapshotTests(unittest.TestCase):
    def calculate(self, catalog, states, *, base="base-1", requirements=(SOURCE,),
                  cache_versions=None):
        return archive.calculate(
            base_oid=base, catalog=catalog, states=states,
            active_requirements=requirements, archived_requirements=frozenset(),
            current_plan_id="G-001", known_statuses=STATUSES,
            cache_versions=cache_versions,
        )

    def test_unrelated_new_plan_keeps_candidate_at_new_base(self):
        initial = {"G-001": {"sources": [SOURCE]}}
        before = self.calculate(initial, {"G-001": "IMPLEMENTING"})
        after = self.calculate(
            {**initial, "G-002": {"sources": [OTHER]}},
            {"G-001": "IMPLEMENTING", "G-002": "READY"},
            base="base-2", requirements=(SOURCE, OTHER),
        )
        self.assertEqual(before.candidates, (SOURCE,))
        self.assertEqual(after.candidates, (SOURCE,))
        self.assertNotEqual(before.token, after.token)

    def test_related_new_plan_excludes_candidate(self):
        snapshot = self.calculate(
            {"G-001": {"sources": [SOURCE]}, "G-002": {"sources": [SOURCE]}},
            {"G-001": "IMPLEMENTING", "G-002": "READY"},
        )
        self.assertEqual(snapshot.candidates, ())
        self.assertEqual(snapshot.related_status,
                         ((SOURCE, (("G-001", "IMPLEMENTING"), ("G-002", "READY"))),))

    def test_renamed_requirement_keeps_old_source_relation(self):
        original = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        snapshot = archive.calculate(
            base_oid="base-1",
            catalog={"G-001": {"sources": [original]},
                     "G-002": {"sources": ["docs/PRODUCT.md"]}},
            states={"G-001": "READY", "G-002": "IMPLEMENTING"},
            active_requirements=(renamed,), archived_requirements=frozenset(),
            current_plan_id="G-002", known_statuses=STATUSES,
            requirement_documents={renamed: "# [R-001] Renamed\n"},
        )
        self.assertEqual(snapshot.candidates, ())
        self.assertEqual(snapshot.related_status,
                         ((renamed, (("G-001", "READY"),)),))

    def test_valid_catalog_source_delta_overrides_old_cache(self):
        snapshot = self.calculate(
            {"G-001": {"sources": [SOURCE]}, "G-002": {"sources": [OTHER]}},
            {"G-001": "IMPLEMENTING", "G-002": "READY"},
            requirements=(SOURCE, OTHER),
        )
        self.assertEqual(snapshot.candidates, (SOURCE,))
        self.assertEqual(snapshot.with_moves((SOURCE,)).move_set, (SOURCE,))
        self.assertEqual(snapshot.with_moves((SOURCE,)).token, snapshot.token)

    def test_invalid_state_or_missing_catalog_cannot_be_candidate(self):
        with self.assertRaisesRegex(archive.ArchiveSnapshotError, "상태를 확정"):
            self.calculate({"G-001": {"sources": [SOURCE]}},
                           {"G-001": "CORRUPT"})
        with self.assertRaisesRegex(archive.ArchiveSnapshotError, "Git ExecPlan 없음"):
            self.calculate({"G-001": {"sources": [SOURCE]}},
                           {"G-001": "IMPLEMENTING", "G-002": "READY"})

    def test_archive_destination_collision_identifies_actual_path(self):
        destination = "docs/requirements/archive/Feature.md"
        with self.assertRaises(archive.ArchiveCollisionError) as caught:
            archive.calculate(
                base_oid="base", catalog={"G-001": {"sources": [SOURCE]}},
                states={"G-001": "IMPLEMENTING"},
                active_requirements=(SOURCE,),
                archived_requirements=frozenset({destination}),
                current_plan_id="G-001", known_statuses=STATUSES,
            )
        self.assertEqual(caught.exception.path, destination)

    def test_catalog_metadata_delta_changes_token_without_changing_candidate(self):
        definition = {
            "sources": [SOURCE], "path": "old", "title": "Old",
            "depends_on": [], "complexity": "low", "risk": "low",
            "priority": "normal", "content_hash": "old-hash",
        }
        old = self.calculate({"G-001": definition},
                             {"G-001": "IMPLEMENTING"})
        changes = {
            "path": "new", "title": "New", "sources": [OTHER],
            "depends_on": ["G-002"], "complexity": "high",
            "risk": "critical", "priority": "critical",
            "content_hash": "new-hash",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                new = self.calculate(
                    {"G-001": {**definition, field: value}},
                    {"G-001": "IMPLEMENTING"},
                )
                self.assertNotEqual(old.catalog_token, new.catalog_token)
                self.assertNotEqual(old.token, new.token)

    def test_cache_only_delta_changes_state_token_not_candidate(self):
        catalog = {"G-001": {"sources": [SOURCE]}}
        states = {"G-001": "IMPLEMENTING"}
        before = self.calculate(catalog, states,
                                cache_versions={"G-001": ("old-path",)})
        after = self.calculate(catalog, states,
                               cache_versions={"G-001": ("new-path",)})
        self.assertEqual(before.candidates, after.candidates)
        self.assertEqual(before.catalog_token, after.catalog_token)
        self.assertNotEqual(before.state_token, after.state_token)


if __name__ == "__main__":
    unittest.main()
