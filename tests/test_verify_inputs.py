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

    def test_compile_execution_keeps_fast_path_without_runtime_reuse_gate(self):
        from loop_manager_internal.verification import manager_adapter
        from loop_manager_internal.execution import result_validation
        script = self.root / 'scripts/verify.sh'
        script.write_bytes(manager_adapter.ISOLATED_COMPILE_SCRIPT)
        self.manifest(['src/**', 'scripts/**'])
        effects = result_validation.VerifyEffects(mock.Mock(), mock.Mock(),
                                                  mock.Mock(return_value=(True, False, 'OK')))
        with mock.patch.object(result_validation, 'os', SimpleNamespace(name='posix', environ={})), \
                mock.patch.object(manager_adapter, 'os', SimpleNamespace(name='posix')), \
                mock.patch.object(manager_adapter.shutil, 'which', return_value=str(self.root / 'python')), \
                mock.patch('subprocess.run') as probe:
            observed = result_validation.observe_verify_inputs(self.root)
            self.assertTrue(observed.proven, observed.reason)
            self.assertTrue(result_validation.verify(effects, self.root)[0])
            arguments = effects.run_verify_process.call_args.args[0]
            self.assertEqual(arguments, result_validation.verify_arguments(self.root)[1])
            self.assertEqual(arguments[1:5], ['-I', '-S', '-B', '-c'])
            with mock.patch.dict(result_validation.os.environ, {'LD_PRELOAD': 'unmeasured'}):
                self.assertTrue(result_validation.observe_verify_inputs(self.root).proven)
            probe.assert_not_called()
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
                mock.patch('subprocess.run') as probe:
            self.assertTrue(result_validation.observe_verify_inputs(self.root).proven)
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
        self.assertTrue(self.observe(completeness=current.completeness_token,
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

    def test_environment_runtime_git_and_external_identity_do_not_gate_code_inputs(self):
        first = self.observe()
        for context in (dict(environment={'MODE': 'changed'}), dict(runtime_identity='other-python'),
                        dict(git_inputs={'HEAD': 'different'}), dict(external_inputs={'library': 'new'})):
            with self.subTest(context=context):
                self.assertEqual(first.fingerprint, self.observe(**context).fingerprint)
        other = manager_adapter.observe_inputs(self.root, ['other'], environment={}, runtime_identity='python',
                                               git_inputs={}, external_inputs={})
        self.assertNotEqual(first.fingerprint, other.fingerprint)
        for context in (dict(runtime_identity=None), dict(git_inputs=None), dict(external_inputs=None)):
            self.assertTrue(self.observe(**context).proven)

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

    def test_repository_manifest_covers_execution_inputs_and_fixed_samples(self):
        root = Path(__file__).resolve().parents[1]
        patterns = inputs.parse((root / inputs.MANIFEST).read_bytes())
        for path in ('loop_manager.py', 'loop_manager_internal/verification/runner.py',
                     'loop_manager_internal/catalog/frontmatter.py',
                     'tests/support/origin.py', 'tests/fixtures/project-files/AGENTS.md',
                     'tests/fixtures/project-files/docs/AGENTS.common.md',
                     'tests/fixtures/project-files/docs/AGENTS.planner.md',
                     'tests/fixtures/project-files/docs/AGENTS.planner_check.md',
                     'tests/fixtures/project-files/docs/AGENTS.worker.md',
                     'tests/fixtures/project-files/docs/AGENTS.reviewer.md',
                     'tests/fixtures/project-files/docs/AGENTS.ponytail.md',
                     'tests/fixtures/project-files/docs/PLANS.md',
                     'tests/fixtures/provider-profiles.ini', 'scripts/verify.json',
                     'scripts/verify.sh', 'scripts/verify.ps1',
                     'scripts/verify_windows.py', 'scripts/verify-windows-tests.json',
                     'scripts/setup.sh',
                     'scripts/copy-project-files.sh', 'scripts/copy-project-files.ps1',
                     '.agents/skills/specification-docs/scripts/next_requirement_id.py',
                     '.agents/skills/specification-docs/scripts/requirement_documents.py',
                     '.agents/skills/specification-docs/scripts/read_requirements.py',
                     'requirements.txt', 'run.sh', 'run.ps1',
                     'docs/AGENTS.common.md', 'docs/AGENTS.planner.md',
                     'docs/AGENTS.planner_check.md', 'docs/AGENTS.worker.md',
                     'docs/AGENTS.reviewer.md', 'docs/AGENTS.ponytail.md'):
            with self.subTest(path=path):
                self.assertTrue(any(inputs.matches(path, pattern) for pattern in patterns))
        for path in ('AGENTS.md', 'README.md', 'config/config.ini.example',
                     'docs/PLANS.md', 'docs/PRODUCT.md', 'docs/ARCHITECTURE.md',
                     'docs/FUTURE_IDEAS.md', 'docs/VERIFY.md',
                     'docs/PRODUCT.template.md', 'docs/ARCHITECTURE.template.md',
                     'docs/REQUIREMENT.template.md',
                     '.agents/skills/specification-docs/SKILL.md',
                     'scripts/setup.ps1', '.gitignore', '.gitattributes'):
            with self.subTest(path=path):
                self.assertFalse(any(inputs.matches(path, pattern) for pattern in patterns))

    def test_setup_input_changes_invalidate_fingerprint_and_exclusion_proof(self):
        root = Path(__file__).resolve().parents[1]
        setup = self.root / 'scripts/setup.sh'
        source = (root / 'scripts/setup.sh').read_bytes()
        self.manifest(['src/**', 'scripts/setup.sh'])
        (self.root / 'notes.md').write_text('excluded by this unit fixture')
        for change in ('bytes', 'kind', 'mode'):
            with self.subTest(change=change):
                if setup.is_symlink():
                    setup.unlink()
                setup.write_bytes(source)
                setup.chmod(0o644)
                before = self.observe()
                self.assertTrue(self.observe(completeness=before.completeness_token).proven)
                if change == 'bytes':
                    setup.write_bytes(source + b'\n# changed setup\n')
                elif change == 'kind':
                    setup.unlink()
                    setup.symlink_to('../src/app.py')
                else:
                    setup.chmod(0o755)
                after = self.observe(completeness=before.completeness_token)
                self.assertNotEqual(before.fingerprint, after.fingerprint)
                self.assertNotEqual(before.completeness_token, after.completeness_token)
                self.assertFalse(after.proven)

    def test_selected_entrypoint_and_connection_require_manifest_coverage(self):
        from loop_manager_internal.execution import result_validation
        self.enterContext(mock.patch.object(result_validation, 'os', SimpleNamespace(name='posix')))
        script = self.root / 'scripts/verify.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        self.assertIn('entrypoint outside manifest',
                      result_validation.observe_verify_inputs(self.root).reason)
        self.manifest(['src/**', 'scripts/verify.sh'])
        self.assertTrue(result_validation.observe_verify_inputs(self.root).proven)
        (self.root / 'scripts/verify.json').write_text('{}')
        self.assertIn('connection outside manifest',
                      result_validation.observe_verify_inputs(self.root).reason)

    def test_changed_observation_rules_invalidate_fingerprint_and_exclusion(self):
        (self.root / 'notes.md').write_text('reviewed document')
        original = self.observe()
        with mock.patch.object(inputs, 'RULES', inputs.RULES + ';changed'):
            changed = self.observe(completeness=original.completeness_token)
        self.assertNotEqual(original.fingerprint, changed.fingerprint)
        self.assertFalse(changed.proven)

    def test_current_documents_are_reconfirmed_and_consumption_or_new_code_blocks_reuse(self):
        from loop_manager_internal.execution import result_validation
        from loop_manager_internal.execution.result_validation import observe_verify_inputs
        self.enterContext(mock.patch.object(result_validation, 'os', SimpleNamespace(name='posix')))
        repository = Path(__file__).resolve().parents[1]
        (self.root / 'scripts/verify.sh').write_bytes((repository / 'scripts/verify.sh').read_bytes())
        self.manifest(['src/**', 'scripts/**', 'tests/**'])
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_app.py').write_text('import unittest\n')
        (self.root / 'scripts/verify.json').write_text(json.dumps({
            'version': 1, 'adapter': 'unittest', 'environment': {'python': 'launcher'},
            'stages': [{'id': 'tests', 'kind': 'tests', 'cwd': '.',
                        'collection': {'directory': 'tests', 'pattern': 'test*.py'},
                        'execution': 'independent'}]}))
        (self.root / 'docs').mkdir()
        document = self.root / 'docs/PRODUCT.md'
        document.write_text('# Original product')
        initial = observe_verify_inputs(self.root)
        self.assertTrue(initial.proven, initial.reason)
        actions = [lambda: document.write_text('# Changed product')]
        if os.name != 'nt':
            actions.append(lambda: document.chmod(0o755))
        for action in actions:
            action()
            current = observe_verify_inputs(self.root)
            self.assertTrue(current.proven, current.reason)
            self.assertEqual(current.fingerprint, initial.fingerprint)
            self.assertNotEqual(current.completeness_token, initial.completeness_token)
        original_read = Path.read_bytes
        reads = 0
        def changed_read(path):
            nonlocal reads
            if path == self.root / 'src/app.py':
                reads += 1
                if reads == 2:
                    return b'changed during proof'
            return original_read(path)
        with mock.patch.object(Path, 'read_bytes', changed_read):
            changed = observe_verify_inputs(self.root)
        self.assertFalse(changed.proven)
        self.assertIn('code changed during exclusion review', changed.reason)
        (self.root / 'src/app.py').write_text("from pathlib import Path\nPath('docs/PRODUCT.md').read_text()\n")
        self.assertFalse(observe_verify_inputs(self.root).proven)
        (self.root / 'src/app.py').write_text('pass\n')
        (self.root / 'new.py').write_text('new code')
        self.assertFalse(observe_verify_inputs(self.root).proven)

        (self.root / 'new.py').unlink()
        (self.root / 'src/app.py').write_text('open(input()).read()\n')
        self.assertFalse(observe_verify_inputs(self.root).proven)
        (self.root / 'src/app.py').write_text('import ctypes\n')
        self.assertFalse(observe_verify_inputs(self.root).proven)
        (self.root / 'src/app.py').write_text('pass\n')
        script = self.root / 'scripts/verify.sh'
        script.write_bytes(script.read_bytes() + b'\ncat docs/PRODUCT.md\n')
        self.assertFalse(observe_verify_inputs(self.root).proven)

    def test_shared_code_index_bytes_invalidate_exclusion_and_fingerprint(self):
        from loop_manager_internal.maintenance import code_index
        target = Path(code_index.__file__).resolve()
        (self.root / 'notes.md').write_text('reviewed document')
        original = self.observe()
        read_bytes = Path.read_bytes
        with mock.patch.object(Path, 'read_bytes', lambda path: (
                read_bytes(path) + b'\n# changed index rules\n' if path == target else read_bytes(path))):
            changed = self.observe(completeness=original.completeness_token)
        self.assertNotEqual(original.fingerprint, changed.fingerprint)
        self.assertFalse(changed.proven)


class WorkerExclusionReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'src').mkdir()
        (self.root / 'docs').mkdir()
        (self.root / 'src/app.py').write_text('VALUE = 1')
        for name in ('verify.sh', 'verify.ps1'):
            (self.root / 'scripts' / name).write_text('custom native verify')
        (self.root / inputs.MANIFEST).write_text(json.dumps(
            dict(version=1, paths=['src/**', 'scripts/**'])))
        self.document = self.root / 'docs/AGENTS.worker.md'
        self.document.write_text('test-owned runtime guidance')
        self.review = inputs.exclusion_request(self.root, ('scripts/verify.sh', 'scripts/verify.ps1'),
                                                ('config/operating.ini',))
        self.review['basis'] = 'Both custom scripts consume only src; runtime guidance is never read.'

    def observe(self, review=None, entrypoint='scripts/verify.sh'):
        return inputs.reconfirm_worker_exclusions(self.root, (entrypoint,), review or self.review)

    def test_current_document_bytes_and_mode_reconfirm_without_environment_proof(self):
        before = self.observe()
        self.assertTrue(before.proven, before.reason)
        self.document.write_text('changed after rebase')
        self.document.chmod(0o755)
        after = self.observe()
        self.assertTrue(after.proven, after.reason)
        self.assertEqual(before.fingerprint, after.fingerprint)
        self.assertNotEqual(before.completeness_token, after.completeness_token)
        self.assertNotEqual(before.uncovered, after.uncovered)
        self.assertTrue(self.observe(entrypoint='scripts/verify.ps1').proven)
        (self.root / 'config').mkdir()
        config = self.root / 'config/operating.ini'
        for content in ('first', 'second'):
            config.write_text(content)
            self.assertEqual(after, self.observe())
        config.unlink()
        self.assertEqual(after, self.observe())

    def test_code_manifest_entrypoint_and_outside_path_changes_reject_old_review(self):
        for target in (self.root / 'src/app.py', self.root / inputs.MANIFEST,
                       self.root / 'scripts/verify.sh'):
            original = target.read_bytes()
            target.write_bytes(original + b'\n')
            self.assertFalse(self.observe().proven)
            target.write_bytes(original)
            mode = target.stat().st_mode & 0o777
            target.chmod(mode ^ 0o111)
            self.assertFalse(self.observe().proven)
            target.chmod(mode)
        new = self.root / 'missing-code.py'
        new.write_text('untracked code')
        self.assertFalse(self.observe().proven)
        new.unlink()
        moved = self.root / 'moved.md'
        self.document.rename(moved)
        self.assertFalse(self.observe().proven)
        moved.rename(self.document)
        self.document.unlink()
        self.assertFalse(self.observe().proven)
        self.document.symlink_to(self.root / 'src/app.py')
        self.assertFalse(self.observe().proven)
        self.assertFalse(self.observe(entrypoint='scripts/other.sh').proven)

    def test_manifest_selected_fixed_configuration_overrides_operating_exclusion(self):
        (self.root / 'config').mkdir()
        config = self.root / 'config/operating.ini'
        config.write_text('fixed fixture')
        manifest = self.root / inputs.MANIFEST
        manifest.write_text(json.dumps(dict(version=1, paths=['src/**', 'scripts/**', 'config/**'])))
        review = inputs.exclusion_request(self.root, ('scripts/verify.sh',), ('config/operating.ini',))
        review['basis'] = 'Runtime guidance is unconsumed; configuration is selected.'
        current = self.observe(review)
        self.assertTrue(current.proven, current.reason)
        self.assertIn('config/operating.ini', {item[0] for item in current.files})
        config.write_text('changed fixed fixture')
        self.assertFalse(self.observe(review).proven)
        config.unlink()
        self.assertFalse(self.observe(review).proven)

    def test_review_parser_requires_explicit_unique_complete_declaration(self):
        line = inputs.EXCLUSION_MARKER + json.dumps(self.review)
        self.assertEqual(inputs.worker_exclusions('PASS\n' + line), self.review)
        request = dict(self.review)
        del request['basis']
        for message in ('PASS', inputs.EXCLUSION_MARKER + json.dumps(request), line + '\n' + line,
                        inputs.EXCLUSION_MARKER + '{"version":1,"version":1}',
                        inputs.EXCLUSION_MARKER + json.dumps(dict(self.review, version=True)),
                        inputs.EXCLUSION_MARKER + json.dumps(dict(self.review, operating_paths=['../escape'])),
                        inputs.EXCLUSION_MARKER + json.dumps(dict(self.review, operating_paths=['docs/**']))):
            self.assertIsNone(inputs.worker_exclusions(message), message)

    def test_read_errors_and_mutation_between_observations_never_confirm(self):
        read_bytes = Path.read_bytes
        def unreadable(path):
            if path == self.document:
                raise PermissionError('cannot read exclusion')
            return read_bytes(path)
        with mock.patch.object(Path, 'read_bytes', unreadable):
            self.assertFalse(self.observe().proven)
        observe = inputs.observe
        def mutate(*args, **kwargs):
            result = observe(*args, **kwargs)
            if 'completeness' not in kwargs:
                self.document.write_text('racing mutation')
            return result
        with mock.patch.object(inputs, 'observe', side_effect=mutate):
            self.assertFalse(self.observe().proven)

    def test_production_observer_accepts_explicit_review_for_custom_entrypoints(self):
        from loop_manager_internal.execution import result_validation
        message = inputs.EXCLUSION_MARKER + json.dumps(self.review)
        for platform, script in (('posix', 'verify.sh'), ('nt', 'verify.ps1')):
            with mock.patch.object(result_validation, 'os', SimpleNamespace(name=platform)):
                self.assertFalse(result_validation.observe_verify_inputs(self.root).proven)
                observed = result_validation.observe_verify_inputs(self.root, worker_result=message)
                self.assertTrue(observed.proven, observed.reason)
                (self.root / 'scripts' / script).write_text('now consumes runtime guidance')
                self.assertFalse(result_validation.observe_verify_inputs(self.root, worker_result=message).proven)
                (self.root / 'scripts' / script).write_text('custom native verify')
