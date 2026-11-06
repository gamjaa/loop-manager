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
    from .management_admission import ensure_schema as ensure_admission
    ensure_admission(connection)
    connection.execute("CREATE TABLE IF NOT EXISTS task_scheduling_cursor (kind TEXT PRIMARY KEY, repo TEXT NOT NULL)")
    from loop_manager_internal.storage.execution_checkpoint_state import SCHEMA as checkpoints_schema
    connection.execute(checkpoints_schema)
    connection.execute("""CREATE TABLE IF NOT EXISTS task_call_reservations (
        token TEXT PRIMARY KEY, repo TEXT NOT NULL, plan_id TEXT NOT NULL, kind TEXT NOT NULL,
        invocation_id TEXT, receipt_path TEXT, run_id TEXT, uncertain INTEGER NOT NULL DEFAULT 0,
        started INTEGER NOT NULL DEFAULT 0,
        UNIQUE(repo,plan_id,kind))""")
    if "uncertain" not in {row[1] for row in connection.execute("PRAGMA table_info(task_call_reservations)")}:
        connection.execute("ALTER TABLE task_call_reservations ADD COLUMN uncertain INTEGER NOT NULL DEFAULT 0")
    if "started" not in {row[1] for row in connection.execute("PRAGMA table_info(task_call_reservations)")}:
        connection.execute("ALTER TABLE task_call_reservations ADD COLUMN started INTEGER NOT NULL DEFAULT 0")


def reserve(connection, state_dir, repo, plan_id, kind, run_id):
    from .runtime_state import _transaction
    from .execplan_catalog_state import is_cancelled
    token = uuid4().hex
    with _transaction(connection):
        if is_cancelled(connection, repo, plan_id):
            raise CallDeferred(f'취소된 작업의 호출을 예약할 수 없습니다: {plan_id}')
        connection.execute("INSERT INTO task_call_reservations(token,repo,plan_id,kind,run_id) VALUES (?,?,?,?,?)",
                           (token,repo,plan_id,kind,run_id))
    return CallOwner(connection,Path(state_dir),token,kind)


