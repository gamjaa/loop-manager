import unittest
import loop_manager
from loop_manager_internal.execution.result_validation import reviewer_targeted_test_evidence
from loop_manager_internal.execution.worker_outcome import parse_worker_outcome
from tests.support.worker import worker_result


class WorkerResultContractTest(unittest.TestCase):
    plan = {"id": "G-008", "title": "Descriptive Worker Commits"}

    def test_korean_inflected_test_success_is_recognized_without_accepting_negation(self):
        message = worker_result(
            "Recognize Korean test evidence",
            "Accept ordinary Korean success wording.",
            "29개 targeted unit case 통과했습니다.",
        )
        publication = loop_manager.parse_worker_result(message, self.plan)
        self.assertIn("통과했습니다", publication.test_evidence)
        with self.assertRaises(loop_manager.WorkerResultError):
            loop_manager.parse_worker_result(
                worker_result(
                    "Reject negative Korean test evidence",
                    "A negated result is not a successful test.",
                    "targeted unit test 통과하지 못했습니다.",
                ),
                self.plan,
            )

    def test_worker_result_fixture_satisfies_new_and_publication_contracts(self):
        message = worker_result(
            "Store immutable plan links", "Adds immutable PR context.",
            "test_one: PASS",
        )
        outcome = parse_worker_outcome(
            message, plan_path="docs/plans/[G-008] Descriptive Worker Commits.md",
            sources=(), documents={},
        )
        publication = loop_manager.parse_worker_result(message, self.plan)
        self.assertEqual(outcome.kind, "ready_for_validation")
        self.assertEqual(publication.commit_title, "Store immutable plan links")

    def test_worker_result_contract_preserves_fields_and_rejects_bad_titles(self):
        publication = loop_manager.parse_worker_result(
            worker_result("Store immutable plan links", "Adds immutable PR context.", "test_one: PASS"),
            self.plan,
        )

        self.assertEqual(publication.commit_title, "Store immutable plan links")
        self.assertEqual(publication.summary, "Adds immutable PR context.")
        self.assertEqual(publication.test_evidence, "test_one: PASS")
        wrapped_title = loop_manager.parse_worker_result(
            'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
            'COMMIT_TITLE: Normalize result\nformat without retry\n'
            'PR_SUMMARY: Accept the same title with folded whitespace.\n'
            'TARGETED_TESTS: parser case: PASS',
            self.plan,
        )
        self.assertEqual(wrapped_title.commit_title, "Normalize result format without retry")
        resolved = loop_manager.parse_worker_result(
            worker_result(
                "Record resolved test evidence",
                "Records a corrected targeted test.",
                "FAILED_COMMAND: python3 -m unittest tests.test_one\n"
                "first run: FAIL\n"
                "RERUN: python3 -m unittest tests.test_one\nPASS",
            ),
            self.plan,
        )
        self.assertIn(
            "RERUN: python3 -m unittest tests.test_one", resolved.test_evidence
        )
        document_reason = "문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다."
        document_publication = loop_manager.parse_worker_result(
            worker_result("Apply approved document bytes", "Summary", document_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(document_publication.test_evidence),
            document_reason,
        )
        freeform_reason = "문서만 변경해 관련 unit test를 실행하지 않았습니다"
        freeform_publication = loop_manager.parse_worker_result(
            worker_result("Explain omitted document tests", "Summary", freeform_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(freeform_publication.test_evidence),
            freeform_reason,
        )
        alternate_reason = "문서만 변경되어 적용할 unit test가 없어 실행하지 않았습니다"
        alternate_publication = loop_manager.parse_worker_result(
            worker_result("Explain omitted unit tests", "Summary", alternate_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(alternate_publication.test_evidence),
            alternate_reason,
        )
        configuration_reason = "설정만 바뀌어 unit test를 수행하지 못했습니다"
        configuration_publication = loop_manager.parse_worker_result(
            worker_result("Explain omitted configuration tests", "Summary", configuration_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(configuration_publication.test_evidence),
            configuration_reason,
        )
        invalid_results = (
            worker_result("", "Summary", "test: PASS"),
            worker_result(
                "Report unresolved targeted failure",
                "Summary",
                "test: FAIL",
            ),
            worker_result("Descriptive Worker Commits", "Summary", "test: PASS"),
            worker_result("Implement G-008: Descriptive Worker Commits", "Summary", "test: PASS"),
            worker_result("Update", "Summary", "test: PASS"),
            worker_result("Implement G-008 worker metadata", "Summary", "test: PASS"),
            worker_result("Update.", "Summary", "test: PASS"),
            worker_result("Implement changes", "Summary", "test: PASS"),
            worker_result("Implement code changes", "Summary", "test: PASS"),
            worker_result("Update implementation", "Summary", "test: PASS"),
            worker_result("Implement the changes", "Summary", "test: PASS"),
            worker_result("Fix!", "Summary", "test: PASS"),
            worker_result("Descriptive Worker Commits.", "Summary", "test: PASS"),
            worker_result("Reject missing test disposition", "Summary", "none"),
            worker_result("Reject unexplained no-test claim", "Summary", "실행 가능한 관련 unit test가 없습니다"),
            worker_result("Reject negated pass result", "Summary", "test_api did not pass"),
            worker_result(
                "Reject unexplained mixed test report", "Summary",
                "실행 가능한 관련 unit test가 없습니다; test_api: PASS",
            ),
        )
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaises(loop_manager.WorkerResultError):
                    loop_manager.parse_worker_result(result, self.plan)

    def test_no_test_reason_omits_same_line_success_details_and_commands(self):
        reason = "문서만 변경해 관련 unit test를 실행하지 않았습니다"
        for evidence in (
            f"test_api: PASS; {reason}; python3 -m unittest tests.test_api",
            f"{reason} test_api: PASS python3 -m unittest tests.test_api",
            f"{reason} npm test: 3 tests passed",
        ):
            with self.subTest(evidence=evidence):
                publication = loop_manager.parse_worker_result(
                    worker_result("Explain document test omission", "Summary", evidence),
                    self.plan,
                )
                extracted = loop_manager.reviewer_targeted_test_reason(
                    publication.test_evidence,
                )
                self.assertEqual(extracted, reason)
                prompt = loop_manager.reviewer_prompt("main", extracted)
                self.assertIn(reason, prompt)
                self.assertNotIn("test_api: PASS", prompt)
                self.assertNotIn("python3 -m unittest tests.test_api", prompt)
                self.assertNotIn("npm test: 3 tests passed", prompt)

    def test_reviewer_targeted_evidence_keeps_reported_commands_and_results_only(self):
        secret = 'sk-' + 'A' * 24
        evidence = (
            f"FAILED_COMMAND: python3 -m unittest tests.test_case --token {secret}\n"
            "Initial run: FAIL (1 failure)\n"
            f"RERUN: python3 -m unittest tests.test_case --token {secret}\n"
            "Final run: PASS; API_KEY=hidden-value\n"
            "unrelated raw log: do not forward this line"
        )

        compact = reviewer_targeted_test_evidence(evidence)

        self.assertIn('FAILED_COMMAND: python3 -m unittest tests.test_case', compact)
        self.assertIn('RERUN: python3 -m unittest tests.test_case', compact)
        self.assertIn('Initial run: FAIL', compact)
        self.assertIn('Final run: PASS', compact)
        self.assertIn('<REDACTED>', compact)
        self.assertNotIn(secret, compact)
        self.assertNotIn('hidden-value', compact)
        self.assertNotIn('unrelated raw log', compact)
        self.assertEqual(
            reviewer_targeted_test_evidence('unrecognized private output'),
            'Worker targeted test 결과를 요약할 수 없습니다.',
        )

    def test_reviewer_targeted_evidence_keeps_other_language_and_continued_commands(self):
        evidence = (
            "go test ./internal/foo \\\n"
            "  -run TestFoo\n"
            "Result: PASS\n"
            "cargo test --package api\n"
            "test result: ok, 3 passed, 0 failed\n"
            "python3 tests/test_api.py\n"
            "Result: PASS\n"
            "unrelated raw log: omit this"
        )

        compact = reviewer_targeted_test_evidence(evidence)

        self.assertIn("go test ./internal/foo -run TestFoo", compact)
        self.assertIn("Result: PASS", compact)
        self.assertIn("cargo test --package api", compact)
        self.assertIn("test result: ok, 3 passed, 0 failed", compact)
        self.assertIn("python3 tests/test_api.py", compact)
        self.assertIn("Result: PASS", compact)
        self.assertEqual(
            compact,
            "go test ./internal/foo -run TestFoo\n"
            "Result: PASS\n"
            "cargo test --package api\n"
            "test result: ok, 3 passed, 0 failed\n"
            "python3 tests/test_api.py\n"
            "Result: PASS",
        )
        self.assertNotIn("unrelated raw log", compact)

    def test_reviewer_targeted_evidence_keeps_absolute_executable_paths(self):
        for command in (
            '/usr/bin/python3 tests/test_api.py',
            '/opt/tools/python3 tests/test_api.py',
        ):
            with self.subTest(command=command):
                evidence = command + '\nResult: PASS'
                self.assertEqual(reviewer_targeted_test_evidence(evidence), evidence)

    def test_reviewer_targeted_evidence_keeps_labeled_and_powershell_commands(self):
        cases = (
            ('COMMAND: /usr/bin/python3 tests/test_api.py',
             'COMMAND: /usr/bin/python3 tests/test_api.py'),
            ('python3 -m unittest `\n  tests.test_api',
             'python3 -m unittest tests.test_api'),
            ('COMMAND: /usr/bin/python3 `\n  tests/test_api.py',
             'COMMAND: /usr/bin/python3 tests/test_api.py'),
        )
        for reported, expected in cases:
            with self.subTest(reported=reported):
                evidence = reported + '\nResult: PASS\nunrelated raw log: omit this'
                publication = loop_manager.parse_worker_result(
                    worker_result('Preserve reported test scope', 'Summary', evidence),
                    self.plan,
                )
                self.assertEqual(
                    reviewer_targeted_test_evidence(publication.test_evidence),
                    expected + '\nResult: PASS',
                )

    def test_reviewer_targeted_evidence_keeps_argumentless_test_paths(self):
        for command in (
            'COMMAND: ./build/tests/test_api',
            './build/tests/test_api',
            'COMMAND: /opt/tests/test_api',
            'COMMAND: ./tests/test_api.sh',
        ):
            with self.subTest(command=command):
                publication = loop_manager.parse_worker_result(
                    worker_result('Preserve individual test paths', 'Summary',
                                  command + '\nResult: PASS\nunrelated raw log'),
                    self.plan,
                )
                self.assertEqual(
                    reviewer_targeted_test_evidence(publication.test_evidence),
                    command + '\nResult: PASS',
                )
        self.assertEqual(reviewer_targeted_test_evidence('unrelated\nResult: PASS'),
                         'Result: PASS')

    def test_targeted_test_evidence_requires_a_clean_explicit_rerun(self):
        valid_evidence = (
            "pytest: 0 failed, 2 passed",
            (
                "FAILED_COMMAND: python3  -m unittest tests.test_case\n"
                "first run: 1 failed, 2 passed\n"
                "RERUN: python3 -m  unittest tests.test_case\n3 passed"
            ),
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_syntax\n"
                "first run: SyntaxError 발생\n"
                "RERUN: python3 -m unittest tests.test_syntax\n1 passed"
            ),
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_api\n"
                "test_api did not pass\n"
                "RERUN: python3 -m unittest tests.test_api\n1 passed"
            ),
        )
        for evidence in valid_evidence:
            with self.subTest(evidence=evidence):
                publication = loop_manager.parse_worker_result(
                    worker_result(
                        "Validate clean targeted reruns", "Summary", evidence
                    ),
                    self.plan,
                )
                self.assertEqual(publication.test_evidence, evidence)

        invalid_evidence = (
            "pytest: 1 failed, 2 passed",
            "unittest: 1 error, 2 passed",
            "RERUN: pytest: 1 failed, 2 passed",
            "SyntaxError 발생, 수정 후 통과",
            "targeted test raised AssertionError: mismatch; then passed",
            "targeted test: FAIL\nunable to run the rerun",
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_a\n"
                "test_a: FAIL\n"
                "RERUN: python3 -m unittest tests.test_b\nPASS"
            ),
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_a\n"
                "test_a: FAIL\n"
                "RERUN: python3 -m unittest tests.test_a\n"
                "FAILED_COMMAND: python3 -m unittest tests.test_b\n"
                "test_b OK"
            ),
        )
        for evidence in invalid_evidence:
            with self.subTest(evidence=evidence):
                with self.assertRaises(loop_manager.WorkerResultError):
                    loop_manager.parse_worker_result(
                        worker_result(
                            "Reject mixed targeted summaries", "Summary", evidence
                        ),
                        self.plan,
                    )

    def test_two_corrected_runs_keep_initial_outcomes_and_clean_final_results(self):
        first = "python3 -m unittest tests.test_support_fixtures -v"
        second = "python3 -m unittest tests.test_worker_result_contract -v"
        evidence = (
            f"FAILED_COMMAND: {first}\n"
            "Initial run: FAIL (1 assertion)\n"
            f"RERUN: {first}\n"
            "Final run: PASS (9 tests)\n"
            f"FAILED_COMMAND: {second}\n"
            "Initial run: ERROR (2 errors)\n"
            f"RERUN: {second}\n"
            "Final run: PASS (5 tests)"
        )
        publication = loop_manager.parse_worker_result(
            worker_result("Preserve corrected unit test reports", "Keep both run outcomes.", evidence),
            self.plan,
        )
        self.assertEqual(publication.test_evidence, evidence)
        for ambiguous in (
            f"FAILED_COMMAND: {first}\nRERUN: {first}\nFinal run: PASS",
            evidence + "\n재실행 실패 없음.",
            (
                "실패 이력 설명\n"
                f"FAILED_COMMAND: {first}\nInitial run: FAIL\n"
                f"RERUN: {first}\nFinal run: PASS"
            ),
        ):
            with self.subTest(evidence=ambiguous):
                with self.assertRaises(loop_manager.WorkerResultError):
                    loop_manager.parse_worker_result(
                        worker_result("Reject ambiguous unit test reports", "Keep clear outcomes.", ambiguous),
                        self.plan,
                    )

    def test_github_origin_normalization_matches_repository_validation(self):
        self.assertEqual(
            loop_manager.github_repository("HTTPS://WWW.GitHub.Com/Owner/Repo.GIT"),
            "owner/repo",
        )
        url = loop_manager.immutable_github_blob_url(
            "https://www.github.com/Owner/Repo.git",
            "a" * 40,
            "docs/plans/[G-008] 한글.md",
        )
        self.assertIn("github.com/owner/repo/blob/", url)
        self.assertIn("%5BG-008%5D%20%ED%95%9C%EA%B8%80.md", url)

    def test_immutable_url_and_pr_body_encode_plan_context(self):
        plan = {
            "id": "G-008",
            "title": "Descriptive Worker Commits",
            "sources": ["docs/requirements/[R-004] 한글 요구사항.md"],
        }
        plan_path = "docs/plans/[G-008] Descriptive Worker Commits.md"
        plan_text = "---\nid: G-008\n---\n\n## Purpose\n\nKeep review context stable.\n"
        published = loop_manager.published_execplan(
            plan_path,
            """---
id: G-008
title: Descriptive Worker Commits
sources:
  - docs/requirements/[R-004] 한글 요구사항.md
depends_on: []
complexity: medium
risk: medium
---
""",
        )
        self.assertEqual(published["sources"], plan["sources"])

        sha = "a" * 40
        body = loop_manager.build_pr_body(
            loop_manager.WorkerPublication("Describe changes", "Adds PR context.", "test: PASS"),
            plan,
            plan_path,
            plan_text,
            "git@github.com:octo/loop-manager.git",
            sha,
        )

        self.assertIn("Adds PR context.", body)
        self.assertIn("Keep review context stable.", body)
        self.assertIn(f"/blob/{sha}/docs/plans/%5BG-008%5D%20Descriptive%20Worker%20Commits.md", body)
        self.assertIn("%5BR-004%5D%20%ED%95%9C%EA%B8%80%20%EC%9A%94%EA%B5%AC%EC%82%AC%ED%95%AD.md", body)
