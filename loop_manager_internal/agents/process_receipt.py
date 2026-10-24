"""Manager 전용 runtime에 남기는 호출별 프로세스 종료 근거."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from loop_manager_internal.execution.verify_process import _confirm_posix_group_stopped


def receipt_path(launch) -> Path:
    # Worker가 쓸 수 있는 attempt 바깥에 보존한다.
    return launch.runtime / (launch.attempt.name + '.process.json')


def _linux_host() -> str:
    return hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()


def _boot() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _identity(process) -> dict:
    value = {'pid': process.pid, 'platform': sys.platform}
    if sys.platform.startswith('linux'):
        value.update(host=_linux_host(), boot=_boot())
    return value


def started(launch, process) -> None:
    _write_process(launch, _identity(process))


def finished(launch, process) -> None:
    value = _identity(process)
    value.update(finished_at=datetime.now(timezone.utc).isoformat(),
                 evidence='windows_job_stopped' if os.name == 'nt' else 'process_group_stopped')
    _write_process(launch, value)


def _write_process(launch, value):
    path = receipt_path(launch)
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(previous, dict):
            raise ValueError('process receipt 형식이 다릅니다.')
        if 'invocation_observation' in previous:
            value['invocation_observation'] = previous['invocation_observation']
    _write(path, value)


def observe(launch, identity: str, observation: dict) -> None:
    """Keep non-secret facts beside the existing process receipt before DB writes."""
    path = receipt_path(launch)
    value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'platform': sys.platform}
    if not isinstance(value, dict) or value.get('platform') != sys.platform:
        raise ValueError('process receipt 형식 또는 실행 환경이 다릅니다.')
    previous = value.get('invocation_observation')
    if previous is not None and (not isinstance(previous, dict) or previous.get('identity') != identity):
        raise ValueError('process receipt의 invocation identity가 다릅니다.')
    value['invocation_observation'] = dict(version=1, identity=identity, **observation)
    _write(path, value)


def observation(path: str, state_dir: Path, identity: str) -> dict | None:
    """Read only a Manager-owned receipt, independently of process termination."""
    candidate = Path(path)
    base = Path(state_dir).resolve() / 'agent-runtime'
    if (candidate.resolve() != candidate.absolute() or candidate.is_symlink()
            or candidate.parent.parent != base or not candidate.name.endswith('.process.json')):
        raise ValueError('invocation 관측 receipt 경로가 유효하지 않습니다.')
    try:
        root = json.loads(candidate.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(root, dict) or root.get('platform') != sys.platform:
        raise ValueError('invocation 관측 receipt 형식이 유효하지 않습니다.')
    value = root.get('invocation_observation')
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('identity') != identity
            or set(value) - {'version', 'identity', 'started_at', 'finished_at', 'duration_ms', 'session_id', 'outcome'}):
        raise ValueError('invocation 관측 identity 또는 형식이 다릅니다.')
    for key in ('started_at', 'finished_at'):
        if key in value and (not isinstance(value[key], str)
                or datetime.fromisoformat(value[key]).tzinfo is None):
            raise ValueError('invocation 관측 시각이 유효하지 않습니다.')
    if 'finished_at' in value and ('started_at' not in value or type(value.get('duration_ms')) is not int
                                      or value['duration_ms'] < 0):
        raise ValueError('invocation 종료 관측에 시작·실행 시간이 없습니다.')
    if 'session_id' in value and (not isinstance(value['session_id'], str) or not value['session_id']):
        raise ValueError('invocation session 관측 형식이 다릅니다.')
    if 'outcome' in value:
        outcome = value['outcome']
        fields = {'kind', 'code', 'duration_ms', 'session_id', 'usage', 'collection_status', 'observed_at'}
        if (not isinstance(outcome, dict) or set(outcome) != fields
                or outcome['kind'] not in {'success', 'transient_failure', 'usage_limit', 'permanent_configuration',
                                           'timeout', 'exception', 'interrupted', 'database_wait'}
                or outcome['code'] is not None and type(outcome['code']) is not int
                or outcome['kind'] == 'success' and outcome['code'] != 0
                or type(outcome['duration_ms']) is not int or outcome['duration_ms'] < 0
                or outcome['session_id'] is not None and (not isinstance(outcome['session_id'], str) or not outcome['session_id'])
                or not isinstance(outcome['collection_status'], str)
                or not isinstance(outcome['observed_at'], str)
                or datetime.fromisoformat(outcome['observed_at']).tzinfo is None):
            raise ValueError('invocation 결과 관측 형식이 다릅니다.')
        usage = outcome['usage']
        tokens = {'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                  'output_tokens', 'reasoning_output_tokens'}
        if (not isinstance(usage, dict) or set(usage) != tokens | {'status'}
                or usage['status'] not in {'unknown', 'known', 'partial', 'invalid'}
                or any(usage[key] is not None and (type(usage[key]) is not int or usage[key] < 0) for key in tokens)):
            raise ValueError('invocation 사용량 관측 형식이 다릅니다.')
    return value


def not_started(launch) -> None:
    """Only a rejected prelaunch callback can prove process creation was not reached."""
    if receipt_path(launch).exists():
        return  # Never replace an existing process observation with non-execution.
    _write(receipt_path(launch), dict(platform=sys.platform,
           finished_at=datetime.now(timezone.utc).isoformat(), evidence='not_started'))


def recover(path: str, state_dir: Path) -> dict | None:
    """실행 결과를 추정하지 않고, 같은 호스트의 writer 종료만 재관측한다."""
    try:
        candidate = Path(path)
        base = Path(state_dir).resolve() / 'agent-runtime'
        if (candidate.resolve() != candidate.absolute() or candidate.is_symlink()
                or candidate.parent.parent != base or not candidate.name.endswith('.process.json')):
            return None
        value = json.loads(candidate.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('platform') != sys.platform:
            return None
        if value.get('evidence') == 'not_started' and 'pid' not in value:
            if datetime.fromisoformat(value.get('finished_at')).tzinfo is not None:
                return value
            return None
        if type(value.get('pid')) is not int or value['pid'] <= 1:
            return None
        if value.get('finished_at') and value.get('evidence') in {
                'windows_job_stopped', 'process_group_stopped',
                'process_group_absent_on_recovery', 'host_rebooted'}:
            if datetime.fromisoformat(value['finished_at']).tzinfo is None:
                return None
            return value
        if not sys.platform.startswith('linux') or value.get('host') != _linux_host():
            return None
        if value.get('boot') == _boot():
            _confirm_posix_group_stopped(value['pid'])
            reason = 'process_group_absent_on_recovery'
        elif value.get('boot'):
            reason = 'host_rebooted'
        else:
            return None
        value.update(finished_at=datetime.now(timezone.utc).isoformat(), evidence=reason)
        _write(candidate, value)
        return value
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
