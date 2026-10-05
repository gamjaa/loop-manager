"""Physical Agent invocation observations and tolerant provider usage parsing."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import io
from unittest import TestCase, mock

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.agents import agent_provider_usage as provider_usage
from loop_manager_internal.agents import provider_adapters
import loop_manager as manager


class UsageTests(TestCase):
    def test_truncated_reviewer_json_redacts_escaped_credential_key(self):
        raw = ('{"item":{"text":"\\"password\\": \\"escaped-secret\\" and context\n'
               '\\"api_key\\": \\"another-secret\\" more context')
        clean = provider_adapters._redact_reviewer_log(raw)
        self.assertNotIn('escaped-secret', clean)
        self.assertNotIn('another-secret', clean)
        self.assertIn('context', clean)

    def test_reviewer_provider_artifacts_redact_json_credentials(self):
        secret = 'review-json-secret'
        review = f'{{"password": "{secret}", "result": "VERDICT: FAIL"}}'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def codex_run(arguments, **kwargs):
                self.assertNotIn('-o', arguments)
                self.assertEqual(kwargs['stdout'], subprocess.PIPE)
                event = json.dumps({'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': review,
                }}) + '\n' + '{"item":{"text":"\\"password\\": \\"truncated-secret'
                return subprocess.run(
                    [sys.executable, '-c', 'import sys; sys.stdout.write(sys.argv[1])', event],
                    **{key: value for key, value in kwargs.items() if key != 'input'},
                    input=kwargs['input'],
                )

            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), codex_run,
            )
            codex_result = provider_adapters.run_codex(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
            self.assertNotIn(secret, str(codex_result))
            self.assertIn('VERDICT: FAIL', codex_result[1])
            log_dir, = (root / 'logs').iterdir()
            for artifact in ('reviewer.log', 'reviewer-last.txt'):
                self.assertNotIn(secret, (log_dir / artifact).read_text())
                self.assertNotIn('truncated-secret', (log_dir / artifact).read_text())

            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(),
                mock.Mock(return_value=mock.Mock(
                    returncode=0, stdout=('{"result": ' +
                    json.dumps(review) + '}'),
                )),
            )
            claude_result = provider_adapters.run_claude(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
            self.assertNotIn(secret, str(claude_result))
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
            provider_adapters.run_codex(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                persist_session=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
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
            provider_adapters.run_claude(
                root, root, root / 'config', 'reviewer', 'prompt', read_only=True,
                persist_session=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high'), effects=effects,
            )
            for artifact in ('claude-reviewer.log', 'claude-reviewer-last.txt'):
                self.assertNotIn(secret, (log_dir / artifact).read_text())

    def test_reviewer_auxiliary_copy_failure_preserves_completed_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = json.dumps({'type': 'item.completed', 'item': {
                'type': 'agent_message', 'text': 'VERDICT: PASS',
            }})
            calls = mock.Mock(return_value=subprocess.CompletedProcess([], 0, event))
            effects = provider_adapters.ProviderEffects(
                mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), calls,
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
            with mock.patch.object(manager.subprocess, "run", return_value=mock.Mock(
                returncode=1, stdout="{not-json")):
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