def pending_for_plan(connection, repo, plan_id):
    """A collected result cannot release an unconfirmed physical call."""
    for table, pending in (
        ('task_call_reservations', '1'),
        ('agent_invocations', "COALESCE(finished_at,'')='' OR "
         "COALESCE(execution_status,'') NOT IN ('executed','not_executed') OR "
         "(execution_status='not_executed' AND execution_started_at IS NOT NULL) OR "
         "(execution_status='executed' AND "
         "(COALESCE(execution_started_at,'')='' OR COALESCE(execution_finished_at,'')='' "
         "OR COALESCE(execution_finish_evidence,'')=''))"),
    ):
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            if connection.execute(
                f'SELECT 1 FROM {table} WHERE repo=? AND plan_id=? AND ({pending}) LIMIT 1',
                (repo, plan_id),
            ).fetchone():
                return True
    return False


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
    admission = ("AND NOT EXISTS (SELECT 1 FROM task_management_admissions a WHERE a.repo=p.repo AND a.plan_id=p.plan_id AND a.parked=1)"
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='task_management_admissions'").fetchone() else "")
    return connection.execute(f"""SELECT COUNT(DISTINCT CASE WHEN m.group_id IS NULL
        THEN 'plan:'||p.plan_id ELSE 'manual:'||m.group_id END)
        FROM exec_plans p LEFT JOIN manual_work_members m ON m.repo=p.repo AND m.plan_id=p.plan_id
        WHERE p.repo=? {admission} AND p.status <> 'PLAN_BLOCKED' AND (p.status IN ('IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING')
            OR EXISTS (SELECT 1 FROM task_call_reservations c WHERE c.repo=p.repo AND c.plan_id=p.plan_id)
            OR EXISTS (SELECT 1 FROM execution_checkpoints x WHERE x.repo=p.repo
                       AND x.plan_id=p.plan_id AND x.operation IS NOT NULL))
            AND (? IS NULL OR p.plan_id=? OR m.group_id=(SELECT group_id FROM manual_work_members
                 WHERE repo=p.repo AND plan_id=?))""",
        (repo,plan_id,plan_id,plan_id)).fetchone()[0]


def management_available(connection, repo, plan_id):
    from loop_manager_internal.config.managed_config import fast_mode_settings
    from loop_manager_internal.runtime.project_registry import task_limit
    from .management_admission import parked
    if parked(connection, repo, plan_id):
        return False
    config=fast_mode_settings.get()
    _,limit,_=task_limit(connection,repo,config.max_tasks_per_project if config else 1)
    return bool(managed_count(connection,repo,plan_id)) or managed_count(connection,repo)<limit


def bind_agent(invocation_id):
    owner = current_call.get()
    if owner is not None and owner.kind == "agent":
        with owner.connection:
            owner.connection.execute("UPDATE task_call_reservations SET invocation_id=? WHERE token=?",
                                     (invocation_id,owner.token))


def mark_started(connection, *, token=None, invocation_id=None):
    """Consume this reservation's retry once, only after a proven process start."""
    from .runtime_state import _transaction
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='task_call_reservations'").fetchone() is None:
        return
    with _transaction(connection):
        row = connection.execute("SELECT token,repo,plan_id,kind FROM task_call_reservations "
            "WHERE started=0 AND ((? IS NOT NULL AND token=?) OR "
            "(? IS NOT NULL AND invocation_id=?))", (token,token,invocation_id,invocation_id)).fetchone()
        if row is None:
            return
        connection.execute("UPDATE task_call_reservations SET started=1 WHERE token=?", (row['token'],))
        connection.execute("INSERT INTO task_management_admissions(repo,plan_id,parked,resumed) VALUES (?,?,0,1) "
            "ON CONFLICT(repo,plan_id) DO UPDATE SET resumed=1,retry_order=NULL", (row['repo'],row['plan_id']))
        connection.execute("INSERT INTO task_scheduling_cursor VALUES (?,?) "
            "ON CONFLICT(kind) DO UPDATE SET repo=excluded.repo", (row['kind'],row['repo']))


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
    owner = current_call.get()
    if owner is not None:
        mark_started(owner.connection, token=owner.token)


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
    proof = recover_agent(row["receipt_path"],Path(state_dir))
    if proof is not None:
        if proof.get('evidence') == 'not_started' and row['invocation_id'] is not None:
            invocation = connection.execute('SELECT execution_started_at FROM agent_invocations '
                'WHERE invocation_id=?', (row['invocation_id'],)).fetchone()
            return invocation is not None and invocation[0] is None
        return True
    invocation=connection.execute("SELECT execution_status,execution_finish_evidence "
        "FROM agent_invocations WHERE invocation_id=?",(row["invocation_id"],)).fetchone()
    return bool(invocation and invocation[1])


def execution_confirmed(connection, row, state_dir):
    """Physical termination is independent of pending observation persistence."""
    return _confirmed(connection, row, state_dir)


def _recover_verify_start(connection, row, state_dir):
    if row['kind'] != 'verify' or not row['receipt_path']:
        return
    path = Path(row['receipt_path'])
    expected = Path(state_dir) / 'logs/task-calls' / (row['token'] + '.json')
    try:
        value = json.loads(path.read_text(encoding='utf-8')) if path.absolute() == expected.absolute() and not path.is_symlink() else {}
        if value.get('token') == row['token'] and value.get('platform') == sys.platform and type(value.get('pid')) is int and value['pid'] > 1:
            mark_started(connection, token=row['token'])
    except (OSError, ValueError, TypeError):
        pass


def _recover_agent_start(connection, row, state_dir):
    if row['kind'] != 'agent' or not row['receipt_path'] or row['started']:
        return
    from loop_manager_internal.agents.process_receipt import recover as recover_agent
    proof = recover_agent(row['receipt_path'], Path(state_dir))
    if proof is not None and proof.get('evidence') != 'not_started':
        mark_started(connection, token=row['token'])


def confirmed(owner):
    from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
    row=owner.connection.execute("SELECT * FROM task_call_reservations WHERE token=?",(owner.token,)).fetchone()
    if row is not None:
        _recover_verify_start(owner.connection, row, owner.state_dir)
    pending = (InvocationRepository(owner.connection).reconcile_observations(
        state_dir=owner.state_dir, invocation_id=row['invocation_id'])
        if row is not None and row['invocation_id'] is not None else set())
    if row is not None and row['invocation_id'] not in pending:
        _recover_agent_start(owner.connection, row, owner.state_dir)
    return row is not None and row['invocation_id'] not in pending and execution_confirmed(owner.connection,row,owner.state_dir)


def recover(connection, state_dir, *, tokens=None):
    """미시작 또는 기존 프로세스 종료 근거가 있는 자리만 회수한다."""
    from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
    repository = InvocationRepository(connection)
    rows=connection.execute("SELECT * FROM task_call_reservations").fetchall()
    if tokens is not None:
        rows = [row for row in rows if row['token'] in tokens]
    pending = set()
    for row in rows:
        if row['kind'] == 'agent' and row['invocation_id'] is not None:
            pending.update(repository.reconcile_observations(state_dir=state_dir, invocation_id=row['invocation_id']))
        elif row['kind'] == 'verify':
            _recover_verify_start(connection, row, state_dir)
    repository.reconcile_execution(state_dir=state_dir)
    for row in rows:
        if row['invocation_id'] not in pending and execution_confirmed(connection,row,state_dir):
            _recover_agent_start(connection, row, state_dir)
            with connection:
                connection.execute("DELETE FROM task_call_reservations WHERE token=?",(row["token"],))
    return tuple(row for row in connection.execute("SELECT * FROM task_call_reservations")
                 if tokens is None or row['token'] in tokens)
