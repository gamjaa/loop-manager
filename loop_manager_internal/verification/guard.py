"""테스트에서 실제 Agent 명령과 외부 API 연결을 실행 전에 거부한다."""
from functools import lru_cache
import ipaddress
import os
from pathlib import Path
import re
import sys

MARKER = "LOOP_MANAGER_TEST_NO_AGENTS"
_agent = re.compile(r"(?i)(?:^|[\s/\\\"'])(?:codex|claude)(?:\.(?:exe|cmd|bat))?(?=$|[\s\"'])")


def check_command(arguments, *, enabled=None):
    if enabled is None:
        enabled = os.environ.get(MARKER) == "1"
    if not enabled:
        return
    if isinstance(arguments, (str, bytes)):
        command = os.fsdecode(arguments)
    else:
        if not arguments:
            return
        executable = os.fsdecode(arguments[0]).replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        if executable not in {"codex", "claude", "cmd", "sh", "bash", "zsh", "pwsh", "powershell", "npx", "env", "sudo"}:
            return
        command = " ".join(os.fsdecode(value) for value in arguments)
    if _agent.search(command):
        raise RuntimeError("테스트에서 실제 Agent CLI 호출을 차단했습니다. 실행 경계를 mock으로 주입하세요.")


def _local(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host in {None, "", "localhost"}:
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


@lru_cache(maxsize=1)
def install_guard():
    def audit(event, arguments):
        if event == "subprocess.Popen":
            check_command([arguments[0]], enabled=True)
            check_command(arguments[1], enabled=True)
            environment = arguments[3]
            # env를 명시하거나 테스트가 부모 env를 비워도 Python 자식의 차단을 유지한다.
            inherited = worker_environment(environment if environment is not None else os.environ)
            target = environment if environment is not None else os.environ
            target[MARKER], target["PYTHONPATH"] = "1", inherited["PYTHONPATH"]
        elif event == "os.system":
            check_command(arguments[0], enabled=True)
        elif event in {"os.exec", "os.posix_spawn"}:
            check_command([arguments[0]], enabled=True)
            check_command(arguments[1], enabled=True)
        elif event == "socket.getaddrinfo" and not _local(arguments[0]):
            raise RuntimeError("테스트의 외부 API/DNS 호출을 차단했습니다.")
        elif event == "socket.connect":
            address = arguments[1]
            if isinstance(address, tuple) and not _local(address[0]):
                raise RuntimeError("테스트의 외부 API 연결을 차단했습니다.")
    sys.addaudithook(audit)


def worker_environment(environment):
    child = dict(environment)
    child[MARKER] = "1"
    child["PYTHONIOENCODING"] = "utf-8"
    root = str(Path(__file__).resolve().parents[2])
    paths = [str(Path(__file__).parent / "child_guard"), root,
             *child.get("PYTHONPATH", "").split(os.pathsep)]
    child["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(filter(None, paths)))
    for name in ("LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SECRET", "LOOP_MANAGER_VERIFY_SESSION"):
        child.pop(name, None)
    return child
