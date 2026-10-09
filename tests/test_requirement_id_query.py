from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
TOOL = runpy.run_path(
    str(ROOT / ".agents/skills/specification-docs/scripts/next_requirement_id.py")
)


class RequirementIdQueryTest(unittest.TestCase):
    def _run(self, root: Path) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(Path, "cwd", return_value=root),
            mock.patch.object(sys, "argv", ["next_requirement_id.py"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = TOOL["main"]()
        return status, stdout.getvalue(), stderr.getvalue()

    def _write(self, root: Path, relative: str, content: str | bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def test_empty_active_directory_and_absent_archive_return_r001(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/requirements").mkdir(parents=True)

            status, stdout, stderr = self._run(root)

            self.assertEqual((status, stdout, stderr), (0, "R-001\n", ""))
            self.assertFalse((root / "docs/requirements/archive").exists())
            self.assertEqual(tuple((root / "docs/requirements").iterdir()), ())

    def test_source_ids_include_archive_and_untracked_files_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "docs/requirements/one.md", "# [R-008] Duplicate\n")
            self._write(root, "docs/requirements/two.md", "# [R-008] Duplicate\n")
            self._write(root, "docs/requirements/legacy.md", "# [R-012] Legacy title\n")
            self._write(
                root,
                "docs/requirements/mismatch.md",
                "---\nid: R-100\ntitle: Frontmatter title\n---\n"
                "# [R-012] Body title\n",
            )
            self._write(
                root,
                "docs/requirements/archive/old.md",
                "# [R-015] Archived title\n",
            )
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            status, stdout, stderr = self._run(root)

            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual((status, stdout, stderr), (0, "R-101\n", ""))
            self.assertEqual(after, before)
            self.assertFalse((root / ".git").exists())
            self.assertEqual(
                TOOL["requirement_identity"](
                    (root / "docs/requirements/mismatch.md").read_text(encoding="utf-8")
                ),
                ("R-100", "Frontmatter title"),
            )
            self.assertEqual(
                TOOL["requirement_identity"]("# [R-012] Legacy title\n"),
                ("R-012", "Legacy title"),
            )

    def test_invalid_documents_fail_without_partial_stdout(self):
        invalid_documents = (
            ("unterminated frontmatter", b"---\nid: R-002\ntitle: Broken\n"),
            ("duplicate key", "---\nid: R-002\nid: R-003\ntitle: Broken\n---\n"),
            ("missing title", "---\nid: R-002\n---\n# [R-002] Body\n"),
            ("invalid ID", "---\nid: G-002\ntitle: Broken\n---\n"),
            ("malformed heading", "# Missing ID\n"),
            ("invalid encoding", b"\xff"),
        )
        for name, invalid in invalid_documents:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write(root, "docs/requirements/good.md", "# [R-010] Good\n")
                self._write(root, "docs/requirements/bad.md", invalid)

                status, stdout, stderr = self._run(root)

                self.assertEqual(status, 1)
                self.assertEqual(stdout, "")
                self.assertIn("Requirement ID 조회 오류:", stderr)

    def test_missing_active_directory_and_read_failure_return_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, stdout, stderr = self._run(root)
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertIn("활성 Requirement 디렉터리가 없습니다", stderr)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self._write(root, "docs/requirements/blocked.md", "# [R-009] Blocked\n")
            read_bytes = Path.read_bytes

            def deny_target(path: Path) -> bytes:
                if path == target:
                    raise PermissionError("denied")
                return read_bytes(path)

            with mock.patch.object(Path, "read_bytes", deny_target):
                status, stdout, stderr = self._run(root)

            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Requirement 파일을 읽을 수 없습니다", stderr)

    def test_lookup_does_not_call_git_database_network_or_agent_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "docs/requirements/one.md", "# [R-001] One\n")
            with ExitStack() as guards:
                for target in (
                    "subprocess.run",
                    "subprocess.Popen",
                    "sqlite3.connect",
                    "urllib.request.urlopen",
                    "os.system",
                ):
                    guards.enter_context(
                        mock.patch(target, side_effect=AssertionError(f"unexpected call: {target}"))
                    )
                status, stdout, stderr = self._run(root)

            self.assertEqual((status, stdout, stderr), (0, "R-002\n", ""))


if __name__ == "__main__":
    unittest.main()
