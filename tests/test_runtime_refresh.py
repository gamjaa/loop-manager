"""Runtime input classification and owned refresh protocol boundaries."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.application import runtime_inputs, self_runtime
from loop_manager_internal.runtime import refresh, operation_events as events
from loop_manager_internal.runtime.contracts import RuntimeRefresh, RuntimeMutation, RuntimeWorktree
from loop_manager_internal.runtime.handoff_gate import RuntimeHandoffGate, SelfMergeRuntimeHandoff, _runtime_handoff_signal, runtime_boundary
from loop_manager_internal.runtime.project_registry import RegisteredProject
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure


class RuntimeInputTests(unittest.TestCase):
    def test_prose_requires_regular_nonexecutable_document_and_noncode_bytes(self):
        for path, mode, content, expected in (
            ('docs/requirements/change.md', '100644', b'# Requirement\n\nThe next step.', True),
            ('docs/AGENTS.common.md', '100644', b'# Rules', False),
            ('docs/AGENTS.worker.md', '100644', b'# Rules', False),
            ('docs/AGENTS.ponytail.md', '100644', b'# Style', False),
            ('README.md', '100644', b'# Usage\n\nRun the program.', True),
            ('docs/example.py', '100644', b'print(1)', False),
            ('docs/guide.md', '100755', b'# Guide', False),
            ('docs/guide.md', '120000', b'../code.py', False),
            ('docs/guide.md', '100644', b'#!/bin/sh\necho hello', False),
            ('docs/guide.md', '100644', b'# Code\nprint(1)', False),
            ('docs/guide.md', '100644', b'\xff', False),
            ('config/settings.md', '100644', b'key=value', False),
            ('requirements.txt', '100644', b'package==1', False),
            ('.agents/skills/helper/scripts/run.py', '100644', b'print(1)', False),
        ):
            with self.subTest(path=path, mode=mode, content=content):
                self.assertEqual(runtime_inputs.prose(path, mode, content), expected)

    def test_added_deleted_moved_mode_and_mixed_inputs(self):
        for paths, changed in (
            ([('000000', '100644', 'A', 'docs/new.md')], False),
            ([('100644', '000000', 'D', 'docs/old.md')], False),
            ([('100644', '100644', 'M', 'docs/update.md')], False),
            ([('100644', '100755', 'M', 'docs/update.md')], True),
            ([('100644', '100644', 'M', 'docs/AGENTS.worker.md')], True),
            ([('000000', '100644', 'A', 'docs/AGENTS.ponytail.md')], True),
            ([('100644', '000000', 'D', 'docs/AGENTS.reviewer.md')], True),
            ([('100644', '000000', 'D', 'docs/old.md'), ('000000', '100644', 'A', 'loader.py')], True),
            ([('100644', '100644', 'M', 'docs/update.md'), ('100644', '100644', 'M', 'scripts/run.sh')], True),
        ):
            raw = ''.join(f':{old} {new} {"a"*40} {"b"*40} {status}\0{path}\0'
                          for old, new, status, path in paths).encode()
            read = mock.Mock(side_effect=lambda _repo, command, *args: raw if command == 'diff' else b'# Heading\n\nA description.')
            with self.subTest(paths=paths):
                self.assertEqual(runtime_inputs.changed(read, Path('/repo'), 'running-A', 'remote-D'), changed)
                self.assertEqual(read.call_args_list[0].args[-2:], ('running-A', 'remote-D'))
        with self.assertRaises(RuntimeError):
            runtime_inputs.changed(mock.Mock(return_value=b'incomplete'), Path('/repo'), 'a', 'b')


class RuntimeObservationTests(unittest.TestCase):
    def test_pause_database_wait_does_not_reenter_pause_check(self):
        from loop_manager_internal.runtime import handoff_gate
        for paused in (False, True):
            with self.subTest(paused=paused), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                with closing(sqlite3.connect(state/'pause.db')) as writer:
                    writer.execute('CREATE TABLE pauses(scope TEXT PRIMARY KEY)')
                    if paused:
                        writer.execute("INSERT INTO pauses VALUES ('')")
                    writer.commit()
                    writer.execute('BEGIN EXCLUSIVE')
                    def check_pause():
                        if refresh.paused(state):
                            raise refresh.RuntimePaused()
                    check = mock.Mock(side_effect=check_pause)
                    gate = RuntimeHandoffGate(threading.Event(), threading.Lock(), pause_check=check)
                    token = _runtime_handoff_signal.set(gate)
                    original = handoff_gate._check_runtime_handoff
                    def release_and_check():
                        writer.rollback()
                        original()
                    try:
                        with mock.patch.object(handoff_gate, '_check_runtime_handoff', side_effect=release_and_check) as waiting:
                            if paused:
                                with self.assertRaises(refresh.RuntimePaused):
                                    gate.check()
                            else:
                                gate.check()
                        self.assertTrue(waiting.called)
                        check.assert_called_once()
                        gate.stop()
                        gate.pause_check = lambda: handoff_gate._check_runtime_handoff()
                        with self.assertRaises(SelfMergeRuntimeHandoff):
                            gate.check()
                    finally:
                        _runtime_handoff_signal.reset(token)

    def test_observation_commits_completed_implicit_state_and_rejects_explicit_transaction(self):
        from loop_manager_internal.storage.db_access import open_connection, finish_implicit_request
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.db'
            with closing(open_connection(path)) as connection:
                connection.execute('CREATE TABLE evidence(value TEXT)')
                connection.execute("INSERT INTO evidence VALUES ('saved failure')")
                self.assertTrue(connection.in_transaction)
                def observe():
                    with closing(open_connection(path)) as other:
                        self.assertEqual(other.execute('SELECT value FROM evidence').fetchone()[0], 'saved failure')
                gate = RuntimeHandoffGate(threading.Event(), threading.Lock(), observer=observe,
                    before_observe=lambda:finish_implicit_request(path))
                gate.observe()
                self.assertFalse(connection.in_transaction)
                with connection:
                    with self.assertRaisesRegex(RuntimeError, '명시적'):
                        gate.observe()

    def test_remote_code_is_independent_of_pr_status_and_source_sync(self):
        project = RegisteredProject('/source', 'develop', 'codex', None, False, origin='owner/repo')
        runtime = RuntimeMutation(Path('/source'), Path('/runtime'), 'a'*40, 'c'*32)
        for merged in (False, True):
            effects = self_runtime.FinishSelfMergeHandoffEffects(
                mock.Mock(return_value=True), mock.Mock(return_value='b'*40),
                mock.Mock(return_value=SimpleNamespace(returncode=0)), mock.Mock(), mock.Mock())
            gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
            token = _runtime_handoff_signal.set(gate)
            try:
                with mock.patch.object(runtime_inputs, 'changed', return_value=True), self.assertRaises(SelfMergeRuntimeHandoff) as raised:
                    self_runtime._finish_self_merge_handoff(effects, None, project, Path('/state'), runtime,
                        loop_manager.WaitingPullRequestObservation((), merged))
                self.assertTrue(gate.event.is_set())
                self.assertEqual(raised.exception.request, gate.request)
                self.assertEqual(gate.request.base, 'develop')
                effects._recorded_base_fetch.assert_called_once_with(Path('/source'), Path('/state'), 'develop', owner='runtime-observation')
            finally:
                _runtime_handoff_signal.reset(token)

    def test_document_advances_always_compare_running_revision_and_failure_never_drains(self):
        project = RegisteredProject('/source', 'main', 'codex', None, False, origin='owner/repo')
        runtime = RuntimeMutation(Path('/source'), Path('/runtime'), 'a'*40, 'c'*32)
        effects = self_runtime.FinishSelfMergeHandoffEffects(
            mock.Mock(return_value=True), mock.Mock(side_effect=['b'*40, 'c'*40, 'd'*40]),
            mock.Mock(return_value=SimpleNamespace(returncode=0)), mock.Mock(), mock.Mock())
        gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
        token = _runtime_handoff_signal.set(gate)
        try:
            with mock.patch.object(runtime_inputs, 'changed', side_effect=[False, False, OSError('read failed')]) as compare:
                for _ in range(2):
                    self_runtime._finish_self_merge_handoff(effects, None, project, Path('/state'), runtime)
                with self.assertRaisesRegex(RecoveryFailure, 'read failed'):
                    self_runtime._finish_self_merge_handoff(effects, None, project, Path('/state'), runtime)
            self.assertFalse(gate.event.is_set())
            self.assertEqual([call.args[2] for call in compare.call_args_list], ['a'*40]*3)
        finally:
            _runtime_handoff_signal.reset(token)

    def test_boundary_observes_after_persisted_failure_and_blocks_next_effect(self):
        calls = []
        gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
        def observe():
            calls.append('observe')
            if 'saved-failure' in calls:
                gate.stop()
        gate.observer = observe
        @runtime_boundary
        def worker():
            calls.append('saved-failure')
            return 'retry'
        token = _runtime_handoff_signal.set(gate)
        try:
            with self.assertRaises(SelfMergeRuntimeHandoff):
                worker()
            with mock.patch('subprocess.Popen') as process, self.assertRaises(SelfMergeRuntimeHandoff):
                gate.popen(['reviewer'])
            process.assert_not_called()
            self.assertEqual(calls, ['observe', 'saved-failure', 'observe'])
        finally:
            _runtime_handoff_signal.reset(token)


class RefreshReceiptTests(unittest.TestCase):
    def test_global_pause_blocks_initial_and_handoff_reselection_effects(self):
        for initial in (True, False):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                source = loop_manager.ROOT
                runtime = RuntimeWorktree(state/'A', 'a'*40, 'c'*32)
                request = RuntimeRefresh(str(source), 'owner/repo', 'main', runtime.commit_sha, 'b'*40, runtime.mutation_id)
                def pause():
                    with closing(sqlite3.connect(state/'pause.db')) as connection, connection:
                        connection.execute('CREATE TABLE IF NOT EXISTS pauses(scope TEXT PRIMARY KEY)')
                        connection.execute("INSERT INTO pauses VALUES ('')")
                if initial:
                    pause()
                def child(*args):
                    pause()
                    return request
                with mock.patch.object(loop_manager.sys, 'argv', ['loop_manager.py', '--state-dir', str(state), 'run']), \
                     mock.patch.object(loop_manager, 'prepare_bootstrap_runtime', return_value=runtime) as prepare, \
                     mock.patch.object(loop_manager, 'run_runtime_child', side_effect=child) as run:
                    self.assertEqual(loop_manager.main(), 0)
                self.assertEqual(prepare.call_count, 0 if initial else 1)
                self.assertEqual(run.call_count, 0 if initial else 1)

    def test_receipt_is_bound_to_channel_run_and_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = RuntimeWorktree(state/'runtime', 'a'*40, 'c'*32)
            request = RuntimeRefresh('/source', 'owner/repo', 'main', runtime.commit_sha, 'b'*40, runtime.mutation_id)
            path = refresh.receipt_path(state, runtime.mutation_id, 'd'*32)
            path.parent.mkdir()
            payload = dict(version=1, token='d'*32, run_id='logical-run', request=request.__dict__)
            path.write_text(json.dumps(payload))
            self.assertEqual(refresh.read_request(path, 'd'*32, Path('/source'), runtime, 'logical-run'), request)
            for changes in ({'token':'e'*32}, {'run_id':'other'}, {'request':replace(request, source='/other').__dict__},
                            {'request':replace(request, mutation_id='f'*32).__dict__},
                            {'request':replace(request, running_sha='f'*40).__dict__}):
                path.write_text(json.dumps({**payload, **changes}))
                with self.assertRaises(RuntimeError):
                    refresh.read_request(path, 'd'*32, Path('/source'), runtime, 'logical-run')

    def test_parent_reselects_latest_with_lock_and_logical_run_held(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            source = loop_manager.ROOT
            first = RuntimeWorktree(state/'A', 'a'*40, 'c'*32)
            latest = RuntimeWorktree(state/'C', 'e'*40, 'f'*32)
            request = RuntimeRefresh(str(source), 'owner/repo', 'main', first.commit_sha, 'b'*40, first.mutation_id)
            observed = []
            def child(actual_source, actual_state, config, runtime, arguments):
                with self.assertRaises(loop_manager.ProcessLockBusyError):
                    with loop_manager.process_lock(state):
                        pass
                observed.append((runtime.commit_sha, events.current_context()['run'], config, arguments))
                return request if runtime == first else 0
            service = mock.Mock()
            with mock.patch.object(loop_manager.sys, 'argv', ['loop_manager.py', '--state-dir', str(state), 'run', '--skip-if-running']), \
                    mock.patch.object(loop_manager, 'prepare_bootstrap_runtime', side_effect=[first, latest]) as prepare, \
                    mock.patch.object(loop_manager, 'run_runtime_child', side_effect=child), \
                    mock.patch.object(loop_manager, '_bootstrap_service', return_value=service):
                self.assertEqual(loop_manager.main(), 0)
            self.assertEqual([row[0] for row in observed], ['a'*40, 'e'*40])
            self.assertEqual(observed[0][1:], observed[1][1:])
            self.assertNotEqual(prepare.call_args_list[0].args[2], prepare.call_args_list[1].args[2])
            service.validate_refresh.assert_called_once_with(source, state, request, latest.commit_sha)
