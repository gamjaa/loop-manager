import unittest

from loop_manager_internal.catalog import requirement_corrections


def requirement(requirement_id: str, title: str) -> str:
    return f"# [{requirement_id}] {title}\n\nRequirement body.\n"


def frontmatter_requirement(requirement_id: str, title: str, heading: str) -> str:
    return (
        "---\n"
        f"id: {requirement_id}\n"
        f"title: {title}\n"
        "owner: preserve me\n"
        "---\n"
        f"# [{heading}] Stale heading\n\nRequirement body.\n"
    )


class RequirementCorrectionSelectionTest(unittest.TestCase):
    def test_reference_protects_later_document_and_next_id_uses_archive_max(self):
        first = "docs/requirements/[R-001] First.md"
        later = "docs/requirements/[R-001] Later.md"
        archive = "docs/requirements/archive/[R-010] Old.md"
        documents = {
            first: requirement("R-001", "First"),
            later: requirement("R-001", "Later"),
            archive: requirement("R-010", "Old"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            documents,
            referenced_sources=(later,), references_complete=True,
            first_seen_order={first: 1, later: 9},
        )

        self.assertEqual(
            [(item.source_path, item.target_path, item.new_id) for item in result.corrections],
            [(first, "docs/requirements/[R-011] First.md", "R-011")],
        )
        self.assertNotIn(later, dict(result.updated_documents))
        self.assertNotIn(archive, dict(result.updated_documents))

    def test_first_remote_reflection_wins_and_ties_use_filename_text_order(self):
        a = "docs/requirements/[R-002] A.md"
        b = "docs/requirements/[R-002] B.md"
        result = requirement_corrections.plan_requirement_corrections(
            {a: requirement("R-002", "A"), b: requirement("R-002", "B")},
            references_complete=True, first_seen_order={a: 12, b: 3},
        )
        self.assertEqual([(item.source_path, item.new_id) for item in result.corrections], [(a, "R-003")])

        tied = requirement_corrections.plan_requirement_corrections(
            {a: requirement("R-002", "A"), b: requirement("R-002", "B")},
            references_complete=True, first_seen_order={a: 3, b: 3},
        )
        self.assertEqual([(item.source_path, item.new_id) for item in tied.corrections], [(b, "R-003")])

    def test_multiple_targets_get_contiguous_ids_in_filename_order_without_filling_gaps(self):
        paths = {
            "docs/requirements/[R-004] A.md": requirement("R-004", "A"),
            "docs/requirements/[R-004] B.md": requirement("R-004", "B"),
            "docs/requirements/[R-004] C.md": requirement("R-004", "C"),
            "docs/requirements/archive/[R-010] Archived.md": requirement("R-010", "Archived"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            paths, references_complete=True,
            first_seen_order={
                "docs/requirements/[R-004] A.md": 1,
                "docs/requirements/[R-004] B.md": 2,
                "docs/requirements/[R-004] C.md": 3,
            },
        )

        self.assertEqual(
            [(item.source_path.rsplit("/", 1)[-1], item.new_id) for item in result.corrections],
            [("[R-004] B.md", "R-011"), ("[R-004] C.md", "R-012")],
        )

    def test_both_referenced_or_ambiguous_documents_keep_their_id(self):
        a = "docs/requirements/[R-005] A.md"
        b = "docs/requirements/[R-005] B.md"
        docs = {a: requirement("R-005", "A"), b: requirement("R-005", "B")}

        both_referenced = requirement_corrections.plan_requirement_corrections(
            docs, referenced_sources=(a, b), references_complete=True,
        )
        self.assertEqual(both_referenced.corrections, ())

        ambiguous = requirement_corrections.plan_requirement_corrections(
            docs,
            referenced_sources=("docs/requirements/[R-005] Legacy.md",),
            references_complete=True,
        )
        self.assertEqual(ambiguous.corrections, ())
        ambiguous_reasons = {item.path: item.reason for item in ambiguous.deferred}
        self.assertTrue(all("참조" in ambiguous_reasons[path] for path in (a, b)))
        self.assertIn("미확인", ambiguous_reasons["docs/requirements/[R-005] Legacy.md"])

        unobserved = requirement_corrections.plan_requirement_corrections(
            docs, references_complete=False,
        )
        self.assertEqual(unobserved.corrections, ())
        unobserved_reasons = {item.path: item.reason for item in unobserved.deferred}
        self.assertTrue(all("참조" in unobserved_reasons[path] for path in (a, b)))

    def test_archive_collision_preserves_archive_and_changes_unreferenced_active(self):
        active = "docs/requirements/[R-006] Active.md"
        archived = "docs/requirements/archive/[R-006] Archived.md"
        docs = {
            active: requirement("R-006", "Active"),
            archived: requirement("R-006", "Archived"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            docs, references_complete=True,
        )

        self.assertEqual([(item.source_path, item.new_id) for item in result.corrections], [(active, "R-007")])
        self.assertNotIn(archived, dict(result.updated_documents))


class RequirementCorrectionEditingTest(unittest.TestCase):
    def test_frontmatter_identity_heading_filename_and_confirmed_links_sync_together(self):
        old = "docs/requirements/[R-001] Old.md"
        owner = "docs/requirements/[R-002] Owner.md"
        plan = "docs/plans/[G-003] Plan.md"
        archived_plan = "docs/plans/archive/[G-001] Archived.md"
        old_text = frontmatter_requirement("R-001", "New Title", "R-099")
        owner_text = (
            "# [R-002] Owner\n\n"
            "[requirement](%5BR-001%5D%20Old.md)\n"
            "`[code](%5BR-001%5D%20Old.md)`\n"
            "    [example](%5BR-001%5D%20Old.md)\n"
            "`multi-line code begins\n"
            "[demo](%5BR-001%5D%20Old.md)\n"
            "and ends here`\n"
        )
        plan_text = (
            "---\n"
            "id: G-003\n"
            "title: Plan\n"
            "sources:\n"
            f"  - {old}\n"
            "---\n\n"
            "## Purpose\n\n"
            "[requirement](../requirements/%5BR-001%5D%20Old.md#scope)\n\n"
            "## User Decision\n\n"
            "[keep](../requirements/%5BR-001%5D%20Old.md)\n"
        )
        archive_text = "[old](../../requirements/%5BR-001%5D%20Old.md)\n"
        requirements = {
            old: old_text,
            owner: owner_text,
        }
        editable = {
            **requirements,
            plan: plan_text,
            archived_plan: archive_text,
        }

        result = requirement_corrections.plan_requirement_corrections(
            requirements, referenced_sources=(old,), references_complete=True,
            editable_documents=editable,
        )

        new = "docs/requirements/[R-001] New Title.md"
        corrected = dict(result.updated_documents)
        self.assertEqual(len(result.corrections), 1)
        self.assertEqual(result.corrections[0].target_path, new)
        self.assertEqual(
            corrected[new],
            (
                "---\nid: R-001\ntitle: New Title\nowner: preserve me\n---\n"
                "# [R-001] New Title\n\nRequirement body.\n"
            ).encode(),
        )
        self.assertIn("owner: preserve me", corrected[new].decode())
        self.assertIn("%5BR-001%5D%20New%20Title.md", corrected[owner].decode())
        self.assertIn("    [example](%5BR-001%5D%20Old.md)\n", corrected[owner].decode())
        self.assertIn("[demo](%5BR-001%5D%20Old.md)\n", corrected[owner].decode())
        self.assertIn("../requirements/%5BR-001%5D%20New%20Title.md#scope", corrected[plan].decode())
        self.assertIn(f"  - {old}\n", corrected[plan].decode())
        self.assertIn("## User Decision\n\n[keep](../requirements/%5BR-001%5D%20Old.md)", corrected[plan].decode())
        self.assertNotIn(archived_plan, corrected)

    def test_duplicate_id_correction_updates_frontmatter_without_touching_other_fields(self):
        a = "docs/requirements/[R-008] A.md"
        b = "docs/requirements/[R-008] B.md"
        canonical_a = "---\nid: R-008\ntitle: A\nowner: preserve me\n---\n# [R-008] A\n\nBody.\n"
        canonical_b = "---\nid: R-008\ntitle: B\nowner: preserve me\n---\n# [R-008] B\n\nBody.\n"
        docs = {
            a: canonical_a,
            b: canonical_b,
        }

        result = requirement_corrections.plan_requirement_corrections(
            docs, references_complete=True, first_seen_order={a: 1, b: 2},
        )

        new = "docs/requirements/[R-009] B.md"
        self.assertEqual(result.corrections[0].target_path, new)
        text = dict(result.updated_documents)[new].decode()
        self.assertIn("id: R-009\ntitle: B\nowner: preserve me", text)
        self.assertIn("# [R-009] B", text)

    def test_duplicate_proposed_destinations_defer_all_without_overwriting_bodies(self):
        a = "docs/requirements/[R-020] A.md"
        b = "docs/requirements/[R-020] B.md"
        c = "docs/requirements/[R-020] C.md"
        documents = {
            a: frontmatter_requirement("R-020", "Common", "R-020"),
            b: frontmatter_requirement("R-020", "Common", "R-020"),
            c: requirement("R-020", "C"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            documents, referenced_sources=(a, b), references_complete=True,
            first_seen_order={a: 1, b: 2, c: 3},
        )

        self.assertEqual(
            [(item.source_path, item.target_path) for item in result.corrections],
            [(c, "docs/requirements/[R-021] C.md")],
        )
        self.assertNotIn("docs/requirements/[R-020] Common.md", dict(result.updated_documents))
        deferred = {item.path: item.reason for item in result.deferred}
        self.assertIn("목적지", deferred[a])
        self.assertIn("목적지", deferred[b])

    def test_source_is_revalidated_after_a_missing_id_veto(self):
        old = "docs/requirements/[R-001] Old.md"
        duplicate = "docs/requirements/[R-001] B.md"
        missing_source = "docs/requirements/[R-002] Missing.md"
        documents = {
            old: frontmatter_requirement("R-001", "New", "R-001"),
            duplicate: requirement("R-001", "B"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            documents, referenced_sources=(old, missing_source), references_complete=True,
            first_seen_order={old: 1, duplicate: 2},
        )

        self.assertEqual(result.corrections, ())
        self.assertEqual(result.updated_documents, ())
        deferred = {item.path: item.reason for item in result.deferred}
        self.assertIn("source", deferred[old])
        self.assertIn("source", deferred[duplicate])
        self.assertIn("미확인", deferred[missing_source])

    def test_unusable_title_does_not_consume_an_id_or_partially_edit_the_document(self):
        a = "docs/requirements/[R-010] A.md"
        b = "docs/requirements/[R-010] B.md"
        c = "docs/requirements/[R-010] C.md"
        docs = {
            a: requirement("R-010", "A"),
            b: requirement("R-010", "Bad/Title"),
            c: requirement("R-010", "C"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            docs, references_complete=True,
            first_seen_order={a: 1, b: 2, c: 3},
        )

        self.assertEqual([(item.source_path, item.new_id) for item in result.corrections], [(c, "R-011")])
        self.assertIn(b, {item.path for item in result.deferred})
        self.assertNotIn("R-011", dict(result.updated_documents).get(b, b"").decode())

    def test_existing_destination_is_never_overwritten(self):
        a = "docs/requirements/[R-001] A.md"
        b = "docs/requirements/[R-001] B.md"
        destination_owner = "docs/requirements/[r-002] b.md"
        docs = {
            a: requirement("R-001", "A"),
            b: requirement("R-001", "B"),
            destination_owner: requirement("R-001", "Other"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            docs, references_complete=True,
            first_seen_order={a: 1, b: 2, destination_owner: 3},
        )

        self.assertIn(b, {item.path for item in result.deferred})
        self.assertEqual(
            [(item.source_path, item.target_path) for item in result.corrections],
            [(destination_owner, "docs/requirements/[R-002] Other.md")],
        )
        updated = dict(result.updated_documents)
        self.assertNotIn(b, updated)
        self.assertNotIn("docs/requirements/[R-002] B.md", updated)

    def test_casefold_equivalent_destination_owned_by_another_file_is_deferred(self):
        source = "docs/requirements/[R-001] old.md"
        destination_owner = "docs/requirements/[R-001] Old.md"
        documents = {
            source: requirement("R-001", "Old"),
            destination_owner: frontmatter_requirement("R-002", "Bad/Title", "R-002"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            documents, references_complete=True,
        )

        self.assertEqual(result.corrections, ())
        self.assertEqual(result.updated_documents, ())
        deferred = {item.path: item.reason for item in result.deferred}
        self.assertIn("목적지", deferred[source])
        self.assertIn("파일명", deferred[destination_owner])

    def test_crlf_and_utf8_bom_are_preserved_during_identity_edit(self):
        old = "docs/requirements/[R-001] Old.md"
        contents = (
            "\ufeff---\r\n"
            "id: R-001\r\n"
            "title: New\r\n"
            "owner: preserve\r\n"
            "---\r\n"
            "# [R-009] Stale\r\n"
            "\r\nBody.\r\n"
        ).encode("utf-8")

        result = requirement_corrections.plan_requirement_corrections(
            {old: contents}, references_complete=True,
        )

        corrected = dict(result.updated_documents)["docs/requirements/[R-001] New.md"]
        self.assertEqual(
            corrected,
            (
                "\ufeff---\r\n"
                "id: R-001\r\n"
                "title: New\r\n"
                "owner: preserve\r\n"
                "---\r\n"
                "# [R-001] New\r\n"
                "\r\nBody.\r\n"
            ).encode("utf-8"),
        )

    def test_unpreservable_source_fallback_defers_a_title_rename(self):
        old = "docs/requirements/[R-012] Old.md"
        archived = "docs/requirements/archive/[R-012] Old.md"
        docs = {
            old: frontmatter_requirement("R-012", "New", "R-012"),
            archived: requirement("R-012", "Old"),
        }

        result = requirement_corrections.plan_requirement_corrections(
            docs, referenced_sources=(old,), references_complete=True,
        )

        self.assertEqual(result.corrections, ())
        self.assertTrue(any(old == item.path and "source" in item.reason for item in result.deferred))
        self.assertNotIn("docs/requirements/[R-012] New.md", dict(result.updated_documents))

    def test_missing_source_id_is_not_bound_to_a_new_document(self):
        a = "docs/requirements/[R-010] A.md"
        b = "docs/requirements/[R-010] B.md"
        docs = {a: requirement("R-010", "A"), b: requirement("R-010", "B")}
        missing_source = "docs/requirements/[R-011] Missing.md"

        result = requirement_corrections.plan_requirement_corrections(
            docs, referenced_sources=(missing_source,), references_complete=True,
            first_seen_order={a: 1, b: 2},
        )

        self.assertEqual(result.corrections, ())
        reasons = {item.path: item.reason for item in result.deferred}
        self.assertIn("미확인", reasons[missing_source])
        self.assertIn("source", reasons[b])


if __name__ == "__main__":
    unittest.main()
