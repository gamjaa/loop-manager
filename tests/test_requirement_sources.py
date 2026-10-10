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
