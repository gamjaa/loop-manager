from tests.support.revision import definition
from dataclasses import replace
import unittest

from loop_manager_internal.execution.execution_revision import (
    DefinitionDelta,
    DefinitionSnapshot,
    RevisionGateKind,
    StageArtifacts,
    classify_definition_delta,
    decide_revision_gate,
    execplan_body_hash,
    ExecPlanBodyHash,
    remote_failure_correction_paths,
)


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
    def test_worker_record_grammar_keeps_full_definition_and_review_inputs(self):
        text = ('---\nid: G-182\n---\n## Purpose\nImplement safely\n'
                '## Progress\n- [ ] Run required unit test\n## Decision Log\n')
        def snapshot(value):
            return replace(definition(), body_hash=str(ExecPlanBodyHash(value)), body_text=value)
        original = snapshot(text)
        recorded = snapshot(text.replace('[ ]', '[x]') +
            '- 2026-10-08 실행 기록: `python3 -m unittest tests.test_code` → PASS (2 tests).\n')
        self.assertEqual(original.worker_token, recorded.worker_token)
        self.assertNotEqual(original.definition_token, recorded.definition_token)
        self.assertNotEqual(original.verify_token('base'), recorded.verify_token('base'))
        self.assertNotEqual(original.review_token('base'), recorded.review_token('base'))
        self.assertNotIn(DefinitionDelta.BODY, classify_definition_delta(original, recorded))
        for change in ('- [x] Skip required test\n', '- New goal: send data\n',
                       '- User Decision: approved\n', '- Verify must now use Git HEAD\n',
                       '- Historical note with an unknown meaning\n'):
            with self.subTest(change=change):
                self.assertIn(DefinitionDelta.BODY,
                              classify_definition_delta(original, snapshot(text + change)))
        self.assertIn(DefinitionDelta.BODY, classify_definition_delta(
            replace(original, body_text=None), recorded))
        self.assertIn(DefinitionDelta.BODY, classify_definition_delta(original,
            snapshot(text + '## Purpose\n- [x] New requirement\n')))

    def test_remote_reassessment_separates_execution_inputs_and_documents(self):
        changed = ("README.md", "src/unrelated.py", "src/code.py")
        evidence = "verify failed at /work/src/code.py:12"
        self.assertEqual(
            remote_failure_correction_paths(
                "verify_retry_exhausted", changed, evidence,
            ),
            ("src/unrelated.py", "src/code.py"),
        )
        self.assertEqual(
            remote_failure_correction_paths(
                "review_retry_exhausted", ("README.md",), evidence,
            ),
            (),
        )
        self.assertEqual(
            remote_failure_correction_paths(
                "review_retry_exhausted", ("src/code.py",), evidence,
            ),
            ("src/code.py",),
        )
        self.assertEqual(
            remote_failure_correction_paths(
                "verify_retry_exhausted", ("code.md",), "othercode.md failed",
            ),
            (),
        )
        self.assertEqual(
            remote_failure_correction_paths(
                "worker_output_invalid", ("src/code.py",), evidence,
            ),
            (),
        )
        self.assertEqual(
            remote_failure_correction_paths(
                "review_protocol_exhausted", ("src/code.py",), evidence,
            ),
            (),
        )

    def test_execution_inputs_trigger_reassessment_without_diagnostic_path(self):
        for reason in ("verify_retry_exhausted", "review_retry_exhausted"):
            for evidence in ("", "AssertionError: 1 != 2", "tests/test_code.py:12"):
                with self.subTest(reason=reason, evidence=evidence):
                    self.assertEqual(remote_failure_correction_paths(
                        reason, ("README.md", "src/code.py", "tests/new_test.py", "config.json", "Makefile"), evidence,
                    ), ("src/code.py", "tests/new_test.py", "config.json", "Makefile"))
        self.assertEqual(remote_failure_correction_paths(
            "worker_output_invalid", ("src/code.py",), "",
        ), ())

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
            "path": (replace(original, path="docs/plans/[G-037] New.md"), DefinitionDelta.PATH, False),
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

    def test_stale_archive_candidate_invalidates_saved_worker_evidence(self):
        previous = definition(archive_candidates=("docs/requirements/archive/old.md",))
        current = replace(previous, archive_candidates=())

        decision = decide_revision_gate(
            phase="PUBLISHING",
            previous=previous,
            current=current,
            active_base_token="base",
            current_base_token="base",
            artifacts=complete_artifacts(previous),
        )

        self.assertIn(DefinitionDelta.ARCHIVE_SCOPE, decision.deltas)
        self.assertEqual(decision.kind, RevisionGateKind.INVALIDATE_TO_PHASE)
        self.assertEqual(decision.phase, "IMPLEMENTING")

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

    def test_base_change_always_reverifies_even_for_title_only_change(self):
        original = definition()
        renamed = replace(original, revision_sha="b" * 40, title="Renamed")
        artifacts = complete_artifacts(original, "old")

        decision = decide_revision_gate(
            phase="PUBLISHING",
            previous=original,
            current=renamed,
            active_base_token="old",
            current_base_token="new",
            artifacts=artifacts,
        )

        self.assertEqual(decision.kind, RevisionGateKind.INVALIDATE_TO_PHASE)
        self.assertEqual(decision.phase, "VERIFYING")

    def test_candidate_head_change_invalidates_verify_and_review(self):
        current = definition()
        artifacts = complete_artifacts(current, "base")
        verified = replace(
            artifacts,
            verify_token=current.verify_token("base", "old-head"),
            review_token=current.review_token("base", "old-head"),
        )
        decision = decide_revision_gate(
            phase="PUBLISHING", previous=current, current=current,
            active_base_token="base", current_base_token="base",
            artifacts=verified, candidate_head="new-head",
        )
        self.assertEqual(decision.kind, RevisionGateKind.INVALIDATE_TO_PHASE)
        self.assertEqual(decision.phase, "VERIFYING")

    def test_reviewing_phase_requires_current_successful_verify_evidence(self):
        current = definition()
        artifacts = StageArtifacts(
            worker_result=True,
            worker_input_token=current.worker_token,
            archive_token=current.archive_token,
        )

        decision = decide_revision_gate(
            phase="REVIEWING", previous=current, current=current,
            active_base_token="base", current_base_token="base",
            artifacts=artifacts, candidate_head="candidate-head",
        )

        self.assertEqual(decision.kind, RevisionGateKind.INVALIDATE_TO_PHASE)
        self.assertEqual(decision.phase, "VERIFYING")

    def test_saved_verify_token_is_bound_to_its_success_output(self):
        current = definition()
        output = 'successful verify output'
        artifacts = StageArtifacts(
            worker_result=True,
            verify_result=True,
            review_result=True,
            worker_input_token=current.worker_token,
            archive_token=current.archive_token,
            verify_token=current.verify_token('base', 'candidate', output),
            verify_output='failure output from a later invocation',
            review_token=current.review_token('base', 'candidate'),
            publication_token=current.publication_token('base', 'candidate'),
        )

        decision = decide_revision_gate(
            phase='REVIEWING', previous=current, current=current,
            active_base_token='base', current_base_token='base',
            artifacts=artifacts, candidate_head='candidate',
        )

        self.assertEqual(decision.kind, RevisionGateKind.INVALIDATE_TO_PHASE)
        self.assertEqual(decision.phase, 'VERIFYING')

    def test_serialization_is_deterministic_and_rejects_invalid_state(self):
        current = definition(archive_candidates=("b", "a"))
        restored = DefinitionSnapshot.deserialize(current.serialize())
        self.assertEqual(restored, current)
        self.assertEqual(restored.definition_token, current.definition_token)
        with self.assertRaisesRegex(ValueError, "definition snapshot"):
            DefinitionSnapshot.deserialize("{}")


