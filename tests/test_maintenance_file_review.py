from tests.support.maintenance import root_results_for, result_for
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.maintenance import maintenance_file_review as review
from loop_manager_internal.storage import maintenance_file_ledger


class FileReviewPromptTest(unittest.TestCase):
    def test_quality_guidance_preserves_single_file_review_contract(self):
        for content in (b"", b"def calculate(): return 1\n",
                        b"x" * (review.MAX_EMBEDDED_SOURCE_BYTES + 1)):
            with self.subTest(size=len(content)):
                source = maintenance_file_inventory.TrackedSource(
                    "src/calc.py", hashlib.sha256(content).hexdigest(), "100644",
                    "a" * 40, "source",
                )
                prompt = review.review_prompt(source, content)
                for phrase in (
                    "기존 구현·공유 helper·관련 테스트와 표준 도구",
                    "현재 목표에 필요하지 않은 추상화·설정·파일 추가",
                    "오류 처리·보안·접근성·회귀 검증",
                    "결정·예외·실패 흐름과 Acceptance Criteria의 의미",
                    "파일이나 문서를 수정하지 마라",
                    "길이만으로 개선을 강제하지 말고",
                    "FILE_REVIEW_RESULT:",
                    "chunk_reviews:",
                    "revision-bound root placement inventory:",
                    "실제 참조·사용자 경로·소유권·안전한 목적지",
                ):
                    self.assertIn(phrase, prompt)
                self.assertIn(source.content_hash, prompt)
                self.assertIn(source.revision, prompt)
                self.assertIn(source.path, prompt)
                if len(content) > review.MAX_EMBEDDED_SOURCE_BYTES:
                    self.assertIn("작은 연속 구간으로 끝까지 읽어라", prompt)
                else:
                    self.assertIn("원본 시작\n" + content.decode() + "\n원본 끝", prompt)


