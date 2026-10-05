"""Physical Agent invocation observations and tolerant provider usage parsing."""

from pathlib import Path
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from unittest import TestCase, mock

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.agents import agent_provider_usage as provider_usage
from loop_manager_internal.agents import provider_adapters
import loop_manager as manager


class UsageTests(TestCase):
    def test_reviewer_escaped_literals_keep_nonsecret_failure_context(self):
        diagnostic = r'FAILED parser at \u003d and \" token'
        malformed = r'{"type":"error","message":"Unexpected \u003d and \" token'
        self.assertEqual(provider_adapters._redact_reviewer_log(diagnostic), diagnostic)
        self.assertEqual(provider_adapters._redact_reviewer_log(malformed), malformed)

        event = json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': diagnostic,
        }})
        clean = json.loads(provider_adapters._redact_reviewer_log(event))
        self.assertEqual(clean['item']['text'], diagnostic)

        mixed = r'FAILED parser at \u003d; password: hidden-secret; tail \"'
        clean = provider_adapters._redact_reviewer_log(mixed)
        self.assertNotIn('hidden-secret', clean)
        self.assertIn(r'FAILED parser at \u003d', clean)
        self.assertIn(r'tail \"', clean)

    def test_reviewer_pipe_yields_session_line_before_eof(self):
        read_fd, write_fd = os.pipe()
        received = []
        done = threading.Event()
        with os.fdopen(read_fd, 'r', encoding='utf-8') as reader:
            writer = os.fdopen(write_fd, 'w', encoding='utf-8')

            def receive():
                try:
                    received.append(next(provider_adapters._bounded_reviewer_lines(reader)))
                except Exception as error:
                    received.append(error)
                finally:
                    done.set()

            thread = threading.Thread(target=receive, daemon=True)
            thread.start()
            try:
                session_line = '{"type":"thread.started","thread_id":"session"}\n'
                writer.write(session_line)
                writer.flush()
                self.assertTrue(done.wait(1), 'session line waited for pipe EOF')
                self.assertEqual(received, [session_line])
            finally:
                writer.close()
                thread.join(1)

    def test_truncated_reviewer_json_redacts_escaped_credential_key(self):
        raw = ('{"item":{"text":"\\"password\\": \\"escaped-secret\\" and context\n'
               '\\"api_key\\": \\"another-secret\\" more context')
        clean = provider_adapters._redact_reviewer_log(raw)
        self.assertNotIn('escaped-secret', clean)
        self.assertNotIn('another-secret', clean)
        self.assertIn('malformed reviewer JSON omitted', clean)

    def test_truncated_reviewer_json_omits_unicode_escaped_key(self):
        raw = '{"item":{"text":"password: first-secret; pass\\u0077ord: unicode-secret and context'
        clean = provider_adapters._redact_reviewer_log(raw)
        self.assertNotIn('first-secret', clean)
        self.assertNotIn('unicode-secret', clean)
        self.assertIn('malformed reviewer JSON omitted', clean)

        doubly_escaped = r'{"item":{"text":"pass\\u0077ord: double-secret and context'
        clean = provider_adapters._redact_reviewer_log(doubly_escaped)
        self.assertNotIn('double-secret', clean)
        self.assertIn('malformed reviewer JSON omitted', clean)

    def test_nested_reviewer_json_redacts_unicode_escaped_key(self):
        raw = json.dumps({'item': {'text': '{"pass\\u0077ord": "nested-secret"}'}})
        clean = provider_adapters._redact_reviewer_log(raw)
        self.assertNotIn('nested-secret', clean)
        self.assertIn('<REDACTED>', clean)

    def test_reviewer_provider_artifacts_redact_json_credentials(self):
        secret = 'review-json-secret'
        context = r'FAILED parser at \u003d and \" token'
        review = json.dumps({
            'password': secret, 'result': 'VERDICT: FAIL', 'detail': context,
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def codex_run(arguments, **kwargs):
                self.assertNotIn('-o', arguments)
                self.assertEqual(kwargs['stdout'], subprocess.PIPE)
                event = json.dumps({'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': review,
                }}) + '\n' + '{"item":{"text":"\\"password\\": \\"truncated-secret'
                return subprocess.Popen(
                    [sys.executable, '-c',
                     'import sys; sys.stdin.read(); sys.stdout.write(sys.argv[1])', event],
                    **kwargs,
                )

            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(), codex_run, mock.Mock(),
            )
            codex_result = provider_adapters.run_codex(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
            self.assertNotIn(secret, str(codex_result))
            self.assertIn('VERDICT: FAIL', codex_result[1])
            self.assertEqual(json.loads(codex_result[1])['detail'], context)
            log_dir, = (root / 'logs').iterdir()
            for artifact in ('reviewer.log', 'reviewer-last.txt'):
                self.assertNotIn(secret, (log_dir / artifact).read_text())
                self.assertNotIn('truncated-secret', (log_dir / artifact).read_text())

            claude_process = mock.Mock()
            claude_process.stdin = io.StringIO()
            claude_process.stdout = io.StringIO(
                json.dumps({'type': 'result', 'result': review}) + '\n'
            )
            claude_process.wait.return_value = 0
            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(),
                mock.Mock(return_value=claude_process), mock.Mock(),
            )
            claude_result = provider_adapters.run_claude(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
            self.assertNotIn(secret, str(claude_result))
            self.assertEqual(json.loads(claude_result[1])['detail'], context)
            for artifact in ('claude-reviewer.log', 'claude-reviewer-last.txt'):
                self.assertNotIn(secret, (log_dir / artifact).read_text())

    def test_streaming_reviewer_logs_redact_before_storage(self):
        secret = 'streaming-review-secret'
        review = f'{{"password": "{secret}", "result": "VERDICT: FAIL"}}'

        class Process:
            def __init__(self, stream):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(stream)

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def codex_start(arguments, **_kwargs):
                self.assertNotIn('-o', arguments)
                event = json.dumps({'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': review,
                }})
                return Process('{"type":"thread.started","thread_id":"session"}\n' + event)

            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(), codex_start, mock.Mock(),
            )
            codex_sessions = []
            provider_adapters.run_codex(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                persist_session=True, on_session_started=codex_sessions.append,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
            self.assertEqual(codex_sessions, ['session'])
            log_dir, = (root / 'logs').iterdir()
            for artifact in ('reviewer.log', 'reviewer-last.txt'):
                self.assertNotIn(secret, (log_dir / artifact).read_text())

            claude_stream = json.dumps({
                'type': 'result', 'session_id': 'session', 'result': review,
            }) + '\n'
            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(),
                mock.Mock(return_value=Process(claude_stream)), mock.Mock(),
            )
            claude_sessions = []
            provider_adapters.run_claude(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                persist_session=True, on_session_started=claude_sessions.append,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
            self.assertEqual(claude_sessions, ['session'])
            for artifact in ('claude-reviewer.log', 'claude-reviewer-last.txt'):
                self.assertNotIn(secret, (log_dir / artifact).read_text())

    def test_reviewer_streams_oversized_lines_without_retaining_raw_output(self):
        secret = 'oversized-review-secret'

        class Stream(io.StringIO):
            def read(self, size=-1):
                self_test.fail('reviewer output must be read one bounded line at a time')

            def readline(self, size=-1):
                self_test.assertEqual(size, provider_adapters.MAX_REVIEWER_LINE + 1)
                return super().readline(size)

        class Process:
            def __init__(self, stream):
                self.stdin = io.StringIO()
                self.stdout = Stream(stream)

            def wait(self):
                return 0

        self_test = self
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            large = json.dumps({'password': secret, 'detail': 'x' * 4096}) + '\n'
            codex_final = json.dumps({'type': 'item.completed', 'item': {
                'type': 'agent_message', 'text': 'VERDICT: PASS',
            }}) + '\n'
            claude_final = json.dumps({
                'type': 'result', 'session_id': 'session', 'result': 'VERDICT: PASS',
            }) + '\n'
            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(),
                mock.Mock(return_value=Process(
                    '{"type":"thread.started","thread_id":"session"}\n' + large + codex_final,
                )), mock.Mock(),
            )
            with mock.patch.object(provider_adapters, 'MAX_REVIEWER_LINE', 256), \
                 mock.patch.object(provider_adapters, 'MAX_REVIEWER_LOG', 256):
                codex = provider_adapters.run_codex(
                    root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                    persist_session=True,
                    resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
                )
                effects = provider_adapters.ProviderEffects(
                    mock.Mock(), mock.Mock(), mock.Mock(),
                    mock.Mock(return_value=Process(large + claude_final)), mock.Mock(),
                )
                claude = provider_adapters.run_claude(
                    root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                    persist_session=True,
                    resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
                )
            self.assertEqual((codex.message, claude.message), ('VERDICT: PASS', 'VERDICT: PASS'))
            log_dir, = (root / 'logs').iterdir()
            for artifact in ('reviewer.log', 'claude-reviewer.log'):
                content = (log_dir / artifact).read_text()
                self.assertLessEqual(len(content), 256)
                self.assertNotIn(secret, content)
                self.assertIn('reviewer output line omitted', content)

    def test_reviewer_auxiliary_copy_failure_preserves_completed_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = json.dumps({'type': 'item.completed', 'item': {
                'type': 'agent_message', 'text': 'VERDICT: PASS',
            }})
            process = mock.Mock()
            process.stdin = io.StringIO()
            process.stdout = io.StringIO(event)
            process.wait.return_value = 0
            calls = mock.Mock(return_value=process)
            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(), calls, mock.Mock(),
            )
            original_write = Path.write_text

            def write(path, content, *args, **kwargs):
                if path.name == 'reviewer-last.txt':
                    raise OSError('auxiliary copy unavailable')
                return original_write(path, content, *args, **kwargs)

            with mock.patch.object(Path, 'write_text', autospec=True, side_effect=write):
                result = provider_adapters.run_codex(
                    root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                    session_id='existing-session',
                    resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
                )
            self.assertEqual((result.code, result.session_id, result.collection_status),
                             (0, 'existing-session', 'error'))
            calls.assert_called_once()

    def test_stream_timeout_preserves_observed_session_and_failure_kind(self):
        class Process:
            def __init__(self, stream):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(stream)
                self.killed = False

            def kill(self):
                self.killed = True

            def wait(self):
                return -9

        class ImmediateTimeout:
            def __init__(self, seconds, terminate):
                self.seconds = seconds
                self.terminate = terminate

            def start(self):
                self.terminate()

            def cancel(self):
                pass

        cases = (
            ('codex', '{"type":"thread.started","thread_id":"observed"}\n', provider_adapters.run_codex),
            ('claude', '{"type":"result","session_id":"observed","result":"partial"}\n', provider_adapters.run_claude),
        )
        for name, stream, run in cases:
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as directory:
                process = Process(stream)
                effects = provider_adapters.ProviderEffects(
                    mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(return_value=process), mock.Mock())
                observed = []
                root = Path(directory)
                with mock.patch.object(provider_adapters.threading, 'Timer', ImmediateTimeout):
                    result = run(root, root, root / 'config', 'worker', 'prompt', read_only=False,
                                 persist_session=True, resolved_profile=invocation.ResolvedProfile('model', 'high'),
                                 on_session_started=observed.append, effects=effects)
                self.assertTrue(process.killed)
                self.assertEqual(observed, ['observed'])
                self.assertEqual(result.session_id, 'observed')
                self.assertEqual(result.result_kind, invocation.AgentResultKind.TRANSIENT_FAILURE)
                self.assertEqual(result.ledger_kind, 'timeout')
                self.assertIn('3600초', result.message)

    def test_codex_final_usage_preserves_nullable_categories(self):
        usage = provider_usage.codex_log_usage(
            '{"type":"turn.completed","usage":{"input_tokens":100,'
            '"cached_input_tokens":60,"output_tokens":15}}\n'
        )
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.cache_write_input_tokens, usage.output_tokens,
                          usage.reasoning_output_tokens, usage.status),
                         (100, 60, None, 15, None, "known"))
        self.assertEqual(provider_usage.codex_log_usage("not json").status, "invalid")

    def test_invalid_counters_do_not_replace_other_provider_values(self):
        usage = provider_usage.claude_usage({"type": "result", "usage": {
            "input_tokens": 10, "cache_read_input_tokens": -1,
            "cache_creation_input_tokens": "5", "output_tokens": 3}})
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.cache_write_input_tokens, usage.output_tokens,
                          usage.reasoning_output_tokens, usage.status),
                         (10, None, None, 3, None, "invalid"))
        self.assertEqual(provider_usage.claude_usage({"type": "result"}).status, "unknown")

    def test_codex_nested_cached_and_reasoning_counters_are_independent(self):
        usage = provider_usage.codex_usage({"type": "turn.completed", "usage": {
            "input_tokens": 50, "output_tokens": 8,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 4}}})
        self.assertEqual((usage.cached_input_tokens, usage.reasoning_output_tokens),
                         (30, 4))

    def test_codex_inconsistent_breakdowns_are_invalid_without_losing_totals(self):
        usage = provider_usage.codex_usage({"type": "turn.completed", "usage": {
            "input_tokens": 5, "cached_input_tokens": 6,
            "output_tokens": 2, "reasoning_output_tokens": 3}})
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens, usage.reasoning_output_tokens,
                          usage.status), (5, None, 2, None, "invalid"))

    def test_claude_complete_usage_keeps_cache_reads_and_writes_separate(self):
        usage = provider_usage.claude_usage({"type": "result", "usage": {
            "input_tokens": 20, "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 5, "output_tokens": 4}})
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.cache_write_input_tokens, usage.output_tokens,
                          usage.reasoning_output_tokens, usage.status),
                         (20, 40, 5, 4, None, "known"))

    def test_claude_reported_thinking_breakdown_is_preserved(self):
        usage = provider_usage.claude_usage({"type": "result", "usage": {
            "output_tokens": 8, "output_tokens_details": {"thinking_tokens": 3}}})
        self.assertEqual((usage.output_tokens, usage.reasoning_output_tokens,
                          usage.status), (8, 3, "known"))

    def test_claude_malformed_json_keeps_agent_failure_and_invalid_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = mock.Mock()
            process.stdin = io.StringIO()
            process.stdout = io.StringIO("{not-json")
            process.wait.return_value = 1
            with mock.patch.object(manager.subprocess, "Popen", return_value=process):
                result = manager.run_claude(
                    root, root, root / "config", "reviewer", "prompt",
                    read_only=True, persist_session=True, session_id="session",
                    resolved_profile=invocation.ResolvedProfile("model", "high"))
        self.assertEqual(result.code, 1)
        self.assertEqual(result.usage.status, "invalid")

    def test_codex_scalar_json_event_does_not_block_session_or_usage(self):
        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(
                    '42\n{"type":"thread.started","thread_id":"session"}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":10,'
                    '"cached_input_tokens":4,"output_tokens":2}}\n'
                )

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(manager.subprocess, "Popen", return_value=Process()):
                result = manager.run_codex(
                    root, root, root / "config", "planner", "prompt",
                    read_only=False, persist_session=True,
                    resolved_profile=invocation.ResolvedProfile("model", "high"))
        self.assertEqual((result.code, result.session_id,
                          result.usage.cached_input_tokens), (0, "session", 4))

    def test_codex_usage_log_read_failure_does_not_retry_successful_call(self):
        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(
                    '{"type":"thread.started","thread_id":"session"}\n'
                )

            def wait(self):
                return 0

        original_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == "planner.log":
                raise OSError("usage log unavailable")
            return original_read_text(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(manager.subprocess, "Popen", side_effect=lambda *a, **k: Process()) as launch, \
                 mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text):
                result = manager.run_agent_with_retries(
                    lambda _session, _fallback: manager.run_codex(
                        root, root, root / "config", "planner", "prompt",
                        read_only=False, persist_session=True,
                        resolved_profile=invocation.ResolvedProfile("model", "high")),
                    session_id=None,
                )
        self.assertEqual((result.code, result.session_id, result.usage.status),
                         (0, "session", "invalid"))
        self.assertEqual(launch.call_count, 1)

    def test_codex_auxiliary_log_copy_failure_preserves_completed_result(self):
        result, runs = self._codex_auxiliary_copy_failure(
            persist_session=True, session_id="saved-session",
        )
        self.assertEqual((result.code, result.session_id, result.usage.input_tokens,
                          result.collection_status),
                         (0, "saved-session", 10, "error"))
        self.assertEqual(runs, 1)

    def test_codex_ephemeral_copy_failure_returns_collection_error(self):
        result, runs = self._codex_auxiliary_copy_failure(
            persist_session=False, session_id=None,
        )
        self.assertEqual((result.code, result.session_id, result.usage.input_tokens,
                          result.collection_status),
                         (0, None, 10, "error"))
        self.assertEqual(runs, 1)

    def test_codex_failed_log_read_does_not_retry_completed_call(self):
        original_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == "planner.log":
                raise OSError("failure log unavailable")
            return original_read_text(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(manager.subprocess, "run",
                                   return_value=mock.Mock(returncode=1)) as launch, \
                 mock.patch.object(Path, "read_text", autospec=True,
                                   side_effect=read_text):
                result = manager.run_agent_with_retries(
                    lambda _session, _fallback: manager.run_codex(
                        root, root, root / "config", "planner", "prompt",
                        read_only=False,
                        resolved_profile=invocation.ResolvedProfile("model", "high")),
                    session_id=None,
                )
        self.assertEqual((result.code, result.message, result.collection_status,
                          result.usage.status), (1, "", "error", "invalid"))
        self.assertEqual(launch.call_count, 1)

    def _codex_auxiliary_copy_failure(self, *, persist_session, session_id):
        original_write_text = Path.write_text

        def write_text(path, data, *args, **kwargs):
            if path.name == "planner.log":
                raise OSError("auxiliary log unavailable")
            return original_write_text(path, data, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = root / "attempt"
            attempt.mkdir()
            launch = provider_adapters.agent_write_isolation.CodexLaunch(
                (), (), {}, root, attempt, "{}",
            )
            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(),
            )
            evidence = mock.Mock(status="verified")
            def run(_launch, _arguments, **kwargs):
                kwargs["stdout"].write(
                    '{"type":"turn.completed","usage":{"input_tokens":10,'
                    '"cached_input_tokens":4,"output_tokens":2}}\n'
                )
                return mock.Mock(returncode=0)

            def prepare(_provider, _role, _worktree, _state_dir, _read_only, observe,
                        *_args, **_kwargs):
                observe(evidence)
                return launch

            with mock.patch.object(provider_adapters.agent_write_isolation.CodexLaunch,
                                   "run", autospec=True, side_effect=run) as run_call, \
                 mock.patch.object(provider_adapters.agent_write_isolation,
                                   "prepare_isolation", side_effect=prepare), \
                 mock.patch.object(provider_adapters.agent_write_isolation,
                                   "assert_current"), \
                 mock.patch.object(Path, "write_text", autospec=True,
                                   side_effect=write_text):
                result = provider_adapters.run_codex(
                    root, root, root / "config", "planner", "prompt",
                    read_only=False, persist_session=persist_session, session_id=session_id,
                    resolved_profile=invocation.ResolvedProfile("model", "high"),
                    isolation_snapshot_digest=lambda: "snapshot",
                    on_isolation_checked=mock.Mock(), effects=effects,
                )
        return result, run_call.call_count