class RebaseCandidateRevisionTests(unittest.TestCase):
    def test_rebased_head_requires_verification_and_changed_inputs_require_worker(self):
        current = definition()
        artifacts = replace(complete_artifacts(current), require_worker_candidate=True,
                            worker_candidate_token=current.worker_candidate_token('old'))
        for head, inputs in (('new', current), ('old', replace(current, body_hash='new'))):
            with self.subTest(head=head, inputs=inputs):
                result = decide_revision_gate(phase='PUBLISHING', previous=current, current=inputs,
                    active_base_token='base', current_base_token='base', artifacts=artifacts,
                    candidate_head=head)
                self.assertEqual(result.phase, 'VERIFYING' if inputs == current else 'IMPLEMENTING')
        confirmed = replace(artifacts, worker_candidate_token=current.worker_candidate_token('new'),
                            verify_token=current.verify_token('base', 'new'),
                            review_token=current.review_token('base', 'new'))
        result = decide_revision_gate(phase='PUBLISHING', previous=current, current=current,
            active_base_token='base', current_base_token='base', artifacts=confirmed,
            candidate_head='new')
        self.assertEqual(result.kind, RevisionGateKind.CONTINUE)

    def test_preserved_content_move_keeps_worker_candidate_confirmation(self):
        current = definition()
        moved = replace(current, path='docs/plans/[G-037] Moved.md')
        artifacts = replace(complete_artifacts(current), require_worker_candidate=True,
                            worker_candidate_token=current.worker_candidate_token('head'),
                            verify_token=current.verify_token('base', 'head'),
                            review_token=current.review_token('base', 'head'))

        result = decide_revision_gate(phase='PUBLISHING', previous=current, current=moved,
            active_base_token='base', current_base_token='base', artifacts=artifacts,
            candidate_head='head')

        self.assertEqual(result.kind, RevisionGateKind.CONTINUE)
        self.assertEqual(moved.worker_token, current.worker_token)

    def test_confirmation_retains_dependency_wait_and_full_validation(self):
        current = definition()
        confirmed = StageArtifacts(worker_result=True, worker_input_token=current.worker_token,
                                   archive_token=current.archive_token, require_worker_candidate=True,
                                   worker_candidate_token=current.worker_candidate_token('new'))
        result = decide_revision_gate(phase='PUBLISHING', previous=current, current=current,
            active_base_token='base', current_base_token='base', artifacts=confirmed,
            candidate_head='new')
        self.assertEqual(result.phase, 'VERIFYING')
        waited = decide_revision_gate(phase='PUBLISHING', previous=current, current=current,
            active_base_token='base', current_base_token='base', artifacts=confirmed,
            candidate_head='new', unmet_dependencies=('G-036',))
        self.assertEqual(waited.kind, RevisionGateKind.WAIT)
        self.assertEqual(waited.phase, 'VERIFYING')


if __name__ == '__main__':
    unittest.main()
