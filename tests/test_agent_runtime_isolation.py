"""Read-only Agent invocation from the detached Manager runtime."""

import sqlite3
from pathlib import Path
import tempfile
import json
from unittest import TestCase, mock, skipUnless

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.agents import agent_write_isolation as isolation
from loop_manager_internal.agents import agent_codex_sandbox as envelope
from loop_manager_internal.storage import agent_invocation_state as state
import loop_manager as manager
from tests.support.workspace import native_agent_boundary


@skipUnless(isolation.platform.system() == 'Linux', 'native tool policy probe requires Linux')
class RuntimeReadOnlyIsolationTests(TestCase):
    def fixture(self, root):
        boundary, state_root, home = native_agent_boundary(root)
        head = 'a' * 40
        runtime = manager.expected_runtime_worktree(state_root, boundary.source, head)
        runtime.parent.mkdir(parents=True)
        boundary.worktree.rename(runtime)
        observed = {'selected': runtime, 'head': head, 'branch': 'HEAD', 'dirty': '',
                    'registered': True}

        def git(path, *args):
            if args == ('rev-parse', '--show-toplevel'):
                return str(observed['selected'])
            if args == ('rev-parse', '--path-format=absolute', '--git-common-dir'):
                return str(boundary.common_dir)
            if args == ('rev-parse', '--path-format=absolute', '--absolute-git-dir'):
                return str(boundary.git_dir)
            if args == ('worktree', 'list', '--porcelain'):
                selected = (f"worktree {observed['selected']}\nHEAD {observed['head']}\ndetached\n"
                            if observed['registered'] else '')
                return f'worktree {boundary.source}\nHEAD base\n\n' + selected
            if args == ('rev-parse', 'HEAD'):
                return observed['head']
            if args == ('rev-parse', '--abbrev-ref', 'HEAD'):
                return observed['branch']
            if args == ('status', '--porcelain=v1', '--untracked-files=all'):
                return observed['dirty']
            if args == ('remote', 'get-url', 'origin'):
                return 'https://example.invalid/owner/repo.git'
            raise AssertionError(args)

        return boundary, state_root, home, observed, git

    def test_runtime_is_allowed_only_for_read_only_and_remains_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home, observed, git = self.fixture(Path(directory))
            runtime = observed['selected']
            with mock.patch.object(isolation, '_git', side_effect=git):
                inspected = isolation.inspect_worktree(runtime, state_root, read_only=True)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(runtime, state_root, read_only=False)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(boundary.source, state_root, read_only=True)
                alias = runtime.parent / 'alias'
                alias.symlink_to(runtime, target_is_directory=True)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(alias, state_root, read_only=True)
            launch = envelope.prepare_launch(inspected, state_root, 'planner', True,
                                             isolation.codex_arguments(True), {'CODEX_HOME': str(home)})
            specification = json.loads(launch.specification)
            self.assertIn(str(runtime), specification['protected'])
            self.assertNotIn(str(runtime), specification['writable'])
            self.assertEqual(inspected.source, boundary.source)

    def test_runtime_rejects_changed_head_attached_dirty_and_unregistered_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            _boundary, state_root, _home, observed, git = self.fixture(Path(directory))
            runtime = observed['selected']
            with mock.patch.object(isolation, '_git', side_effect=git):
                for changes in ({'head': 'b' * 40}, {'branch': 'main'},
                                {'dirty': ' M code.py'}, {'registered': False}):
                    with self.subTest(changes=changes), mock.patch.dict(observed, changes):
                        with self.assertRaises(isolation.IsolationError):
                            isolation.inspect_worktree(runtime, state_root, read_only=True)
                invalid = state_root / 'runtimes' / 'wrong-repository' / observed['head']
                invalid.parent.mkdir()
                runtime.rename(invalid)
                observed['selected'] = invalid
                with self.assertRaisesRegex(isolation.IsolationError, 'exact SHA'):
                    isolation.inspect_worktree(invalid, state_root, read_only=True)

    def test_document_generate_reaches_isolated_agent_from_runtime_root(self):
        from loop_manager_internal.documents import document_drafts

        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home, observed, git = self.fixture(Path(directory))
            runtime = observed['selected']
            connection = sqlite3.connect(':memory:')
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            state.ensure_schema(connection)
            connection.execute('CREATE TABLE projects (repo TEXT, base_branch TEXT, provider TEXT, registered INT)')
            connection.execute('INSERT INTO projects VALUES (?, ?, ?, 1)',
                               (str(boundary.source), 'main', 'codex'))
            connection.commit()
            drafts = mock.Mock()
            drafts.generation_prompt.return_value = ('draft prompt', 'document identity')
            launched = []

            def run(command, **kwargs):
                if command == ['codex', '--version']:
                    return manager.subprocess.CompletedProcess(command, 0, 'codex-cli 0.159.3', '')
                if tuple(command[-2:]) == ('features', 'list'):
                    return manager.subprocess.CompletedProcess(command, 0, 'plugins stable false\napps stable false\n', '')
                return manager.subprocess.CompletedProcess(command, 0, 'agent-tool-boundary-v2\n', '')

            def execute(command, **kwargs):
                launched.append(command)
                self.assertEqual(command[:2], ['codex', 'exec'])
                self.assertEqual(command[command.index('-C') + 1], str(runtime))
                self.assertIn('--sandbox', command)
                self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
                Path(command[command.index('-o') + 1]).write_text('draft response')
                return manager.subprocess.CompletedProcess(command, 0)

            args = manager.argparse.Namespace(repository=str(boundary.source),
                                               draft_command='generate', target='docs/PRODUCT.md')
            with mock.patch.object(manager, 'ROOT', runtime), \
                 mock.patch.object(manager, 'is_loop_manager_repository', return_value=True), \
                 mock.patch.object(manager, 'connect_database', return_value=connection), \
                 mock.patch.object(manager, '_recorded_base_fetch'), \
                 mock.patch.object(document_drafts, 'DocumentDraftRepository', return_value=drafts), \
                 mock.patch.object(document_drafts, 'parse_generated_draft', return_value=None) as parse, \
                 mock.patch.object(manager, 'operation_print'), \
                 mock.patch.object(manager, '_agent_profile', return_value=invocation.ResolvedProfile('model', 'high')), \
                 mock.patch.object(manager, 'capture_git_snapshot', return_value=mock.Mock(serialize=lambda: 'snapshot')), \
                 mock.patch.object(isolation, '_git', side_effect=git), \
                 mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                 mock.patch.object(envelope.CodexLaunch, 'run', side_effect=execute):
                result = manager._document_drafts_command(args, state_root, state_root / 'config')
            self.assertEqual(result, 0)
            self.assertEqual(len(launched), 1)
            drafts.generation_prompt.assert_called_once_with(str(boundary.source), boundary.source,
                                                             'main', 'docs/PRODUCT.md')
            parse.assert_called_once_with('draft response')
            row = connection.execute('''SELECT proof.status, proof.canonical_worktree, call.result_kind
                FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
            self.assertEqual(tuple(row), ('verified', str(runtime), 'success'))