class ProvisionalFileReviewTest(unittest.TestCase):
    def test_unchanged_source_review_survives_remote_revision_advance(self):
        first = self.publish({"src/a.py": "def calculate(): return 1\n"})
        source = first.sources[0]
        self.finish(source)
        advanced = self.publish({"docs/OTHER.md": "Unrelated change.\n"})

        resumed = self.state(advanced, cycle=2)

        self.assertEqual(len(resumed.completed), 1)
        self.assertEqual(resumed.completed[0]["path"], "src/a.py")
        self.assertEqual(resumed.completed[0]["source_revision"], advanced.revision)
        changed = self.publish({"src/a.py": "def calculate(): return 2\n"})
        self.assertEqual(self.state(changed, cycle=3).completed, ())

    def test_structured_reference_candidates_are_normalized_and_source_checked(self):
        content = b"def calculate(): return helper()\n"
        source = maintenance_file_inventory.TrackedSource(
            "src/calc.py", hashlib.sha256(content).hexdigest(), "100644",
            "a" * 40, "source",
        )
        payload = result_for(source, "def calculate(): return helper()", content)
        payload["code_map"]["direct_references"] = [
            {"target_path": "src/helper.py", "symbol": "helper", "evidence": "helper()"},
        ]
        payload["pending_references"] = [
            {"target_path": "src/helper.py", "symbol": "helper", "evidence": "helper()"},
        ]
        payload["similar_responsibilities"] = [
            {"target_path": "src/other.py", "responsibility": "calculation",
             "reason": "Both modules calculate a returned value."},
        ]
        output = review.RESULT_PREFIX + json.dumps(payload)
        parsed = review.parse_review(output, source, content)
        self.assertEqual(parsed["pending_references"][0]["target_path"], "src/helper.py")
        self.assertEqual(parsed["similar_responsibilities"][0]["target_path"], "src/other.py")
        payload["pending_references"][0]["evidence"] = "imaginary()"
        with self.assertRaisesRegex(review.FileReviewError, "원본 인용"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)

    def test_large_source_prompt_uses_revision_bound_chunked_read(self):
        content = b"def calculate(): return 1\n" * 3000
        source = maintenance_file_inventory.TrackedSource(
            "src/calc.py", hashlib.sha256(content).hexdigest(), "100644",
            "a" * 40, "source",
        )
        prompt = review.review_prompt(source, content)
        self.assertIn(source.revision + ":" + source.path, prompt)
        self.assertIn("작은 연속 구간", prompt)
        self.assertNotIn("원본 시작", prompt)
        payload = result_for(source, "def calculate(): return 1", content)
        self.assertEqual(len(payload["evidence"]["chunk_reviews"]), 2)
        review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)
        payload["evidence"]["chunk_reviews"].pop()
        with self.assertRaisesRegex(review.FileReviewError, "모든 검토 구간"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)
        payload = result_for(source, "def calculate(): return 1", content)
        payload["evidence"]["chunk_reviews"][1]["middle_excerpt"] = "not in source"
        with self.assertRaisesRegex(review.FileReviewError, "중간"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)

    def test_many_chunk_result_over_old_limit_is_saved_and_resumed(self):
        line = b"def calculate(): return 1\n"
        content = line * (review.MAX_EMBEDDED_SOURCE_BYTES * 160 // len(line))
        inventory = self.publish({"src/calc.py": content})
        source = inventory.sources[0]
        payload = result_for(source, "def calculate(): return 1", content)
        self.assertEqual(len(payload["evidence"]["chunk_reviews"]), 160)
        raw = json.dumps(payload)
        self.assertGreater(len(raw), 48_000)
        parsed = review.parse_review(review.RESULT_PREFIX + raw, source, content)
        self.progress.start(str(self.repo), 1, source)
        self.progress.complete(str(self.repo), 1, source, parsed)

        resumed = review.ProvisionalReviewRepository(self.connection).progress(
            str(self.repo), self.repo, "main", 1, inventory,
        )
        self.assertEqual(resumed.completed, (parsed,))
        self.assertEqual(resumed.next_unit.source.reason, maintenance_file_ledger.ROOT_REVIEW_REASON)
        self.assertEqual(self.ledger.reviews(str(self.repo)), ())
        self.assertIsNone(self.ledger.last_success(str(self.repo)))

        # A large saved result must still cover every source span on resume.
        payload["evidence"]["chunk_reviews"].pop(80)
        self.connection.execute(
            "UPDATE provisional_file_reviews SET result_json = ? WHERE repo = ?",
            (json.dumps(payload), str(self.repo)),
        )
        invalid = self.state(inventory)
        self.assertEqual(invalid.completed, ())
        self.assertEqual(invalid.next_unit.source, source)

    def test_result_without_total_cap_still_enforces_field_and_list_bounds(self):
        content = b"def calculate(): return 1\n"
        source = maintenance_file_inventory.TrackedSource(
            "src/calc.py", hashlib.sha256(content).hexdigest(), "100644",
            "a" * 40, "source",
        )
        for field in ("readability", "chunk_review", "public_symbols"):
            with self.subTest(field=field):
                payload = result_for(source, "def calculate(): return 1", content)
                if field == "readability":
                    payload["evidence"][field] = "x" * (review.MAX_FIELD_CHARS + 1)
                elif field == "chunk_review":
                    payload["evidence"]["chunk_reviews"][0]["review"] = (
                        "x" * (review.MAX_FIELD_CHARS + 1)
                    )
                else:
                    payload["code_map"][field] = ["calculate"] * (review.MAX_LIST_ITEMS + 1)
                with self.assertRaises(review.FileReviewError):
                    review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)

    def test_empty_and_whitespace_sources_are_reviewed_and_resumed(self):
        for content in (b"", b" \n\t"):
            with self.subTest(content=content):
                inventory = self.publish({"src/__init__.py": content})
                selected = self.state(inventory)
                source = selected.next_unit.source
                self.assertEqual(source.path, "src/__init__.py")
                self.assertEqual(review.source_bytes(self.repo, "main", source), content)
                prompt = review.review_prompt(source, content)
                self.assertIn('"bytes": ' + str(len(content)), prompt)
                self.assertIn("excerpts는 []", prompt)
                self.progress.start(str(self.repo), 1, source)
                self.progress.session(str(self.repo), 1, source, "empty-review")
                self.assertEqual(self.state(inventory).next_unit.session_id, "empty-review")
                payload = result_for(source, "", content)
                payload["evidence"]["excerpts"] = []
                for key in payload["evidence"]:
                    if key not in {"excerpts", "chunk_reviews"}:
                        payload["evidence"][key] = "No executable code exists in this package marker."
                payload["code_map"] = {
                    "responsibility": "Package marker without executable statements.",
                    "entry_points": [], "public_symbols": [],
                    "direct_references": [], "related_tests": [],
                }
                parsed = review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)
                self.progress.complete(str(self.repo), 1, source, parsed)
                resumed = review.ProvisionalReviewRepository(self.connection).progress(
                    str(self.repo), self.repo, "main", 1, inventory,
                )
                self.assertEqual(resumed.completed, (parsed,))
                self.assertEqual(resumed.next_unit.source.reason, maintenance_file_ledger.ROOT_REVIEW_REASON)
                self.assertEqual(self.ledger.reviews(str(self.repo)), ())
                self.assertIsNone(self.ledger.last_success(str(self.repo)))
                for field in ("excerpts", "public_symbols", "readability"):
                    with self.subTest(forged=field):
                        forged = json.loads(json.dumps(payload))
                        if field == "excerpts":
                            forged["evidence"][field] = ["def imaginary(): pass"]
                        elif field == "public_symbols":
                            forged["code_map"][field] = ["imaginary"]
                        else:
                            forged["evidence"][field] = ""
                        with self.assertRaises(review.FileReviewError):
                            review.parse_review(review.RESULT_PREFIX + json.dumps(forged), source, content)

    def test_non_utf8_source_has_byte_preserving_prompt_and_review(self):
        content = b"def calculate(): return '\xff'\n"
        inventory = self.publish({"src/calc.py": content})
        source = inventory.sources[0]
        self.assertEqual(review.source_bytes(self.repo, "main", source), content)
        prompt = review.review_prompt(source, content)
        self.assertIn("\\xff", prompt)
        payload = result_for(source, "def calculate(): return '\\xff'", content)
        parsed = review.parse_review(
            review.RESULT_PREFIX + json.dumps(payload), source, content,
        )
        self.progress.start(str(self.repo), 1, source)
        self.progress.complete(str(self.repo), 1, source, parsed)
        self.assertEqual(self.state(inventory).completed, (parsed,))

    def test_incomplete_large_result_cannot_resume_as_completed(self):
        content = b"def calculate(): return 1\n" * 3000
        inventory = self.publish({"src/calc.py": content})
        source = inventory.sources[0]
        payload = result_for(source, "def calculate(): return 1", content)
        payload["evidence"]["chunk_reviews"].pop()
        self.progress.start(str(self.repo), 1, source)
        self.progress.complete(str(self.repo), 1, source, payload)

        resumed = self.state(inventory)

        self.assertEqual(resumed.completed, ())
        self.assertEqual(resumed.next_unit.source, source)
        self.assertIsNone(self.ledger.last_success(str(self.repo)))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(
            "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT)"
        )
        self.connection.execute(
            "INSERT INTO projects VALUES (?, 'main')", (str(self.repo),)
        )
        self.progress = review.ProvisionalReviewRepository(self.connection)
        self.ledger = maintenance_file_ledger.FileReviewRepository(self.connection)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def publish(self, files):
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "source")
        revision = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", revision)
        return maintenance_file_inventory.tracked_source_inventory(
            self.repo, "main", revision,
        )

    def state(self, inventory, cycle=1):
        return self.progress.progress(
            str(self.repo), self.repo, "main", cycle, inventory,
        )

    def finish(self, source):
        content = review.source_bytes(self.repo, "main", source)
        payload = result_for(source, content.decode().strip(), content)
        parsed = review.parse_review(
            review.RESULT_PREFIX + json.dumps(payload), source, content,
        )
        self.progress.start(str(self.repo), 1, source)
        self.progress.complete(str(self.repo), 1, source, parsed)
        return parsed

    def finish_root(self, inventory, cycle=1):
        source = maintenance_file_ledger.root_review_source(self.repo, inventory)
        result = {"path": source.path, "content_hash": source.content_hash,
                  "mode": source.mode, "source_revision": source.revision,
                  "root_reviews": list(root_results_for(self.repo, inventory))}
        content = review.source_bytes(self.repo, "main", source)
        parsed = review.parse_review(review.RESULT_PREFIX + json.dumps(result), source, content)
        self.progress.start(str(self.repo), cycle, source)
        self.progress.complete(str(self.repo), cycle, source, parsed)
        return parsed

    def test_asset_only_tree_requires_investigation_and_resumes_root_unit(self):
        from loop_manager_internal.maintenance import maintenance_file_publication as publication
        import loop_manager
        from unittest import mock
        inventory = self.publish({"scratch.png": b"\x89PNG\x00fixture"})
        self.assertEqual(inventory.sources, ())
        port = loop_manager._PlanningFileReviewPorts(self.connection)
        self.assertIsNone(port.empty_selection_inventory(str(self.repo), self.repo, "main", inventory.revision))
        with self.assertRaises(publication.FileAssessmentPublicationError):
            publication.collect(self.connection, str(self.repo), self.repo, "main", 1, inventory.revision)
        progress = self.state(inventory)
        source = progress.next_unit.source
        prompt = review.review_prompt(source, review.source_bytes(self.repo, "main", source),
                                      root_placements=progress.root_placements)
        self.assertIn("root_reviews", prompt)
        self.assertIn("scratch.png", prompt)
        self.progress.start(str(self.repo), 1, source)
        self.progress.session(str(self.repo), 1, source, "root-session")
        self.assertEqual(self.state(inventory).next_unit.session_id, "root-session")
        result = {"path": source.path, "content_hash": source.content_hash,
                  "mode": source.mode, "source_revision": source.revision, "root_reviews": []}
        with self.assertRaises(review.FileReviewError):
            self.progress.complete(str(self.repo), 1, source, result)
        self.progress.fail(str(self.repo), 1, source, "missing investigation")
        self.assertEqual(self.state(inventory).next_unit.error, "missing investigation")
        self.finish_root(inventory)
        restarted = review.ProvisionalReviewRepository(self.connection).progress(
            str(self.repo), self.repo, "main", 1, inventory)
        self.assertIsNone(restarted.next_unit)
        self.assertEqual(restarted.root_reviews[0]["category"], "uncertain")
        # Root records never become source file reviews or code maps.
        with mock.patch.object(publication, "_merged_count", return_value=0):
            evidence = publication.collect(self.connection, str(self.repo), self.repo, "main", 1, inventory.revision)
        self.assertEqual(evidence.reviewed, ())
        self.assertEqual(evidence.code_maps, ())
        publication.promote(self.connection, str(self.repo), self.repo, "main", inventory.revision, evidence)
        self.assertEqual(self.ledger.root_placements(str(self.repo))[0].category, "uncertain")
        self.assertEqual(port.empty_selection_inventory(str(self.repo), self.repo, "main", inventory.revision), inventory)
        self.assertIsNone(self.state(inventory, cycle=2).next_unit)
        changed = self.publish({"scratch.png": b"\x89PNG\x00changed"})
        self.assertIsNotNone(self.state(changed, cycle=2).next_unit)
        self.assertIsNone(port.empty_selection_inventory(str(self.repo), self.repo, "main", changed.revision))

    def test_root_only_move_requires_synthesis_and_cannot_use_empty_stabilization(self):
        import loop_manager
        from loop_manager_internal.maintenance import maintenance_file_publication as publication
        inventory = self.publish({"notes.md": "Internal development notes owned by the documentation team.\n"})
        source = maintenance_file_ledger.root_review_source(self.repo, inventory)
        roots = list(root_results_for(self.repo, inventory))
        roots[0].update(category="internal_implementation", destination="docs/notes.md",
                        references=[{"path": "notes.md", "excerpt": "Internal development notes owned by the documentation team."}],
                        ownership="The documentation team owns these internal notes.",
                        reason="Internal notes have no user entry path and belong in docs/notes.md.")
        self.progress.start(str(self.repo), 1, source)
        self.progress.complete(str(self.repo), 1, source, {"path": source.path, "mode": source.mode,
            "content_hash": source.content_hash, "source_revision": source.revision, "root_reviews": roots})
        self.assertEqual(self.state(inventory).selected, ())
        self.assertIsNone(self.state(inventory).next_unit)
        with self.assertRaisesRegex(publication.FileAssessmentPublicationError, "종합"):
            publication.collect(self.connection, str(self.repo), self.repo, "main", 1, inventory.revision)
        port = loop_manager._PlanningFileReviewPorts(self.connection)
        self.assertIsNone(port.empty_selection_inventory(str(self.repo), self.repo, "main", inventory.revision))
        self.ledger.record_success(str(self.repo), str(self.repo), "main", inventory, (), root_reviews=roots)
        self.assertIsNone(port.empty_selection_inventory(str(self.repo), self.repo, "main", inventory.revision))

    def test_legacy_automatic_root_result_cannot_replace_investigation(self):
        from unittest import mock
        inventory = self.publish({"scratch.png": b"\x89PNG\x00fixture"})
        self.connection.execute(
            "INSERT INTO root_placement_reviews VALUES (?, 'scratch.png', 'execution_build_deploy', ?, ?, 'old', 1)",
            (str(self.repo), "root manifest, configuration, asset, or build contract", inventory.revision),
        )
        self.assertIsNone(self.ledger.reusable_root_reviews(str(self.repo), inventory))
        self.assertEqual(self.state(inventory).next_unit.source.reason, maintenance_file_ledger.ROOT_REVIEW_REASON)
        self.finish_root(inventory)
        self.assertIsNone(self.state(inventory).next_unit)
        with mock.patch.object(review, "validate_root_reviews") as validate:
            self.assertIsNone(self.state(inventory).next_unit)
            validate.assert_not_called()
        raw = self.connection.execute(
            "SELECT result_json FROM provisional_file_reviews WHERE repo = ? AND path = '.'", (str(self.repo),),
        ).fetchone()[0]
        forged = json.loads(raw)
        forged["root_reviews"][0]["ownership"] = ""
        self.connection.execute(
            "UPDATE provisional_file_reviews SET result_json = ? WHERE repo = ? AND path = '.'",
            (json.dumps(forged), str(self.repo)),
        )
        self.assertIsNotNone(self.state(inventory).next_unit)
        # Invalid saved results must allow replacement, rather than trapping retries.
        self.finish_root(inventory)
        self.assertIsNone(self.state(inventory).next_unit)

    def test_multiple_units_resume_without_advancing_success_ledger(self):
        inventory = self.publish({
            "src/a.py": "def calculate(): return 1\n",
            "src/b.py": "def calculate(): return 2\n",
        })
        first = self.state(inventory)
        self.assertEqual(len(first.selected), 2)
        self.assertEqual(first.next_unit.source.path, "src/a.py")
        self.progress.start(str(self.repo), 1, first.next_unit.source)
        self.progress.session(str(self.repo), 1, first.next_unit.source, "resume-a")
        in_flight = self.state(inventory)
        self.assertEqual(in_flight.next_unit.session_id, "resume-a")
        a = self.finish(first.next_unit.source)
        resumed = review.ProvisionalReviewRepository(self.connection).progress(
            str(self.repo), self.repo, "main", 1, inventory,
        )
        self.assertEqual(resumed.completed, (a,))
        self.assertEqual(resumed.completed[0]["code_map"]["public_symbols"], ["calculate"])
        self.assertEqual(resumed.completed[0]["code_map"]["related_tests"],
                         ["tests/test_calc.py"])
        self.assertEqual(resumed.next_unit.source.path, "src/b.py")
        self.finish(resumed.next_unit.source)
        final = self.state(inventory)
        self.assertEqual(final.next_unit.source.reason, maintenance_file_ledger.ROOT_REVIEW_REASON)
        self.finish_root(inventory)
        self.assertIsNone(self.state(inventory).next_unit)
        self.assertEqual(len(final.completed), 2)
        self.assertEqual(self.ledger.reviews(str(self.repo)), ())
        self.assertIsNone(self.ledger.last_success(str(self.repo)))

    def test_rejects_forged_evidence_empty_blob_and_duplicate_result(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n"})
        source = inventory.sources[0]
        content = review.source_bytes(self.repo, "main", source)
        payload = result_for(source, "not in source", content)
        with self.assertRaisesRegex(review.FileReviewError, "인용"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)
        payload["evidence"]["excerpts"] = ["def calculate(): return 1"]
        line = review.RESULT_PREFIX + json.dumps(payload)
        with self.assertRaisesRegex(review.FileReviewError, "정확히 하나"):
            review.parse_review(line + "\n" + line, source, content)
        with self.assertRaisesRegex(review.FileReviewError, "빈 원본"):
            review.parse_review(line, source, b"")
        payload["mode"] = "100755"
        with self.assertRaisesRegex(review.FileReviewError, "identity"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)

    def test_source_drift_invalidates_units_but_cycle_revision_reuses_unchanged_source(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n"})
        source = inventory.sources[0]
        self.finish(source)
        self.assertEqual(len(self.state(inventory).completed), 1)
        resumed = self.state(inventory, cycle=2)
        self.assertEqual(len(resumed.completed), 1)
        self.assertEqual(resumed.next_unit.source.reason,
                         maintenance_file_ledger.ROOT_REVIEW_REASON)
        changed = self.publish({"src/a.py": "def calculate(): return 2\n"})
        with self.assertRaises(maintenance_file_inventory.SourceInventoryError):
            self.state(inventory)
        fresh = self.state(changed)
        self.assertEqual(fresh.completed, ())
        self.assertEqual(fresh.next_unit.source.content_hash,
                         hashlib.sha256(b"def calculate(): return 2\n").hexdigest())

    def test_corrupt_saved_result_and_mode_change_require_new_review(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n"})
        source = inventory.sources[0]
        self.finish(source)
        self.connection.execute(
            "UPDATE provisional_file_reviews SET result_json = ? WHERE repo = ?",
            ('{"forged":true}', str(self.repo)),
        )
        self.assertEqual(self.state(inventory).completed, ())
        self.finish(source)
        (self.repo / source.path).chmod(0o755)
        changed = self.publish({})
        current = self.state(changed)
        self.assertEqual(current.completed, ())
        self.assertEqual(current.next_unit.source.mode, "100755")
        (self.repo / source.path).rename(self.repo / "src/renamed.py")
        renamed = self.publish({})
        moved = self.state(renamed)
        self.assertEqual(moved.completed, ())
        self.assertEqual(moved.next_unit.source.path, "src/renamed.py")

    def test_context_file_is_not_selected_or_code_mapped(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n",
                                  "src/b.py": "def calculate(): return 2\n"})
        selected = inventory.sources[0]
        context = inventory.sources[1]
        self.ledger.record_success(
            str(self.repo), str(self.repo), "main", inventory,
            tuple(maintenance_file_ledger.ReviewedSource(
                item.path, item.content_hash, item.mode, item.revision,
                "Reviewed implementation and test responsibilities.",
            ) for item in inventory.sources),
            root_reviews=root_results_for(self.repo, inventory),
        )
        changed = self.publish({"src/a.py": "def calculate(): return 3\n"})
        state = self.state(changed)
        self.assertEqual(tuple(item.path for item in state.selected), (selected.path,))
        current_context = next(item for item in changed.sources if item.path == context.path)
        self.assertEqual(review.source_bytes(self.repo, "main", current_context),
                         b"def calculate(): return 2\n")
        self.assertEqual(self.progress.completed_summary(str(self.repo)),
                         {"preview": [], "more_than_preview": False})


class ReviewReceiptUnitTest(unittest.TestCase):
    def test_validated_results_survive_restart_without_source_reread(self):
        from unittest import mock
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('repo')")
        content = b"def calculate(): return 1\n"
        source = maintenance_file_inventory.TrackedSource("src/a.py", hashlib.sha256(content).hexdigest(),
                                                         "100644", "a" * 40, "source")
        inventory = maintenance_file_inventory.SourceInventory(source.revision, (source,), ())
        repository = review.ProvisionalReviewRepository(connection)
        repository.start("repo", 1, source)
        result = result_for(source, "def calculate(): return 1", content)
        repository.complete("repo", 1, source, result)
        with (mock.patch.object(review, "cached_source_inventory", return_value=inventory),
              mock.patch.object(review.FileReviewRepository, "selected_sources", return_value=(source,)),
              mock.patch.object(review, "source_bytes", return_value=content) as read):
            self.assertEqual(len(repository.progress("repo", Path("repo"), "main", 1, inventory).completed), 1)
            repository = review.ProvisionalReviewRepository(connection)
            self.assertEqual(len(repository.progress("repo", Path("repo"), "main", 1, inventory).completed), 1)
            read.assert_called_once()
            result["evidence"]["excerpts"] = ["invented code that is absent"]
            connection.execute("UPDATE provisional_file_reviews SET result_json = ?", (json.dumps(result),))
            self.assertEqual(repository.progress("repo", Path("repo"), "main", 1, inventory).completed, ())
            self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
