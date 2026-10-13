"""상태 루트·인증 링크·호출 환경을 임시 파일과 모의 CLI로 확인한다."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.agents import agent_profiles as profiles, provider_home as homes
from loop_manager_internal.agents import agent_codex_sandbox as sandbox, agent_write_isolation as isolation
from tests.support.workspace import native_agent_boundary


class ProviderHomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.user = self.root / 'user'
        self.state = self.root / 'state'
        self.variables = {'HOME': str(self.user), 'USERPROFILE': str(self.user), 'PATH': '/bin',
                          'CODEX_HOME': '/personal', 'codex_home': '/other',
                          'CLAUDE_CONFIG_DIR': '/personal-claude', 'TELEGRAM_TOKEN': 'private'}

    def environment(self, provider='codex', state=None):
        return profiles.agent_environment(provider, state_dir=state or self.state,
                                          environment_variables=self.variables)

    def source(self, provider='codex'):
        _, folder, filename, _, _ = homes.PROVIDERS[provider]
        path = self.user / folder / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture-auth')
        return path

    def test_selected_state_overrides_only_provider_home_without_mutating_parent(self):
        for provider in homes.PROVIDERS:
            with self.subTest(provider=provider):
                variable = homes.PROVIDERS[provider][0]
                original = self.variables.copy()
                env = self.environment(provider)
                self.assertEqual(env[variable], str(self.state / f'automation-{provider}'))
                self.assertEqual(sum(key.upper() == variable for key in env), 1)
                self.assertNotIn('TELEGRAM_TOKEN', env)
                self.assertEqual(self.variables, original)
                self.assertNotEqual(env[variable], self.environment(provider, self.root / 'other-state')[variable])
                self.assertFalse(self.state.exists())  # 환경 선택만으로 파일을 만들지 않는다.

    def test_initialization_links_only_auth_and_preserves_atomic_source_replacement(self):
        for provider in homes.PROVIDERS:
            with self.subTest(provider=provider):
                source = self.source(provider)
                (source.parent / 'config.toml').write_text('hooks=["personal"]')
                env = self.environment(provider)
                homes.prepare_authentication(provider, env)
                home = Path(env[homes.PROVIDERS[provider][0]])
                auth = home / source.name
                self.assertTrue(auth.is_symlink())
                self.assertEqual(auth.readlink(), source)
                self.assertEqual(list(home.iterdir()), [auth])
                links = homes.authentication_links(provider, env)
                replacement = source.with_name('replacement')
                replacement.write_text('renewed')
                replacement.replace(source)
                homes.assert_authentication_links(provider, env, links)
                homes.prepare_authentication(provider, env)
                self.assertEqual(auth.read_text(), 'renewed')

    def test_existing_auth_is_preserved_and_missing_source_makes_no_empty_file(self):
        env = self.environment()
        homes.prepare_authentication('codex', env)
        auth = Path(env['CODEX_HOME']) / 'auth.json'
        self.assertFalse(auth.exists())
        auth.write_text('existing-dedicated')
        self.source()
        homes.prepare_authentication('codex', env)
        self.assertFalse(auth.is_symlink())
        self.assertEqual(auth.read_text(), 'existing-dedicated')

    def test_dangling_link_is_preserved_and_diagnosed(self):
        env = self.environment()
        homes.prepare_authentication('codex', env)
        auth = Path(env['CODEX_HOME']) / 'auth.json'
        target = self.root / 'missing'
        auth.symlink_to(target)
        self.source()
        with self.assertRaisesRegex(RuntimeError, '전용 인증 경로 준비 실패'):
            homes.prepare_authentication('codex', env)
        self.assertEqual(auth.readlink(), target)

    def test_link_permission_failure_never_copies_credentials(self):
        source = self.source()
        env = self.environment()
        with mock.patch.object(Path, 'symlink_to', side_effect=PermissionError('link denied')):
            with self.assertRaisesRegex(RuntimeError, 'codex login'):
                homes.prepare_authentication('codex', env)
        self.assertFalse((Path(env['CODEX_HOME']) / 'auth.json').exists())
        self.assertEqual(source.read_text(), 'fixture-auth')

    def test_dedicated_home_cannot_alias_personal_settings_directory(self):
        boundary, state, _ = native_agent_boundary(self.root / 'alias')
        source = self.source()
        env = self.environment(state=state)
        Path(env['CODEX_HOME']).symlink_to(source.parent, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, '전용 인증 경로 준비 실패'):
            sandbox.prepare_launch(boundary, state, 'worker', False, (), env)
        self.assertFalse((state / 'agent-runtime').exists())

    def test_link_replacement_by_cli_is_rejected_without_repair(self):
        self.source()
        env = self.environment()
        homes.prepare_authentication('codex', env)
        links = homes.authentication_links('codex', env)
        auth = Path(env['CODEX_HOME']) / 'auth.json'
        auth.unlink()
        auth.write_text('cli-replacement')
        with self.assertRaisesRegex(RuntimeError, '인증 공유 확인 실패'):
            homes.assert_authentication_links('codex', env, links)
        self.assertEqual(auth.read_text(), 'cli-replacement')

    def test_auth_check_and_model_lookup_share_dedicated_environment(self):
        for provider in homes.PROVIDERS:
            with self.subTest(provider=provider):
                self.source(provider)
                variable = homes.PROVIDERS[provider][0]
                observed = []
                def catalog(*args):
                    observed.append(profiles.agent_environment(provider))
                    return {}
                effects = mock.Mock()
                effects.command.return_value = subprocess.CompletedProcess([], 0, '')
                effects.codex_model_efforts.side_effect = catalog
                effects.claude_model_efforts.side_effect = catalog
                with mock.patch.dict(os.environ, self.variables), \
                     mock.patch.object(profiles.shutil, 'which', return_value='cli'), \
                     mock.patch.object(profiles, 'validate_provider_profiles'):
                    profiles.validate_provider_runtime(self.root / 'config', provider,
                        effects=effects, state_dir=self.state)
                self.assertEqual(effects.command.call_args.kwargs['env'][variable],
                                 str(self.state / f'automation-{provider}'))
                self.assertEqual(observed[0][variable], effects.command.call_args.kwargs['env'][variable])

    def test_auth_failure_reports_the_selected_path_and_login_command(self):
        effects = mock.Mock()
        effects.command.return_value = subprocess.CompletedProcess([], 1, '')
        with mock.patch.dict(os.environ, self.variables), \
             mock.patch.object(profiles.shutil, 'which', return_value='cli'):
            with self.assertRaisesRegex(RuntimeError, 'automation-claude.*claude auth login'):
                profiles.validate_provider_runtime(self.root / 'config', 'claude',
                    effects=effects, state_dir=self.state)
        effects.claude_model_efforts.assert_not_called()

    def test_runtime_reuses_identity_and_protects_default_auth_without_personal_config(self):
        boundary, state, _ = native_agent_boundary(self.root / 'boundary')
        source = self.source()
        env = self.environment(state=state)
        first = sandbox.prepare_launch(boundary, state, 'worker', False, (), env)
        self.addCleanup(first.attempt.rmdir)
        second = sandbox.prepare_launch(boundary, state, 'worker', False, (), env)
        self.addCleanup(second.attempt.rmdir)
        self.assertEqual(first.runtime, second.runtime)
        home = Path(first.environment['CODEX_HOME'])
        self.assertEqual((home / 'auth.json').resolve(), source)
        self.assertFalse((home / 'config.toml').exists())
        import json
        self.assertIn(str(source), json.loads(first.specification)['protected'])
        if os.name == 'nt':
            self.assertEqual(home, state / 'automation-codex')
        else:
            self.assertEqual(home, first.runtime / 'codex')

    @unittest.skipIf(os.name == 'nt', 'Windows에는 역할별 config 연결이 없다')
    def test_known_personal_runtime_config_is_detached_without_losing_session(self):
        boundary, state, _ = native_agent_boundary(self.root / 'migration')
        source = self.source()
        personal_config = source.parent / 'config.toml'
        personal_config.write_text('hooks=["personal"]')
        old = sandbox.prepare_launch(boundary, state, 'worker', False, (),
                                     dict(self.variables, CODEX_HOME=str(source.parent)))
        self.addCleanup(old.attempt.rmdir)
        home = Path(old.environment['CODEX_HOME'])
        session = home / 'session-fixture'
        session.write_text('keep')
        new = sandbox.prepare_launch(boundary, state, 'worker', False, (), self.environment(state=state))
        self.addCleanup(new.attempt.rmdir)
        self.assertEqual(new.runtime, old.runtime)
        self.assertFalse((home / 'config.toml').exists())
        self.assertFalse((home / 'config.toml').is_symlink())
        self.assertEqual(personal_config.read_text(), 'hooks=["personal"]')
        self.assertEqual(session.read_text(), 'keep')
        self.assertEqual((home / 'auth.json').resolve(), source)

    def test_fingerprint_and_extension_checks_use_selected_config(self):
        env = self.environment()
        homes.prepare_authentication('codex', env)
        worktree = self.root / 'project'
        worktree.mkdir()
        personal = self.user / '.codex/config.toml'
        personal.parent.mkdir(parents=True)
        personal.write_text('hooks=["personal"]')
        isolation._assert_no_codex_extensions(worktree, env)
        before = isolation._configuration_digest('codex', worktree, None, env)
        config = Path(env['CODEX_HOME']) / 'config.toml'
        config.write_text('hooks=["dedicated"]')
        self.assertNotEqual(before, isolation._configuration_digest('codex', worktree, None, env))
        with self.assertRaises(isolation.IsolationError):
            isolation._assert_no_codex_extensions(worktree, env)

    def test_auth_path_changes_affect_proof_without_hashing_auth_contents(self):
        source = self.source()
        env = self.environment()
        homes.prepare_authentication('codex', env)
        original = isolation._configuration_digest('codex', self.root, None, env)
        source.write_text('renewed-secret')
        self.assertEqual(original, isolation._configuration_digest('codex', self.root, None, env))
        other = source.with_name('other-auth')
        other.write_text('other-secret')
        auth = Path(env['CODEX_HOME']) / 'auth.json'
        auth.unlink()
        auth.symlink_to(other)
        self.assertNotEqual(original, isolation._configuration_digest('codex', self.root, None, env))

    def test_source_execpolicy_is_checked_even_when_runtime_has_no_rules(self):
        boundary, state, _ = native_agent_boundary(self.root / 'rules')
        env = self.environment(state=state)
        rules = Path(env['CODEX_HOME']) / 'rules'
        rules.mkdir(parents=True, mode=0o700)
        rules.parent.chmod(0o700)
        (rules / 'unsafe.rules').write_text('fixture-rule')
        with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary):
            with self.assertRaisesRegex(isolation.IsolationError, 'exec-policy'):
                isolation.prepare_isolation('codex', 'worker', boundary.worktree, state, False,
                    snapshot_digest=lambda: 'snapshot', expected_repository=str(boundary.source),
                    environment_variables=env)

    def test_registration_prerequisites_forward_actual_state_root(self):
        from loop_manager_internal.runtime import registration_service
        effects = mock.Mock()
        with mock.patch.object(registration_service.shutil, 'which', return_value='cli'):
            registration_service.validate_runtime_prerequisites(effects, self.root,
                provider='claude', config_path=self.root / 'config', state_dir=self.state)
        effects.validate_provider_runtime.assert_called_once_with(
            self.root / 'config', 'claude', self.root, state_dir=self.state)

    @unittest.skipIf(os.name == 'nt', 'Windows는 전용 홈을 직접 사용한다')
    def test_path_scoped_credential_store_cannot_claim_shared_runtime_auth(self):
        env = self.environment()
        homes.prepare_authentication('codex', env)
        (Path(env['CODEX_HOME']) / 'config.toml').write_text('cli_auth_credentials_store="keyring"')
        with self.assertRaisesRegex(isolation.IsolationError, '파일 외 인증 저장소'):
            isolation._assert_no_codex_extensions(self.root, env)


if __name__ == '__main__':
    unittest.main()
