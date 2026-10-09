"""Unit contracts for verify manifest interpretation and conservative observation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from loop_manager_internal.verification import inputs, manager_adapter


class VerifyInputObservationTest(unittest.TestCase):
    def test_measured_compile_ignores_only_unobserved_generated_directories(self):
        from loop_manager_internal.verification.manager_adapter import _ISOLATED_COMPILE_PROGRAM
        for directory in ('.git', '.venv', '__pycache__'):
            target = self.root / 'src' / directory
            target.mkdir()
            (target / 'ignored.py').write_text('invalid syntax !')
        (self.root / 'src/app.py').write_text('valid = 1')
        with mock.patch('pathlib.Path', side_effect=lambda path: self.root / path):
            exec(_ISOLATED_COMPILE_PROGRAM, {})
            (self.root / 'src/app.py').write_text('invalid syntax !')
            with self.assertRaises(SyntaxError):
                exec(_ISOLATED_COMPILE_PROGRAM, {})

    def test_measured_compile_runtime_hashes_interpreter_stdlib_and_native_libraries(self):
        import contextlib
        import io
        import sys
        from loop_manager_internal.verification.manager_adapter import _COMPILE_RUNTIME_PROBE
        interpreter = self.root / 'python'
        interpreter.write_bytes(b'interpreter')
        library = self.root / 'native.so'
        library.write_bytes(b'native')
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == Path('/proc/self/maps'):
                return '001-002 r-xp 000 00:00 0 /mapped-library.so\n'
            return original_read(path, *args, **kwargs)
        def identity():
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(_COMPILE_RUNTIME_PROBE, {})
            return output.getvalue().strip()
        with mock.patch.object(sys, 'executable', str(interpreter)), \
                mock.patch.object(sys, 'path', [str(self.root / 'src')]), \
                mock.patch.object(sys, 'platform', 'linux'), \
                mock.patch.object(os, 'geteuid', return_value=1000, create=True), \
                mock.patch.object(os, 'getegid', return_value=1000, create=True), \
                mock.patch.object(os, 'getgroups', return_value=[1000], create=True), \
                mock.patch.object(os, 'uname', return_value=('Linux', 'fixture', '1', '1', 'x86_64'), create=True), \
                mock.patch('pathlib.Path', side_effect=lambda path: (
                    library if path == '/mapped-library.so' else Path(path))), \
                mock.patch.object(Path, 'read_text', read):
            first = identity()
            self.assertEqual(len(first), 64)
            self.assertEqual(identity(), first)
            for file in (interpreter, self.root / 'src/app.py', library):
                before = identity()
                file.write_bytes(file.read_bytes() + b'changed')
                self.assertNotEqual(identity(), before)

    def test_measured_compile_command_and_uncertainty_use_real_execution_boundary(self):
        from loop_manager_internal.verification import manager_adapter
        from loop_manager_internal.execution import result_validation
        import subprocess
        script = self.root / 'scripts/verify.sh'
        script.write_bytes(manager_adapter.ISOLATED_COMPILE_SCRIPT)
        self.manifest(['src/**', 'scripts/**'])
        effects = result_validation.VerifyEffects(mock.Mock(), mock.Mock(),
                                                  mock.Mock(return_value=(True, False, 'OK')))
        with mock.patch.object(result_validation, 'os', SimpleNamespace(name='posix', environ={})), \
                mock.patch.object(manager_adapter, 'os', SimpleNamespace(name='posix')), \
                mock.patch.object(manager_adapter.shutil, 'which', return_value=str(self.root / 'python')), \
                mock.patch.object(manager_adapter.subprocess, 'run',
                    return_value=SimpleNamespace(stdout='a' * 64)) as probe:
            observed = result_validation.observe_verify_inputs(self.root)
            self.assertTrue(observed.proven, observed.reason)
            self.assertTrue(result_validation.verify(effects, self.root)[0])
            arguments = effects.run_verify_process.call_args.args[0]
            self.assertEqual(arguments, result_validation.verify_arguments(self.root)[1])
            self.assertEqual(arguments[1:5], ['-I', '-S', '-B', '-c'])
            probe.side_effect = subprocess.TimeoutExpired('measurement', 30)
            self.assertFalse(result_validation.observe_verify_inputs(self.root).proven)
            probe.side_effect = None
            with mock.patch.dict(result_validation.os.environ, {'LD_PRELOAD': 'unmeasured'}):
                self.assertFalse(result_validation.observe_verify_inputs(self.root).proven)
            (self.root / inputs.MANIFEST).write_text('invalid')
            self.assertFalse(result_validation.observe_verify_inputs(self.root).proven)

    def test_windows_compile_contract_falls_back_to_powershell_without_measurement(self):
        from loop_manager_internal.execution import result_validation
        (self.root / 'scripts/verify.sh').write_bytes(manager_adapter.ISOLATED_COMPILE_SCRIPT)
        script = self.root / 'scripts/verify.ps1'
        script.write_text('exit 0')
        self.manifest(['src/**', 'scripts/**'])
        effects = result_validation.VerifyEffects(mock.Mock(), mock.Mock(),
                                                  mock.Mock(return_value=(True, False, 'OK')))
        with mock.patch.object(result_validation, 'os', SimpleNamespace(name='nt', environ={})), \
                mock.patch.object(manager_adapter, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(result_validation.shutil, 'which', return_value=None), \
                mock.patch.object(manager_adapter.subprocess, 'run') as probe:
            self.assertFalse(result_validation.observe_verify_inputs(self.root).proven)
            self.assertTrue(result_validation.verify(effects, self.root)[0])
            self.assertEqual(effects.run_verify_process.call_args.args[0],
                ['pwsh', '-NoProfile', '-File', str(script)])
            probe.assert_not_called()

    def test_observation_ignores_read_access_time_but_detects_file_mutations(self):
        target = self.root / 'src/app.py'
        original = target.lstat()
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_atime_ns')
        before = SimpleNamespace(**{key: getattr(original, key) for key in fields})
        lstat = Path.lstat
        for field in ('st_atime_ns', 'st_size', 'st_mode', 'st_ino', 'st_mtime_ns', 'st_ctime_ns'):
            after = SimpleNamespace(**{**vars(before), field: getattr(before, field) + 1})
            samples = iter((before, after))
            with self.subTest(field=field), mock.patch.object(Path, 'lstat',
                    lambda path: next(samples) if path == target else lstat(path)):
                observed = self.observe()
            if field == 'st_atime_ns':
                self.assertTrue(observed.proven, observed.reason)
            else:
                self.assertIsNone(observed.fingerprint)
                self.assertIn('changed during observation', observed.reason)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'src').mkdir()
        (self.root / 'src/app.py').write_text('initial')
        self.manifest(['src/**'])

    def manifest(self, paths):
        (self.root / inputs.MANIFEST).write_text(json.dumps({'version': 1, 'paths': paths}))

    def observe(self, **kwargs):
        context = dict(environment={'PYTHONIOENCODING': 'utf-8'}, runtime_identity='python-executable-hash',
                       git_inputs={}, external_inputs={})
        context.update(kwargs)
        return manager_adapter.observe_inputs(self.root, ['bash', 'scripts/verify.sh'], **context)

    def test_globstar_zero_segments_dotfiles_and_segment_boundaries(self):
        for path in ('src/main.py', 'src/nested/main.py', 'src/.hidden/main.py'):
            self.assertTrue(inputs.matches(path, 'src/**/*.py'))
        self.assertFalse(inputs.matches('src/nested/main.py', 'src/*.py'))
        self.assertFalse(inputs.matches('SRC/main.py', 'src/**'))
        self.assertTrue(inputs.matches('src/a.py', 'src/?.py'))
        self.assertFalse(inputs.matches('src/a/b.py', 'src/?.py'))

    def test_invalid_format_and_unsafe_paths(self):
        for value in ({'version': True, 'paths': ['src/**']}, {'version': 2, 'paths': ['src/**']},
                      {'version': 1, 'paths': []}, {'version': 1, 'paths': ['src/**'], 'extra': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                inputs.parse(json.dumps(value).encode())
        for path in ('/src', '../src', 'src/../other', 'C:/src', 'src\\app', 'src//a',
                     'src/**.py', 'src/[ab].py', '.', 'src/', '', '.git/**', '.venv/**'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                inputs.parse(json.dumps({'version': 1, 'paths': [path]}).encode())
        for raw in (b'{"version":1,"version":1,"paths":["src/**"]}',
                    b'{"version":1,"paths":["src/**","src/**"]}'):
            with self.assertRaises(ValueError):
                inputs.parse(raw)

    def test_missing_invalid_manifest_and_literal_fail_closed(self):
        self.manifest(['absent.py'])
        self.assertIn('missing literal', self.observe().reason)
        self.manifest(['absent/**', 'src/**'])
        self.assertTrue(self.observe().proven)
        (self.root / inputs.MANIFEST).unlink()
        self.assertIsNone(self.observe().fingerprint)
        (self.root / inputs.MANIFEST).write_text('invalid')
        self.assertIsNone(self.observe().fingerprint)

    def test_content_path_creation_deletion_and_manifest_are_inputs(self):
        first = self.observe()
        self.assertTrue(first.proven)
        (self.root / 'src/app.py').write_text('changed')
        second = self.observe()
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        (self.root / 'src/app.py').rename(self.root / 'src/moved.py')
        third = self.observe()
        self.assertNotEqual(second.fingerprint, third.fingerprint)
        (self.root / 'src/untracked.py').write_text('new')
        fourth = self.observe()
        self.assertNotEqual(third.fingerprint, fourth.fingerprint)
        (self.root / 'src/untracked.py').unlink()
        self.assertEqual(third.fingerprint, self.observe().fingerprint)
        self.manifest(['src/**', 'empty/**'])
        self.assertNotEqual(third.fingerprint, self.observe().fingerprint)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable permission observation')
    def test_executable_mode_and_symlink_kind_fail_closed(self):
        first = self.observe()
        (self.root / 'src/app.py').chmod(0o755)
        self.assertNotEqual(first.fingerprint, self.observe().fingerprint)
        (self.root / 'outside.py').write_text('outside')
        (self.root / 'src/app.py').unlink()
        (self.root / 'src/app.py').symlink_to('../outside.py')
        observed = self.observe()
        self.assertIn('non-regular', observed.reason)
        self.assertEqual(observed.files[-1][1], 'symlink')

    @unittest.skipIf(os.name == 'nt', 'symlink creation requires Windows privilege')
    def test_symlink_directory_and_manifest_are_not_followed(self):
        (self.root / 'src/link').symlink_to(self.root / 'scripts', target_is_directory=True)
        self.assertIn('non-regular', self.observe().reason)
        manifest = self.root / inputs.MANIFEST
        manifest.rename(self.root / 'real.json')
        manifest.symlink_to(self.root / 'real.json')
        self.assertIsNone(self.observe().fingerprint)

    def test_outside_manifest_requires_exact_bounded_proof(self):
        outside = self.root / 'config.json'
        outside.write_text('initial')
        first = self.observe()
        self.assertFalse(first.proven)
        proven = self.observe(completeness=first.completeness_token)
        self.assertTrue(proven.proven)
        outside.write_text('changed')
        self.assertFalse(self.observe(completeness=first.completeness_token).proven)
        outside.rename(self.root / 'moved.json')
        self.assertFalse(self.observe(completeness=first.completeness_token).proven)
        current = self.observe()
        self.assertFalse(self.observe(completeness=current.completeness_token,
                                      environment={'MODE': 'changed'}).proven)

    def test_exclusion_proof_expires_when_selected_script_or_settings_change(self):
        self.manifest(['src/**', 'scripts/**', 'verify-config.json'])
        script = self.root / 'scripts/verify.sh'
        config = self.root / 'verify-config.json'
        script.write_text('verify src')
        config.write_text('src only')
        (self.root / 'excluded.json').write_text('previously unused')
        for target in (script, config):
            with self.subTest(target=target.name):
                initial = self.observe()
                self.assertTrue(self.observe(completeness=initial.completeness_token).proven)
                target.write_text('verify now reads excluded.json')
                changed = self.observe(completeness=initial.completeness_token)
                self.assertFalse(changed.proven)
                self.assertNotEqual(initial.completeness_token, changed.completeness_token)
                self.assertTrue(self.observe(completeness=changed.completeness_token).proven)

    def test_command_environment_runtime_git_external_and_unknown(self):
        first = self.observe()
        for context in (dict(environment={'MODE': 'changed'}), dict(runtime_identity='other-python'),
                        dict(git_inputs={'HEAD': 'different'}), dict(external_inputs={'library': 'new'})):
            with self.subTest(context=context):
                self.assertNotEqual(first.fingerprint, self.observe(**context).fingerprint)
        other = manager_adapter.observe_inputs(self.root, ['other'], environment={}, runtime_identity='python',
                                               git_inputs={}, external_inputs={})
        self.assertNotEqual(first.fingerprint, other.fingerprint)
        for context in (dict(runtime_identity=None), dict(git_inputs=None), dict(external_inputs=None)):
            self.assertFalse(self.observe(**context).proven)

    def test_missing_and_invalid_manifest_preserve_full_verify_execution(self):
        from types import SimpleNamespace
        coordinator = mock.Mock()
        coordinator.open.return_value = ('session', 'secret')
        coordinator.endpoint = {'address': 'test-only'}
        from threading import Lock
        coordinator.lock = Lock()
        coordinator.reservations = {}
        context = SimpleNamespace(coordinator=coordinator, state_dir=self.root, cap=1)
        effects = SimpleNamespace(run_verify_process=mock.Mock(return_value=(True, False, 'passed')))
        for raw in (None, 'invalid'):
            manifest = self.root / inputs.MANIFEST
            if raw is None:
                manifest.unlink()
            else:
                manifest.write_text(raw)
            token = manager_adapter.settings.set(context)
            try:
                self.assertEqual(manager_adapter.run(effects, ['verify-command'], self.root, 60),
                                 (True, False, 'passed'))
            finally:
                manager_adapter.settings.reset(token)
        self.assertEqual(effects.run_verify_process.call_count, 2)

    def test_walk_read_failure_is_not_empty_input_success(self):
        def failed_walk(root, **kwargs):
            kwargs['onerror'](PermissionError('unreadable directory'))
        with mock.patch.object(inputs.os, 'walk', side_effect=failed_walk):
            self.assertIsNone(self.observe().fingerprint)

    def test_repository_manifest_covers_actual_fixture_documents_and_skills(self):
        root = Path(__file__).resolve().parents[1]
        patterns = inputs.parse((root / inputs.MANIFEST).read_bytes())
        for path in ('loop_manager.py', 'loop_manager_internal/verification/runner.py',
                     'tests/support/origin.py', 'scripts/verify.sh', 'scripts/verify.ps1',
                     'requirements.txt', 'config/config.ini.example', 'AGENTS.md', 'README.md',
                     'docs/PLANS.md', 'docs/AGENTS.worker.md', 'docs/PRODUCT.md',
                     'docs/ARCHITECTURE.md',
                     'docs/FUTURE_IDEAS.md', 'docs/PRODUCT.template.md',
                     'docs/ARCHITECTURE.template.md', 'docs/REQUIREMENT.template.md',
                     '.agents/skills/specification-docs/SKILL.md',
                     '.agents/skills/specification-docs/scripts/next_requirement_id.py'):
            with self.subTest(path=path):
                self.assertTrue(any(inputs.matches(path, pattern) for pattern in patterns))
