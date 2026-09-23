from dataclasses import replace
import unittest

from execution_revision import (
    DefinitionDelta,
    DefinitionSnapshot,
    RevisionGateKind,
    StageArtifacts,
    classify_definition_delta,
    decide_revision_gate,
    execplan_body_hash,
)


def definition(**changes):
    values = {
        "revision_sha": "a" * 40,
        "path": "docs/plans/[G-037] Revision.md",
        "title": "Revision",
        "sources": ("docs/PRODUCT.md",),
        "depends_on": ("G-036",),
        "complexity": "high",
        "risk": "critical",
        "priority": "critical",
        "body_hash": "body",
        "source_fingerprint": "source",
        "archive_candidates": (),
    }
    values.update(changes)
    return DefinitionSnapshot(**values)


def complete_artifacts(current, base="base"):
    return StageArtifacts(
        worker_result=True,
        verify_result=True,
        review_result=True,
        worker_input_token=current.worker_token,
        archive_token=current.archive_token,
        verify_token=current.verify_token(base),
        review_token=current.review_token(base),
        publication_token=current.publication_token(base),
    )


class ExecutionRevisionTests(unittest.TestCase):
    def test_body_hash_excludes_frontmatter_metadata(self):
        first = "---\nid: G-037\ntitle: First\n---\n\n## Purpose\nSame\n"
        renamed = first.replace("title: First", "title: Renamed")
        changed = first.replace("Same", "Changed")

        self.assertEqual(execplan_body_hash(first), execplan_body_hash(renamed))
        self.assertNotEqual(execplan_body_hash(first), execplan_body_hash(changed))

    def test_invalidation_matrix_distinguishes_display_profile_and_worker_input(self):
        original = definition()
        cases = {
            "title": (replace(original, title="Renamed"), DefinitionDelta.TITLE, False),
            "priority": (replace(original, priority="low"), DefinitionDelta.PRIORITY, False),
            "profile": (replace(original, risk="high"), DefinitionDelta.PROFILE, False),
            "path": (replace(original, path="docs/plans/[G-037] New.md"), DefinitionDelta.PATH, True),
            "source": (replace(original, source_fingerprint="new"), DefinitionDelta.SOURCE_INPUT, True),
            "dependency": (replace(original, depends_on=("G-036", "G-049")), DefinitionDelta.DEPENDENCIES, True),
            "body": (replace(original, body_hash="new"), DefinitionDelta.BODY, True),
            "archive": (replace(original, archive_candidates=("docs/requirements/R.md",)), DefinitionDelta.ARCHIVE_SCOPE, True),
        }

        for name, (current, delta, invalidates_worker) in cases.items():
            with self.subTest(name=name):
                self.assertIn(delta, classify_definition_delta(original, current))
                decision = decide_revision_gate(
                    phase="PUBLISHING",
                    previous=original,
                    current=current,
                    active_base_token="base",
                    current_base_token="base",
                    artifacts=complete_artifacts(original),
                )
                if invalidates_worker:
                    self.assertEqual(decision.kind, RevisionGateKind.INVALIDATE_TO_PHASE)
                    self.assertEqual(decision.phase, "IMPLEMENTING")
                else:
                    self.assertEqual(decision.kind, RevisionGateKind.CONTINUE)
                    self.assertEqual(decision.phase, "PUBLISHING")

    def test_dependency_wait_preserves_identity_and_requests_earliest_phase(self):
        original = definition()
        current = replace(original, depends_on=("G-036", "G-049"))

        decision = decide_revision_gate(
            phase="REVIEWING",
            previous=original,
            current=current,
            active_base_token="old",
            current_base_token="new",
            artifacts=complete_artifacts(original, "old"),
            unmet_dependencies=("G-049", "G-036"),
            base_requires_validation=True,
        )

        self.assertEqual(decision.kind, RevisionGateKind.WAIT)
        self.assertEqual(decision.phase, "IMPLEMENTING")
        self.assertEqual(decision.unmet_dependencies, ("G-036", "G-049"))

    def test_missing_worker_result_cannot_skip_implementation(self):
        current = definition()
        for phase in ("VERIFYING", "REVIEWING", "PUBLISHING"):
            with self.subTest(phase=phase):
                decision = decide_revision_gate(
                    phase=phase,
                    previous=current,
                    current=current,
                    active_base_token="base",
                    current_base_token="base",
                    artifacts=StageArtifacts(),
                )
                self.assertEqual(
                    decision.kind, RevisionGateKind.INVALIDATE_TO_PHASE
                )
                self.assertEqual(decision.phase, "IMPLEMENTING")

    def test_title_only_rebase_carries_validation_but_code_change_reverifies(self):
        original = definition()
        renamed = replace(original, revision_sha="b" * 40, title="Renamed")
        artifacts = complete_artifacts(original, "old")

        harmless = decide_revision_gate(
            phase="PUBLISHING",
            previous=original,
            current=renamed,
            active_base_token="old",
            current_base_token="new",
            artifacts=artifacts,
            base_requires_validation=False,
        )
        code_change = decide_revision_gate(
            phase="PUBLISHING",
            previous=original,
            current=renamed,
            active_base_token="old",
            current_base_token="new",
            artifacts=artifacts,
            base_requires_validation=True,
        )

        self.assertEqual(harmless.kind, RevisionGateKind.REFRESH_WORKTREE)
        self.assertTrue(harmless.preserve_validation)
        self.assertEqual(code_change.kind, RevisionGateKind.INVALIDATE_TO_PHASE)
        self.assertEqual(code_change.phase, "VERIFYING")

    def test_serialization_is_deterministic_and_rejects_invalid_state(self):
        current = definition(archive_candidates=("b", "a"))
        restored = DefinitionSnapshot.deserialize(current.serialize())
        self.assertEqual(restored, current)
        self.assertEqual(restored.definition_token, current.definition_token)
        with self.assertRaisesRegex(ValueError, "definition snapshot"):
            DefinitionSnapshot.deserialize("{}")


if __name__ == "__main__":
    unittest.main()
