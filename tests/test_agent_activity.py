import io
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

from loop_manager_internal.agents.agent_activity import AgentActivity, received_lines, LINE_LIMIT


class AgentActivityTest(TestCase):
    def test_adapter_silence_on_fresh_and_resumed_calls_is_timeout_never_success(self):
        from loop_manager_internal.agents import provider_adapters
        from loop_manager_internal.agents.agent_invocation import ResolvedProfile
        from loop_manager_internal.agents.agent_codex_sandbox import CodexLaunch
        for session, phase in ((None, 'Agent 무응답'), ('saved', '결과 수집 대기')):
            with self.subTest(session=session), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempt = root / 'call-one'
                attempt.mkdir()
                launch = CodexLaunch((), (), {'CODEX_HOME': str(root / 'home')}, root, attempt, '{}')
                monitor = []
                now = [0]
                process = mock.Mock(pid=4321)
                process.poll.return_value = None
                process.wait.return_value = 0
                process.stdin = io.StringIO()
                raw = '{"type":"thread.started","thread_id":"new"}\n'
                if session:
                    raw += '{"type":"turn.completed"}\n'
                class Stream(io.StringIO):
                    def readline(inner, size):
                        part = super().readline(size)
                        if not part:
                            now[0] = 600
                            monitor[0].check(process, monitor[1])
                        return part
                process.stdout = Stream(raw)
                def start(activity, child, stop):
                    activity.clock = lambda: now[0]
                    activity.last_output = 0
                    monitor.extend([activity, stop])
                effects = provider_adapters.ProviderEffects(
                    mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(return_value=process), mock.Mock())
                with mock.patch.object(AgentActivity, 'start', start), \
                     mock.patch.object(CodexLaunch, 'start', return_value=process), \
                     mock.patch.object(CodexLaunch, 'stop') as stop:
                    result = provider_adapters.run_codex(root, root, root / 'config', 'worker', 'prompt',
                        read_only=False, persist_session=True, session_id=session, prepared_launch=launch,
                        resolved_profile=ResolvedProfile('model', 'high'), effects=effects)
                self.assertEqual(result.ledger_kind, 'timeout')
                self.assertNotEqual(result.code, 0)
                self.assertIn(phase, result.message)
                self.assertTrue(stop.called)
                self.assertTrue((root / 'call-one.process.json').is_file())

    def setUp(self):
        self.now = 0
        self.activity = AgentActivity(clock=lambda: self.now)
        self.process = mock.Mock()
        self.process.poll.return_value = None
        self.stop = mock.Mock()

    def event(self, kind, identity='one', item_type='command_execution'):
        self.activity.event(json.dumps({'type': kind, 'item': {'id': identity, 'type': item_type}}))

    def test_ten_minutes_of_silence_terminates_once_and_heartbeat_does_not_reset(self):
        self.now = 599
        self.activity.check(self.process, self.stop)
        self.stop.assert_not_called()
        from loop_manager_internal.runtime import operation_events
        with mock.patch.object(operation_events, 'emit'):
            operation_events.emit('Manager heartbeat')
            self.now = 600
            self.activity.check(self.process, self.stop)
            self.activity.check(self.process, self.stop)
        self.stop.assert_called_once_with(self.process)
        self.assertEqual(self.activity.reason, 'Agent 무응답')

    def test_new_output_resets_timer_even_after_reviewer_storage_limit(self):
        self.now = 590
        output = 'x' * (LINE_LIMIT * 2) + '\n'
        self.assertEqual(list(received_lines(io.StringIO(output), self.activity, bounded=True)), [None])
        self.now = 1189
        self.activity.check(self.process, self.stop)
        self.stop.assert_not_called()
        self.now = 1190
        self.activity.check(self.process, self.stop)
        self.stop.assert_called_once()

    def test_parallel_commands_wait_until_all_complete(self):
        self.event('item.started', 'one')
        self.event('item.started', 'two')
        self.event('item.completed', 'one')
        self.now = 600
        self.activity.check(self.process, self.stop)
        self.stop.assert_not_called()
        self.event('item.completed', 'two')
        self.activity.check(self.process, self.stop)
        self.stop.assert_called_once()

    def test_completed_turn_uses_collection_phase(self):
        self.event('item.started')
        self.event('turn.completed')
        self.now = 600
        self.activity.check(self.process, self.stop)
        self.assertEqual(self.activity.reason, '결과 수집 대기')
        self.stop.assert_called_once()

    def test_oversized_reviewer_event_keeps_unknown_commands_until_turn_completion(self):
        raw = json.dumps({'type': 'item.started', 'item': {'id': 'large',
                         'type': 'command_execution', 'command': 'x' * LINE_LIMIT}}) + '\n'
        self.assertEqual(list(received_lines(io.StringIO(raw), self.activity, bounded=True)), [None])
        self.now = 600
        self.activity.check(self.process, self.stop)
        self.stop.assert_not_called()
        self.event('turn.completed')
        self.activity.check(self.process, self.stop)
        self.assertEqual(self.activity.reason, '결과 수집 대기')

    def test_exited_process_with_open_output_uses_collection_phase(self):
        self.process.poll.return_value = 0
        self.now = 600
        self.activity.check(self.process, self.stop)
        self.stop.assert_called_once()
        self.assertEqual(self.activity.reason, '결과 수집 대기')

    def test_partial_output_refreshes_before_newline_or_truncation(self):
        activity = self.activity
        class Stream:
            chunks = iter(['partial', ' output\n', ''])
            def readline(inner, size):
                self.now += 590
                return next(inner.chunks)
        lines = received_lines(Stream(), activity)
        self.assertEqual(next(lines), 'partial output\n')
        self.assertEqual(activity.last_output, 1180)

    def test_raw_pipe_receipt_precedes_line_completion_and_decodes_split_utf8(self):
        value = '새 출력\r\n'.encode()
        activity = self.activity
        seen = []
        class Buffer:
            parts = iter([value[:1], value[1:-1], value[-1:], b''])
            def read1(inner, size):
                seen.append(activity.last_output)
                self.now += 10
                return next(inner.parts)
        stream = mock.Mock(buffer=Buffer())
        self.assertEqual(list(received_lines(stream, activity)), ['새 출력\n'])
        self.assertEqual(seen[:3], [0, 10, 20])
