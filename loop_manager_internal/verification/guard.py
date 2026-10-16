"""테스트에서 실제 Agent 명령과 외부 API 연결을 실행 전에 거부한다."""
from functools import wraps
import inspect
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

MARKER = "LOOP_MANAGER_TEST_NO_AGENTS"
_COORDINATION_ENV = (
    "LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SECRET", "LOOP_MANAGER_VERIFY_SESSION",
    "LOOP_MANAGER_VERIFY_BUDGET_DIR",
)
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


# runpy/sitecustomize and candidate imports share interpreter-owned installation evidence.
_STATE_KEY = "_loop_manager_verify_guard_state"
_state = sys.__dict__.setdefault(_STATE_KEY, SimpleNamespace(
    lock=threading.RLock(), active=threading.local(), audit=None, wrapper=None))
_PROBE = "loop_manager.verify_guard.probe"


def install_guard():
    with _state.lock:
        if _state.audit is None:
            def audit(event, arguments):
                if event == _PROBE:
                    if arguments[0] is _state:
                        arguments[1].append(audit)
                elif event == "subprocess.Popen":
                    if arguments[0] is not None:
                        check_command([arguments[0]], enabled=True)
                    environment = arguments[3]
                    active = getattr(_state.active, "environment", None)
                    command = arguments[1]
                    if active is not None and len(active) > 2 and active[0] is environment and active[2] is not None:
                        command = active[2]
                    check_command(command, enabled=True)
                    if environment is not None and not (
                            active is not None and environment is active[0] and environment == active[1]):
                        prepared = worker_environment(environment)
                        environment.clear()
                        environment.update(prepared)
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
            _state.audit = audit
        confirmed = []
        sys.audit(_PROBE, _state, confirmed)
        if _state.audit not in confirmed:
            _state.audit = None
            raise RuntimeError("verify Python 가드 audit hook 설치를 확인하지 못했습니다.")
        _install_child_environment_guard()
        os.environ[MARKER] = "1"


def _install_child_environment_guard():
    original_init = subprocess.Popen.__init__
    if original_init is _state.wrapper:
        return
    signature = inspect.signature(original_init)

    @wraps(original_init)
    def guarded_init(self, *args, **kwargs):
        bound = signature.bind_partial(self, *args, **kwargs)
        environment = worker_environment(bound.arguments.get("env"))
        bound.arguments["env"] = environment
        command = bound.arguments.get("args")
        if isinstance(command, (list, tuple)) and not bound.arguments.get("shell") and bound.arguments.get("executable") is None:
            # Windows audit의 문자열 변환 전 argv를 고정해 Python 코드 속 모델 이름과 실행 파일을 구분한다.
            bound.arguments["args"] = command = tuple(command)
        else:
            command = None
        previous = getattr(_state.active, "environment", None)
        _state.active.environment = (environment, dict(environment), command)
        try:
            return original_init(*bound.args, **bound.kwargs)
        finally:
            _state.active.environment = previous

    _state.wrapper = guarded_init
    subprocess.Popen.__init__ = guarded_init


def worker_environment(environment):
    child = dict(os.environ if environment is None else environment)
    managed_names = (MARKER, "LOOP_MANAGER_RUNTIME_DEFAULT_PYCACHEPREFIX",
                     "LOOP_MANAGER_VERIFY_PROJECT_ROOT", "PYTHONPATH", "PYTHONIOENCODING",
                     "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")
    canonical = {name.casefold() if os.name == "nt" else name: name for name in managed_names}
    for key in tuple(child):
        if isinstance(key, (str, bytes)):
            name = os.fsdecode(key)
            target = canonical.get(name.casefold() if os.name == "nt" else name)
            if target is not None:
                child[target] = os.fsdecode(child.pop(key))
    manager_default_cache = child.pop("LOOP_MANAGER_RUNTIME_DEFAULT_PYCACHEPREFIX", None) == "1"
    if sys.dont_write_bytecode:
        child["PYTHONDONTWRITEBYTECODE"] = "1"
    if sys.pycache_prefix is not None and not manager_default_cache:
        child["PYTHONPYCACHEPREFIX"] = sys.pycache_prefix
    child[MARKER] = "1"
    child["PYTHONIOENCODING"] = "utf-8"
    root = child.get("LOOP_MANAGER_VERIFY_PROJECT_ROOT", str(Path(__file__).resolve().parents[2]))
    paths = [str(Path(__file__).parent / "child_guard"), root,
             *child.get("PYTHONPATH", "").split(os.pathsep)]
    child["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(filter(None, paths)))
    if os.name == "nt":
        coordination_names = {name.casefold() for name in _COORDINATION_ENV}
        for key in tuple(child):
            if isinstance(key, (str, bytes)) and os.fsdecode(key).casefold() in coordination_names:
                child.pop(key, None)
    else:
        for name in _COORDINATION_ENV:
            child.pop(name, None)
            child.pop(os.fsencode(name), None)
    return child
