"""Use each CLI's installed plugin inventory once per Manager execution."""
from __future__ import annotations

import json
import subprocess
from threading import Lock


def ponytail_installed(provider: str, output: str) -> bool:
    inventory = json.loads(output)
    if provider == 'codex':
        if not isinstance(inventory, dict) or not isinstance(inventory.get('installed'), list):
            raise ValueError('Codex installed plugin 목록 형식이 아닙니다.')
        entries = inventory['installed']
    elif provider == 'claude':
        if not isinstance(inventory, list):
            raise ValueError('Claude installed plugin 목록 형식이 아닙니다.')
        entries = inventory
    else:
        raise ValueError('지원하지 않는 plugin provider입니다.')
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('plugin 항목 형식이 아닙니다.')
        identity = entry.get('pluginId' if provider == 'codex' else 'id')
        if not isinstance(identity, str):
            raise ValueError('plugin 식별자가 불명확합니다.')
        name, separator, marketplace = identity.partition('@')
        if not name or not separator or not marketplace or '@' in marketplace:
            raise ValueError('plugin 식별자가 불명확합니다.')
        if provider == 'codex':
            if entry.get('name') != name or type(entry.get('installed')) is not bool:
                raise ValueError('Codex plugin 설치 상태가 불명확합니다.')
            installed = entry['installed']
        else:
            installed = True  # Without --available Claude lists installed entries only.
        found = found or (name == 'ponytail' and installed)
    return found


class PonytailPlugins:
    def __init__(self, *, run=None, log=print):
        self.run = run
        self.log = log
        self._lock = Lock()
        self._installed = {}

    def reset(self) -> None:
        with self._lock:
            self._installed.clear()

    def installed(self, provider: str) -> bool:
        if provider not in {'codex', 'claude'}:
            raise ValueError('지원하지 않는 plugin provider입니다.')
        with self._lock:
            if not self._installed:
                for cli in ('codex', 'claude'):
                    self._installed[cli] = self._query(cli)
            return self._installed[provider]

    def _query(self, provider: str) -> bool:
        try:
            result = (self.run or subprocess.run)(
                [provider, 'plugin', 'list', '--json'], capture_output=True,
                text=True, encoding='utf-8', timeout=15, check=False,
            )
            if result.returncode:
                raise ValueError(f'plugin list 종료 코드 {result.returncode}')
            installed = ponytail_installed(provider, result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as error:
            self.log(f'Ponytail {provider}: plugin 조회 실패 ({error}); Manager 지침 추가')
            return False
        decision = 'Manager 지침 생략' if installed else 'Manager 지침 추가'
        self.log(f'Ponytail {provider}: 설치 {"확인" if installed else "없음"}; {decision}')
        return installed
