"""Direct Codex invocation, native policy and runtime handoff contracts."""

import sqlite3
from pathlib import Path
import tempfile
import io
import json
import subprocess
import threading
from unittest import TestCase, mock, skipUnless

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.agents import agent_write_isolation as isolation
from loop_manager_internal.agents import agent_codex_sandbox as envelope
from loop_manager_internal.agents import agent_sandbox_exec as gate
from loop_manager_internal.storage import agent_invocation_state as state
import loop_manager as manager
from tests.support.workspace import native_agent_boundary


@skipUnless(isolation.platform.system() == "Linux", "native tool policy probe requires Linux")
class NativeWriteIsolationTests(TestCase):
    def boundary(self, root):
        return native_agent_boundary(root)

    def test_direct_launch_preserves_sessions_and_excludes_runtime_from_tool_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home = self.boundary(Path(directory))
            environment = {'CODEX_HOME': str(home), 'PATH': '/bin'}
            launch = envelope.prepare_launch(boundary, state_root, 'worker', False,
                                             isolation.codex_arguments(False), environment)
            self.assertEqual(environment['CODEX_HOME'], str(home))
            scoped_home = Path(launch.environment['CODEX_HOME'])
            self.assertTrue((scoped_home / 'auth.json').is_symlink())
            self.assertEqual((scoped_home / 'auth.json').resolve(), home / 'auth.json')
            policy = json.loads(launch.specification)
            self.assertEqual(set(policy['writable']),
                             {str(launch.attempt), str(boundary.worktree)})
            self.assertIn(str(home), policy['protected'])
            self.assertIn(str(boundary.common_dir), policy['protected'])
            self.assertIn(str(boundary.other_worktrees[0]), policy['protected'])
            self.assertIn(str(boundary.worktree / '.git'), policy['protected'])
            self.assertEqual(launch.probe_arguments[:2], ('codex', 'sandbox'))
            probe_script = Path(launch.probe_arguments[launch.probe_arguments.index('-I') + 1])
            self.assertEqual(probe_script, Path(gate.__file__))
            self.assertEqual(probe_script.parent, Path(envelope.__file__).parent)
            self.assertTrue(probe_script.is_file())
            self.assertIn('--include-managed-config', launch.probe_arguments)
            for args in (launch.probe_arguments, launch.arguments):
                self.assertIn('features.plugins=false', args)
                self.assertIn('features.apps=false', args)
            self.assertEqual(launch.probe_arguments[launch.probe_arguments.index('-C') + 1],
                             str(boundary.worktree))
            self.assertNotIn('exec', launch.probe_arguments)
            self.assertNotIn('app-server', launch.probe_arguments)
            import tomllib
            profile = next(arg for arg in launch.probe_arguments
                           if arg.startswith('permissions.loop-manager-tools.filesystem='))
            filesystem = tomllib.loads(profile)['permissions']['loop-manager-tools']['filesystem']
            self.assertEqual(filesystem[':root'], 'read')
            self.assertEqual({path for path, access in filesystem.items() if access == 'write'},
                             set(policy['writable']))
            self.assertTrue(all(filesystem[path] == 'read' for path in policy['protected']))
            self.assertIn('permissions.loop-manager-tools.network.enabled=false', launch.probe_arguments)
            self.assertIn('sandbox_workspace_write.network_access=false', launch.arguments)
            second = envelope.prepare_launch(boundary, state_root, 'worker', False,
                                              launch.arguments, environment)
            self.assertEqual(launch.runtime, second.runtime)
            self.assertNotEqual(launch.attempt, second.attempt)
            reviewer = envelope.prepare_launch(boundary, state_root, 'reviewer', True,
                                                isolation.codex_arguments(True), environment)
            self.assertNotEqual(launch.runtime, reviewer.runtime)
            self.assertNotIn(str(boundary.worktree), json.loads(reviewer.specification)['writable'])
            self.assertIn(str(boundary.worktree), json.loads(reviewer.specification)['protected'])

    def test_runtime_alias_and_replaced_auth_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary, state_root, home = self.boundary(root)
            environment = {'CODEX_HOME': str(home)}
            launch = envelope.prepare_launch(boundary, state_root, 'worker', False, (), environment)
            auth = Path(launch.environment['CODEX_HOME']) / 'auth.json'
            auth.unlink()
            auth.write_text('replaced')
            with self.assertRaisesRegex(RuntimeError, '읽기 전용 연결'):
                envelope.prepare_launch(boundary, state_root, 'worker', False, (), environment)
            alias = root / 'alias'
            alias.symlink_to(launch.runtime, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, '소유권'):
                envelope._owned_directory(alias)

    def test_runtime_overlap_is_rejected_before_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, _state_root, home = self.boundary(Path(directory))
            before = set(boundary.source.iterdir())
            with self.assertRaisesRegex(RuntimeError, '보호 경로가 겹칩니다'):
                envelope.prepare_launch(boundary, boundary.source, 'worker', False, (),
                                        {'CODEX_HOME': str(home)})
            self.assertEqual(set(boundary.source.iterdir()), before)
            self.assertFalse((boundary.source / 'agent-runtime').exists())

    def test_failed_proof_and_unknown_probe_output_do_not_verify(self):
        launch = envelope.CodexLaunch((), ('codex', 'sandbox'), {}, Path('/runtime'),
                                     Path('/attempt'), '{"worktree":"/selected"}')
        for code, output in ((1, ''), (0, '{}'), (0, 'agent-tool-boundary-v2 extra')):
            with self.subTest(code=code, output=output), \
                 mock.patch.object(envelope.subprocess, 'run', side_effect=(
                                   manager.subprocess.CompletedProcess([], 0, 'plugins stable false\napps stable false\n', ''),
                                   manager.subprocess.CompletedProcess([], code, output, ''))):
                with self.assertRaisesRegex(RuntimeError, '확인할 수 없습니다'):
                    launch.probe()

    def test_effective_extensions_must_be_disabled_before_native_probe(self):
        launch = envelope.CodexLaunch((), ('codex', 'sandbox'), {'CODEX_HOME': '/owned'},
                                     Path('/runtime'), Path('/attempt'), '{"worktree":"/selected"}')
        for output in ('plugins stable true\napps stable false\n',
                       'plugins stable false\n',
                       'plugins stable false\nplugins stable false\napps stable false\n'):
            with self.subTest(output=output), mock.patch.object(envelope.subprocess, 'run',
                    return_value=manager.subprocess.CompletedProcess([], 0, output, '')) as run:
                with self.assertRaisesRegex(RuntimeError, '확장 도구'):
                    launch.probe()
                self.assertEqual(run.call_count, 1)
        with mock.patch.object(envelope.subprocess, 'run', side_effect=(
                manager.subprocess.CompletedProcess([], 0, 'plugins stable false\napps stable false\n', ''),
                manager.subprocess.CompletedProcess([], 0, 'agent-tool-boundary-v2\n', ''))) as run:
            launch.probe()
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs['cwd'], '/selected')
                self.assertEqual(call.kwargs['env'], launch.environment)

    def test_probe_never_executes_provider_and_fails_closed(self):
        specification = json.dumps({'writable': [], 'protected': [], 'files': []})
        with mock.patch.object(gate.sys, 'argv', ['probe', specification]), \
             mock.patch.object(gate, 'check_boundary') as check, \
             mock.patch.object(gate.sys, 'stdout', io.StringIO()):
            self.assertEqual(gate.main(), 0)
            check.assert_called_once()
        with mock.patch.object(gate.sys, 'argv', ['probe', specification, 'codex', 'exec']), \
             mock.patch.object(gate, 'check_boundary') as check, \
             mock.patch.object(gate.sys, 'stderr', io.StringIO()):
            self.assertEqual(gate.main(), 125)
            check.assert_not_called()

    def test_protected_file_write_is_checked_without_truncation(self):
        import errno
        spec = {'writable': [], 'protected': [], 'files': ['/source/HEAD']}
        for denied in (errno.EROFS, errno.EACCES, errno.EPERM):
            with self.subTest(denied=denied), \
                 mock.patch.object(gate.os, 'open', side_effect=OSError(denied, 'denied')) as opened:
                gate.check_boundary(spec)
                opened.assert_called_once_with('/source/HEAD', gate.os.O_WRONLY | gate.os.O_CLOEXEC)
        with mock.patch.object(gate.os, 'open', return_value=17), \
             mock.patch.object(gate.os, 'close') as closed, \
             self.assertRaisesRegex(gate.BoundaryError, 'write was allowed'):
            gate.check_boundary(spec)
        closed.assert_called_once_with(17)
        with mock.patch.object(gate.os, 'open', side_effect=FileNotFoundError), \
             self.assertRaisesRegex(gate.BoundaryError, 'inconclusive'):
            gate.check_boundary(spec)

    def test_source_readonly_failure_never_attempts_mutation(self):
        specification = {'writable': [], 'protected': ['/source'], 'files': ['/source/file']}
        with mock.patch.object(gate.Path, 'exists', return_value=True), \
             mock.patch.object(gate.os, 'statvfs', return_value=mock.Mock(f_flag=0)), \
             mock.patch.object(gate.os, 'open') as write_open, \
             mock.patch.object(gate.tempfile, 'TemporaryDirectory') as scratch:
            with self.assertRaisesRegex(gate.BoundaryError, 'not read-only'):
                gate.check_boundary(specification)
            write_open.assert_not_called()
            scratch.assert_not_called()

    def test_writable_probe_creates_updates_renames_and_removes_only_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            selected = root / 'selected'
            source.mkdir()
            selected.mkdir()
            preserved = source / 'local-only'
            preserved.write_bytes(b'unchanged')
            specification = {'writable': [str(selected)], 'protected': [str(source)], 'files': []}
            with mock.patch.object(gate.os, 'statvfs', return_value=mock.Mock(f_flag=gate.os.ST_RDONLY)), \
                 mock.patch.object(gate.os, 'walk', side_effect=PermissionError('tree inspection unavailable')) as walk:
                gate.check_boundary(specification)
            walk.assert_not_called()
            self.assertEqual(list(selected.iterdir()), [])
            self.assertEqual(preserved.read_bytes(), b'unchanged')
            self.assertEqual(list(source.iterdir()), [preserved])

    def test_actual_adapter_runs_direct_roles_and_resume_and_persists_evidence(self):
        cases = ((role, role == 'reviewer', session)
                 for role in ('planner', 'worker', 'reviewer', 'maintenance')
                 for session in (None, 'saved'))
        for role, read_only, session in cases:
            with self.subTest(role=role, session=session), tempfile.TemporaryDirectory() as directory:
                boundary, state_root, home = self.boundary(Path(directory))
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                state.ensure_schema(connection)
                commands = []

                def run(command, **kwargs):
                    if command == ['codex', '--version']:
                        return manager.subprocess.CompletedProcess(command, 0, 'codex-cli 0.159.3', '')
                    if tuple(command[-2:]) == ('features', 'list'):
                        return manager.subprocess.CompletedProcess(command, 0, 'plugins stable false\napps stable false\n', '')
                    self.assertEqual(list(command[:2]), ['codex', 'sandbox'] if 'exec' not in command
                                     else ['codex', 'exec'])
                    commands.append(command)
                    if 'exec' not in command:
                        return manager.subprocess.CompletedProcess(command, 0, 'agent-tool-boundary-v2\n', '')
                    self.assertIn('approvals_reviewer="auto_review"', command)
                    self.assertIn('sandbox_workspace_write.network_access=false', command)
                    if session:
                        self.assertIn('resume', command)
                        self.assertIn(session, command)
                    if role == 'reviewer':
                        self.assertNotIn('-o', command)
                        self.assertEqual(kwargs['stdout'], subprocess.PIPE)
                        return manager.subprocess.CompletedProcess(
                            command, 0,
                            '{"type":"item.completed","item":{"type":"agent_message",'
                            '"text":"completed"}}\n',
                        )
                    target = Path(command[command.index('-o') + 1])
                    target.write_text('completed')
                    kwargs['stdout'].write('provider log\n')
                    return manager.subprocess.CompletedProcess(command, 0)

                with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                     mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                     mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                     mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                       mock.Mock(serialize=lambda: 'snapshot')), \
                     mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                     mock.patch.object(envelope.CodexLaunch, 'run', side_effect=run):
                    result = manager.run_agent(
                        'codex', boundary.worktree, state_root, state_root / 'config', role, 'prompt',
                        read_only=read_only, session_id=session, connection=connection,
                        resolved_profile=invocation.ResolvedProfile('model', 'high'),
                        invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
                self.assertEqual(result.code, 0)
                self.assertEqual(result.message, 'completed')
                self.assertEqual(len(commands), 2)
                self.assertEqual(list(commands[0][:2]), ['codex', 'sandbox'])
                self.assertEqual(commands[1][:2], ['codex', 'exec'])
                self.assertNotIn('sandbox', commands[1])
                self.assertNotIn('app-server', commands[1])
                self.assertEqual(commands[1][commands[1].index('-C') + 1], str(boundary.worktree))
                row = connection.execute('''SELECT proof.status, call.result_kind, call.session_id
                    FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
                self.assertEqual(row, ('verified', 'success', session))

    def test_session_unavailable_fallback_rechecks_direct_policy_without_losing_history(self):
        for role in ('planner', 'worker', 'maintenance', 'reviewer'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                boundary, state_root, home = self.boundary(Path(directory))
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                state.ensure_schema(connection)
                product = boundary.worktree / 'work-product.txt'
                product.write_text('saved work product')
                calls = []

                def run(command, **kwargs):
                    if command == ['codex', '--version']:
                        return manager.subprocess.CompletedProcess(command, 0, 'codex-cli 0.159.3', '')
                    if tuple(command[-2:]) == ('features', 'list'):
                        return manager.subprocess.CompletedProcess(command, 0,
                                                                  'plugins stable false\napps stable false\n', '')
                    if command[:2] != ['codex', 'exec']:
                        self.assertEqual(command[:2], ('codex', 'sandbox'))
                        return manager.subprocess.CompletedProcess(command, 0, 'agent-tool-boundary-v2', '')
                    calls.append(command)
                    self.assertIn('approvals_reviewer="auto_review"', command)
                    self.assertEqual(command[command.index('--sandbox') + 1],
                                     'read-only' if role == 'reviewer' else 'workspace-write')
                    self.assertNotIn('--approve-for-me', command)
                    if 'resume' in command:
                        if role == 'reviewer':
                            return manager.subprocess.CompletedProcess(
                                command, 1, 'thread saved-session not found\n',
                            )
                        kwargs['stdout'].write('thread saved-session not found\n')
                        return manager.subprocess.CompletedProcess(command, 1)
                    if role == 'reviewer':
                        self.assertNotIn('-o', command)
                        return manager.subprocess.CompletedProcess(
                            command, 0,
                            '{"type":"item.completed","item":{"type":"agent_message",'
                            '"text":"completed"}}\n',
                        )
                    Path(command[command.index('-o') + 1]).write_text('completed')
                    return manager.subprocess.CompletedProcess(command, 0)

                attempts = invocation.AttemptSequence(str(boundary.source), 'G-097', 'revision')
                invoke = invocation.TrackedAgentCall(
                    lambda session, fallback, context: manager.run_agent(
                        'codex', boundary.worktree, state_root, state_root / 'config', role, 'prompt',
                        read_only=role == 'reviewer', session_id=session, connection=connection,
                        resolved_profile=invocation.ResolvedProfile('model', 'high'), invocation=context),
                    attempts)
                with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                     mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                     mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                     mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                       mock.Mock(serialize=lambda: 'snapshot')), \
                     mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                     mock.patch.object(envelope.CodexLaunch, 'run', side_effect=run):
                    result = manager.run_agent_with_retries(invoke, session_id='saved-session')
                self.assertEqual(result.code, 0)
                self.assertEqual(len(calls), 2)
                rows = connection.execute('''SELECT call.session_id, call.transition, proof.status,
                    proof.policy_digest FROM agent_invocations call JOIN agent_isolation_evidence proof
                    USING(invocation_id) ORDER BY call.attempt''').fetchall()
                self.assertEqual([row[:3] for row in rows],
                                 [('saved-session', 'resume', 'invalidated'), (None, 'fallback', 'verified')])
                self.assertNotEqual(rows[0][3], rows[1][3])
                self.assertEqual(product.read_text(), 'saved work product')

    def test_handoff_after_preflight_blocks_native_spawn_and_invalidates_evidence(self):
        cases = (('reviewer', True, False, None),
                 ('worker', False, True, 'saved-session'),
                 ('worker', False, True, None))
        for role, read_only, persist_session, session in cases:
            with self.subTest(role=role, session=session), tempfile.TemporaryDirectory() as directory:
                boundary, state_root, home = self.boundary(Path(directory))
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                state.ensure_schema(connection)
                logs = state_root / 'logs' / manager.hashlib.sha256(
                    str(boundary.worktree).encode()).hexdigest()[:16]
                logs.mkdir(parents=True)
                (logs / f'{role}.log').write_text('old log')
                (logs / f'{role}-last.txt').write_text('old result')
                handoff = manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
                token = manager._runtime_handoff_signal.set(handoff)
                try:
                    with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                         mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                         mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                         mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                           mock.Mock(serialize=lambda: 'snapshot')), \
                         mock.patch.object(isolation.subprocess, 'run', return_value=
                                           manager.subprocess.CompletedProcess([], 0, 'codex-cli 0.159.3', '')), \
                         mock.patch.object(envelope.CodexLaunch, 'probe', side_effect=handoff.stop) as probe, \
                         mock.patch.object(manager.subprocess, 'Popen') as spawn, \
                         mock.patch.object(envelope.os, 'killpg') as terminate:
                        with self.assertRaises(manager.SelfMergeRuntimeHandoff):
                            manager.run_agent(
                                'codex', boundary.worktree, state_root, state_root / 'config',
                                role, 'prompt', read_only=read_only, persist_session=persist_session,
                                session_id=session, connection=connection,
                                resolved_profile=invocation.ResolvedProfile('model', 'high'),
                                invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
                        probe.assert_called_once()
                        spawn.assert_not_called()
                        terminate.assert_not_called()
                finally:
                    manager._runtime_handoff_signal.reset(token)
                proof = connection.execute('''SELECT proof.status, call.session_id, call.finished_at
                    FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
                self.assertEqual(proof[:2], ('invalidated', session))
                self.assertIsNotNone(proof[2])
                self.assertEqual((logs / f'{role}.log').read_text(), 'old log')
                self.assertEqual((logs / f'{role}-last.txt').read_text(), 'old result')

    def test_native_spawn_holds_handoff_lock_only_until_process_starts(self):
        for role, read_only, session in (('reviewer', True, None),
                                        ('worker', False, 'saved-session')):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                boundary, state_root, home = self.boundary(Path(directory))
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                state.ensure_schema(connection)
                handoff = manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
                process = mock.Mock(pid=4321, returncode=0)

                def start(command, **kwargs):
                    self.assertTrue(handoff.launch_lock.locked())
                    self.assertEqual(command[:2], ['codex', 'exec'])
                    self.assertTrue(kwargs['start_new_session'])
                    if session:
                        self.assertIn('resume', command)
                        self.assertIn(session, command)

                    def communicate(prompt, *, timeout):
                        self.assertEqual(prompt, 'prompt')
                        self.assertEqual(timeout, manager.AGENT_CLI_TIMEOUT_SECONDS)
                        self.assertTrue(handoff.launch_lock.acquire(blocking=False))
                        handoff.launch_lock.release()
                        handoff.stop()
                        if role == 'reviewer':
                            self.assertNotIn('-o', command)
                            return ('{"type":"item.completed","item":{"type":"agent_message",'
                                    '"text":"completed"}}\n', None)
                        Path(command[command.index('-o') + 1]).write_text('completed')
                        kwargs['stdout'].write('provider log\n')
                        return (None, None)

                    process.communicate.side_effect = communicate
                    return process

                token = manager._runtime_handoff_signal.set(handoff)
                try:
                    with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                         mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                         mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                         mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                           mock.Mock(serialize=lambda: 'snapshot')), \
                         mock.patch.object(isolation.subprocess, 'run', return_value=
                                           manager.subprocess.CompletedProcess([], 0, 'codex-cli 0.159.3', '')), \
                         mock.patch.object(envelope.CodexLaunch, 'probe'), \
                         mock.patch.object(manager.subprocess, 'Popen', side_effect=start) as spawn, \
                         mock.patch.object(envelope.os, 'killpg') as terminate:
                        result = manager.run_agent(
                            'codex', boundary.worktree, state_root, state_root / 'config', role, 'prompt',
                            read_only=read_only, persist_session=session is not None, session_id=session,
                            connection=connection,
                            resolved_profile=invocation.ResolvedProfile('model', 'high'),
                            invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
                        spawn.assert_called_once()
                        terminate.assert_called_once_with(4321, envelope.signal.SIGKILL)
                finally:
                    manager._runtime_handoff_signal.reset(token)
                process.communicate.assert_called_once()
                process.wait.assert_called_once()
                self.assertEqual((result.code, result.message, result.session_id), (0, 'completed', session))
                proof = connection.execute('''SELECT proof.status, call.result_kind, call.session_id
                    FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
                self.assertEqual(proof, ('verified', 'success', session))

    def test_envelope_timeout_kills_process_group_and_waits(self):
        launch = envelope.CodexLaunch((), ('native',), {}, Path('/runtime'), Path('/attempt'), '{}')
        process = mock.Mock(pid=4321)
        process.communicate.side_effect = manager.subprocess.TimeoutExpired(['native'], 1)
        with mock.patch.object(envelope.subprocess, 'Popen', return_value=process) as start, \
             mock.patch.object(envelope.os, 'killpg') as terminate:
            with self.assertRaises(manager.subprocess.TimeoutExpired):
                launch.run(['native'], launch_process=manager._handoff_popen,
                           input='prompt', timeout=1, text=True)
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        terminate.assert_called_once_with(4321, envelope.signal.SIGKILL)
        process.wait.assert_called_once()

    def test_streamed_new_session_uses_direct_exec_and_persists_session(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home = self.boundary(Path(directory))
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            state.ensure_schema(connection)
            process = mock.Mock(pid=4321)
            process.stdin = io.StringIO()
            process.stdout = io.StringIO('{"type":"thread.started","thread_id":"new-session"}\n')
            process.wait.return_value = 0
            commands = []

            def run(command, **kwargs):
                if command == ['codex', '--version']:
                    return manager.subprocess.CompletedProcess(command, 0, 'codex-cli 0.159.3', '')
                if tuple(command[-2:]) == ('features', 'list'):
                    return manager.subprocess.CompletedProcess(command, 0, 'plugins stable false\napps stable false\n', '')
                return manager.subprocess.CompletedProcess(command, 0, 'agent-tool-boundary-v2\n', '')

            def start(command, **kwargs):
                commands.append(command)
                self.assertTrue(kwargs['start_new_session'])
                Path(command[command.index('-o') + 1]).write_text('streamed result')
                return process

            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                   mock.Mock(serialize=lambda: 'snapshot')), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                 mock.patch.object(manager.subprocess, 'Popen', side_effect=start), \
                 mock.patch.object(envelope.os, 'killpg') as terminate:
                result = manager.run_agent(
                    'codex', boundary.worktree, state_root, state_root / 'config', 'worker', 'prompt',
                    read_only=False, persist_session=True, connection=connection,
                    resolved_profile=invocation.ResolvedProfile('model', 'high'),
                    invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
            self.assertEqual(commands[0][:2], ['codex', 'exec'])
            self.assertEqual((result.code, result.session_id), (0, 'new-session'))
            self.assertEqual(connection.execute('SELECT session_id FROM agent_invocations').fetchone()[0],
                             'new-session')
            terminate.assert_called_once_with(4321, envelope.signal.SIGKILL)

    def test_actual_gate_failure_invalidates_evidence_and_preserves_old_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home = self.boundary(Path(directory))
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            state.ensure_schema(connection)
            logs = state_root / 'logs' / manager.hashlib.sha256(str(boundary.worktree).encode()).hexdigest()[:16]
            logs.mkdir(parents=True)
            (logs / 'planner.log').write_text('old log')
            (logs / 'planner-last.txt').write_text('old result')

            def run(command, **kwargs):
                if command == ['codex', '--version']:
                    return manager.subprocess.CompletedProcess(command, 0, 'codex-cli 0.159.3', '')
                if tuple(command[-2:]) == ('features', 'list'):
                    return manager.subprocess.CompletedProcess(command, 0, 'plugins stable false\napps stable false\n', '')
                if 'exec' not in command:
                    return manager.subprocess.CompletedProcess(command, 0, 'agent-tool-boundary-v2\n', '')
                kwargs['stdout'].write('agent write boundary verification failed')
                return manager.subprocess.CompletedProcess(command, 125)

            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                   mock.Mock(serialize=lambda: 'snapshot')), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                     mock.patch.object(envelope.CodexLaunch, 'run', side_effect=run):
                result = manager.run_agent(
                    'codex', boundary.worktree, state_root, state_root / 'config', 'planner', 'prompt',
                    read_only=False, connection=connection,
                    resolved_profile=invocation.ResolvedProfile('model', 'high'),
                    invocation=invocation.InvocationContext(str(boundary.source), None))
            self.assertEqual(result.code, 125)
            self.assertEqual(connection.execute('SELECT status FROM agent_isolation_evidence').fetchone()[0],
                             'invalidated')
            self.assertEqual((logs / 'planner.log').read_text(), 'old log')
            self.assertEqual((logs / 'planner-last.txt').read_text(), 'old result')
