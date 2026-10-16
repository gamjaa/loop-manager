"""실제 호출의 소유 자리와 확인된 종료 근거를 재시작 뒤에도 보존한다."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from uuid import uuid4


@dataclass(frozen=True)
class CallOwner:
    connection: object
    state_dir: Path
    token: str
    kind: str


current_call = ContextVar("task_call_owner", default=None)


class CallDeferred(RuntimeError):
    """확인되지 않은 기존 호출이 자리를 소유하므로 현재 단계만 보존한다."""


def ensure_schema(connection):
    from loop_manager_internal.storage.execution_checkpoint_state import SCHEMA as checkpoints_schema
    connection.execute(checkpoints_schema)
    connection.execute("""CREATE TABLE IF NOT EXISTS task_call_reservations (
        token TEXT PRIMARY KEY, repo TEXT NOT NULL, plan_id TEXT NOT NULL, kind TEXT NOT NULL,
        invocation_id TEXT, receipt_path TEXT, run_id TEXT, uncertain INTEGER NOT NULL DEFAULT 0,
        UNIQUE(repo,plan_id,kind))""")
    if "uncertain" not in {row[1] for row in connection.execute("PRAGMA table_info(task_call_reservations)")}:
        connection.execute("ALTER TABLE task_call_reservations ADD COLUMN uncertain INTEGER NOT NULL DEFAULT 0")


def reserve(connection, state_dir, repo, plan_id, kind, run_id):
    from .runtime_state import _transaction
    token = uuid4().hex
    with _transaction(connection):
        connection.execute("INSERT INTO task_call_reservations(token,repo,plan_id,kind,run_id) VALUES (?,?,?,?,?)",
                           (token,repo,plan_id,kind,run_id))
    return CallOwner(connection,Path(state_dir),token,kind)


def release(owner):
    with owner.connection:
        owner.connection.execute("DELETE FROM task_call_reservations WHERE token=?",(owner.token,))


def mark_uncertain(owner):
    with owner.connection:
        owner.connection.execute("UPDATE task_call_reservations SET uncertain=1 WHERE token=?",(owner.token,))


def bind_agent_receipt(path):
    owner=current_call.get()
    if owner is not None and owner.kind=="agent":
        with owner.connection:
            owner.connection.execute("UPDATE task_call_reservations SET receipt_path=? WHERE token=?",
                                     (str(path),owner.token))


def managed_count(connection, repo, plan_id=None):
    return connection.execute("""SELECT COUNT(DISTINCT CASE WHEN m.group_id IS NULL
        THEN 'plan:'||p.plan_id ELSE 'manual:'||m.group_id END)
        FROM exec_plans p LEFT JOIN manual_work_members m ON m.repo=p.repo AND m.plan_id=p.plan_id
        WHERE p.repo=? AND p.status <> 'PLAN_BLOCKED' AND (p.status IN ('IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING')
            OR EXISTS (SELECT 1 FROM task_call_reservations c WHERE c.repo=p.repo AND c.plan_id=p.plan_id)
            OR EXISTS (SELECT 1 FROM execution_checkpoints x WHERE x.repo=p.repo
                       AND x.plan_id=p.plan_id AND x.operation IS NOT NULL))
            AND (? IS NULL OR p.plan_id=? OR m.group_id=(SELECT group_id FROM manual_work_members
                 WHERE repo=p.repo AND plan_id=?))""",
        (repo,plan_id,plan_id,plan_id)).fetchone()[0]


def management_available(connection, repo, plan_id):
    from loop_manager_internal.config.managed_config import fast_mode_settings
    from loop_manager_internal.runtime.project_registry import task_limit
    config=fast_mode_settings.get()
    _,limit,_=task_limit(connection,repo,config.max_tasks_per_project if config else 1)
    return bool(managed_count(connection,repo,plan_id)) or managed_count(connection,repo)<limit


def bind_agent(invocation_id):
    owner = current_call.get()
    if owner is not None and owner.kind == "agent":
        with owner.connection:
            owner.connection.execute("UPDATE task_call_reservations SET invocation_id=? WHERE token=?",
                                     (invocation_id,owner.token))


def _write(path, value):
    from loop_manager_internal.verification.timing import atomic_json
    path.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(path,value)


def prepare_verify_receipt():
    owner = current_call.get()
    if owner is None or owner.kind != "verify":
        return None
    path = owner.state_dir / "logs/task-calls" / (owner.token+".json")
    _write(path,{"token":owner.token,"state":"launching","platform":sys.platform})
    with owner.connection:
        owner.connection.execute("UPDATE task_call_reservations SET receipt_path=? WHERE token=?",
                                 (str(path),owner.token))
    return path


def verify_started(path, process):
    if path is None:
        return
    value=json.loads(path.read_text(encoding="utf-8"))
    value.update(pid=process.pid,state="running")
    if sys.platform.startswith("linux"):
        from loop_manager_internal.agents.process_receipt import _linux_host, _boot
        value.update(host=_linux_host(),boot=_boot())
    _write(path,value)


def verify_stopped(path):
    if path is not None:
        value=json.loads(path.read_text(encoding="utf-8"))
        _write(path,{**value,"state":"stopped"})


def verify_waiting_for_wsl(path, directory):
    if path is not None:
        value=json.loads(path.read_text(encoding="utf-8"))
        _write(path,{**value,"state":"waiting_for_wsl_stop", "wsl_receipt":str(directory/"wsl-lifecycle.json")})


def _verify_recovered(row, state_dir):
    path=Path(row["receipt_path"])
    expected=Path(state_dir)/"logs/task-calls"/(row["token"]+".json")
    if path.absolute()!=expected.absolute() or path.is_symlink():
        return False
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        if value.get("token")!=row["token"] or value.get("platform")!=sys.platform:
            return False
        if value.get("state")=="stopped":
            return True
        if value.get("state")=="waiting_for_wsl_stop":
            wsl=Path(value["wsl_receipt"])
            if wsl.name=="wsl-lifecycle.json" and not wsl.is_symlink() and json.loads(wsl.read_text(encoding="utf-8")).get("state")=="stopped":
                verify_stopped(path)
                return True
            return False
        if sys.platform.startswith("linux") and type(value.get("pid")) is int and value['pid']>1:
            from loop_manager_internal.agents.process_receipt import _linux_host, _boot
            from loop_manager_internal.execution.verify_process import _confirm_posix_group_stopped
            if value.get("host")!=_linux_host():
                return False
            if not isinstance(value.get("boot"),str) or not value["boot"]:
                return False
            if value["boot"]==_boot():
                _confirm_posix_group_stopped(value["pid"])
            verify_stopped(path)
            return True
    except (OSError,ValueError,TypeError,RuntimeError):
        pass
    return False


def _confirmed(connection, row, state_dir):
    if row["receipt_path"] is None:
        return not row["uncertain"]  # 소유 launch를 연결하기 전에는 실제 자식을 시작하지 않는다.
    if row["kind"]=="verify":
        return _verify_recovered(row,state_dir)
    from loop_manager_internal.agents.process_receipt import recover as recover_agent
    if recover_agent(row["receipt_path"],Path(state_dir)) is not None:
        return True
    invocation=connection.execute("SELECT execution_status,execution_finish_evidence "
        "FROM agent_invocations WHERE invocation_id=?",(row["invocation_id"],)).fetchone()
    return bool(invocation and invocation[1])


def confirmed(owner):
    row=owner.connection.execute("SELECT * FROM task_call_reservations WHERE token=?",(owner.token,)).fetchone()
    return row is not None and _confirmed(owner.connection,row,owner.state_dir)


def recover(connection, state_dir):
    """미시작 또는 기존 프로세스 종료 근거가 있는 자리만 회수한다."""
    from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
    InvocationRepository(connection).reconcile_execution(state_dir=state_dir)
    rows=connection.execute("SELECT * FROM task_call_reservations").fetchall()
    for row in rows:
        if _confirmed(connection,row,state_dir):
            with connection:
                connection.execute("DELETE FROM task_call_reservations WHERE token=?",(row["token"],))
    return tuple(connection.execute("SELECT * FROM task_call_reservations"))
