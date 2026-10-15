"""OS별 프로세스 관측·실행 진입점의 회귀 검사."""
import ctypes
import errno
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest import mock

from loop_manager_internal.verification import coordination, darwin_process, resources
from loop_manager_internal.runtime import process_lock


class PlatformCompatibilityTests(unittest.TestCase):
    def test_darwin_resources_fall_back_without_reading_linux_procfs(self):
        with mock.patch.object(resources.sys, "platform", "darwin"), mock.patch.object(
            resources.os, "name", "posix"
        ), mock.patch.object(resources, "linux_sample", side_effect=AssertionError("procfs")):
            snapshot = resources.ResourceSampler().sample()
            decision = resources.decide(snapshot, now=snapshot.at, running=0, cap="auto")
            self.assertTrue(decision.granted)
            self.assertTrue(decision.fallback)
            self.assertEqual(decision.limit, 1)

    def test_darwin_identity_and_ancestry_do_not_read_procfs(self):
        with mock.patch.object(coordination.sys, "platform", "darwin"), mock.patch.object(
            darwin_process, "process_info", side_effect=lambda pid: {30: (20, 100001), 20: (10, 100000)}.get(pid)
        ), mock.patch.object(coordination.Path, "read_text", side_effect=AssertionError("procfs")):
            self.assertEqual(coordination.process_identity(30), 100001)
            self.assertIsNone(coordination.process_identity(999))
            self.assertTrue(coordination.descendant(30, 10))
            self.assertFalse(coordination.descendant(30, 5))

    def test_darwin_native_result_and_failures(self):
        library = mock.Mock()
        def query(pid, flavor, argument, pointer, size):
            self.assertEqual((flavor, argument, size), (3, 0, 136))
            info = ctypes.cast(pointer, ctypes.POINTER(darwin_process.ProcessInfo)).contents
            info.pid, info.ppid, info.status = pid, 10, 2
            info.seconds, info.microseconds = 123, 456
            return size
        library.proc_pidinfo.side_effect = query
        with mock.patch.object(darwin_process.ctypes, "CDLL", return_value=library):
            self.assertEqual(darwin_process.process_info(20), (10, 123000456))
            for error in (errno.ESRCH, errno.EPERM, 0):
                def fail(*args):
                    ctypes.set_errno(error)
                    return 0
                library.proc_pidinfo.side_effect = fail
                if error == errno.ESRCH:
                    self.assertIsNone(darwin_process.process_info(20))
                else:
                    with self.assertRaises(OSError):
                        darwin_process.process_info(20)

    def test_native_child_identity_is_stable_until_exit(self):
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            identity = coordination.process_identity(child.pid)
            self.assertIsNotNone(identity)
            self.assertEqual(coordination.process_identity(child.pid), identity)
            if os.name != "nt":
                self.assertTrue(coordination.descendant(child.pid, os.getpid()))
        finally:
            child.communicate(timeout=10)
        self.assertIsNone(coordination.process_identity(child.pid))

    def test_non_contention_oserror_is_not_hidden_on_windows(self):
        with mock.patch.object(process_lock.os, "name", "nt"):
            self.assertFalse(process_lock.is_lock_contention(OSError(errno.ENOLCK, "failed")))

    def test_verify_launcher_uses_venv_and_root_and_preserves_failure(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="verify space ") as directory:
            project = Path(directory) / "project"
            scripts = project / "scripts"
            scripts.mkdir(parents=True)
            suffix = "ps1" if os.name == "nt" else "sh"
            launcher = scripts / f"verify.{suffix}"
            shutil.copyfile(root / "scripts" / launcher.name, launcher)
            venv.EnvBuilder(with_pip=False).create(project / ".venv")
            package = project / "loop_manager_internal" / "verification"
            package.mkdir(parents=True)
            (package.parent / "__init__.py").touch()
            (package / "__init__.py").touch()
            (package / "runner.py").write_text(
                "import os, sys\nfrom pathlib import Path\n"
                "assert Path.cwd() == Path(__file__).resolve().parents[2]\n"
                "assert Path(sys.prefix) == Path.cwd() / '.venv'\n"
                "assert os.environ['PYTHONIOENCODING'] == 'utf-8'\n"
                "print('검증 진입점')\nraise SystemExit(37)\n", encoding="utf-8")
            command = ([shutil.which("pwsh") or "powershell", "-NoProfile", "-File"]
                       if os.name == "nt" else ["bash"])
            # Model Manager's injected launcher settings, then exercise standalone.
            environment = dict(os.environ)
            environment.update({
                "LOOP_MANAGER_VERIFY_RUNNER": str(project / "manager-entrypoint.py"),
                "LOOP_MANAGER_VERIFY_RUNTIME_PYTHON": sys.executable,
                "LOOP_MANAGER_VERIFY_VERSION": "1",
                "LOOP_MANAGER_VERIFY_TIMEOUT": "600",
            })
            environment = {key: value for key, value in environment.items()
                           if not key.startswith("LOOP_MANAGER_VERIFY_")}
            result = subprocess.run([*command, str(launcher)], cwd=directory,
                                    env=environment, capture_output=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
            self.assertIn("검증 진입점", result.stdout)
