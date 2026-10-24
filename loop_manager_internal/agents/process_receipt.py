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
    _write(receipt_path(launch), _identity(process))


def finished(launch, process) -> None:
    value = _identity(process)
    value.update(finished_at=datetime.now(timezone.utc).isoformat(),
                 evidence='windows_job_stopped' if os.name == 'nt' else 'process_group_stopped')
    _write(receipt_path(launch), value)


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
