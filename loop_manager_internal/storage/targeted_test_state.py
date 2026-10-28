"""실제 targeted test 실행의 중복 없는 연속 실패 기록."""
from __future__ import annotations

from pathlib import Path
import sqlite3

from loop_manager_internal.storage.runtime_state import _transaction
from loop_manager_internal.storage.db_access import CoordinatedConnection


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS targeted_test_collections (
        invocation_id TEXT PRIMARY KEY REFERENCES agent_invocations(invocation_id),
        directory TEXT NOT NULL UNIQUE, device TEXT NOT NULL, inode TEXT NOT NULL,
        receipt TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending', error TEXT)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS targeted_test_run_owners (
        run_id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL
        REFERENCES targeted_test_collections(invocation_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS targeted_test_results (
        sequence INTEGER PRIMARY KEY, repo TEXT NOT NULL, plan_id TEXT NOT NULL,
        run_id TEXT NOT NULL, test_id TEXT NOT NULL, result TEXT NOT NULL,
        UNIQUE(repo, plan_id, run_id, test_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS targeted_test_streaks (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, test_id TEXT NOT NULL,
        failures INTEGER NOT NULL, repeated INTEGER NOT NULL,
        PRIMARY KEY(repo, plan_id, test_id))''')


class TargetedTestRepository:
    def __init__(self, connection):
        self.connection = connection

    def bind(self, identity, directory, receipt, *, state_dir):
        """Persist the physical writer's private directory before process creation."""
        root, proof = Path(directory).absolute(), Path(receipt).absolute()
        base = Path(state_dir).resolve() / 'agent-runtime'
        if (root.resolve() != root or not root.is_dir() or root.parent.parent != base
                or proof != root.parent / (root.name + '.process.json')):
            raise ValueError('targeted test 기록의 호출 경로가 유효하지 않습니다.')
        info = root.stat()
        def bind(current):
            with _transaction(current):
                owner = current.execute('''SELECT role, plan_id FROM agent_invocations
                    WHERE invocation_id=? AND execution_started_at IS NULL AND finished_at IS NULL''',
                    (identity,)).fetchone()
                if owner is None or owner[0] != 'worker' or not owner[1]:
                    raise ValueError('targeted test 기록의 Worker 소유권이 없습니다.')
                current.execute('''INSERT INTO targeted_test_collections
                    (invocation_id, directory, device, inode, receipt) VALUES (?, ?, ?, ?, ?)''',
                    (identity, str(root), str(info.st_dev), str(info.st_ino), str(proof)))
        if isinstance(self.connection, CoordinatedConnection) and not self.connection.in_transaction:
            self.connection.run_request(bind)
        else:
            # An explicit caller transaction retains rollback/retry ownership.
            bind(self.connection)

    def pending(self, repo, plan_id):
        return bool(self.connection.execute('''SELECT 1 FROM targeted_test_collections c
            JOIN agent_invocations a USING (invocation_id)
            WHERE a.repo=? AND a.plan_id=? AND c.status!='collected' LIMIT 1''',
            (repo, plan_id)).fetchone())

    def reconcile(self, *, state_dir, repo=None, plan_id=None):
        """Replay only bound, stopped writers; preserve unconsumed evidence on error."""
        from loop_manager_internal.agents.process_receipt import recover
        from loop_manager_internal.storage.work_claims import live_manual_tasks
        from loop_manager_internal.verification.targeted import collect
        protected = live_manual_tasks(self.connection, state_dir)
        rows = self.connection.execute('''SELECT c.invocation_id, c.directory, c.device,
            c.inode, c.receipt, a.repo, a.plan_id, a.guidance_task_id, a.process_receipt_path, a.execution_started_at
            FROM targeted_test_collections c JOIN agent_invocations a USING (invocation_id)
            WHERE c.status!='collected' AND (? IS NULL OR a.repo=?)
            AND (? IS NULL OR a.plan_id=?) ORDER BY a.rowid''',
            (repo, repo, plan_id, plan_id)).fetchall()
        waiting = set()
        for identity, directory, device, inode, receipt, owner_repo, owner_plan, task, bound_receipt, started_at in rows:
            owner = (owner_repo, owner_plan)
            if owner in waiting:
                continue
            try:
                root = Path(directory)
                if (root.resolve() != root or not root.is_dir()
                        or root.parent.parent != Path(state_dir).resolve() / 'agent-runtime'
                        or receipt != str(root.parent / (root.name + '.process.json'))
                        or receipt != bound_receipt
                        or (str(root.stat().st_dev), str(root.stat().st_ino)) != (device, inode)):
                    raise ValueError('targeted test 기록 경로가 교체되었거나 호출 결합이 다릅니다.')
                proof = None if task in protected else recover(receipt, Path(state_dir))
                if (proof is None or proof.get('evidence') == 'not_started' and started_at is not None):
                    waiting.add(owner)
                    continue
                results = collect(root)
                if root.resolve() != root or (str(root.stat().st_dev), str(root.stat().st_ino)) != (device, inode):
                    raise ValueError('targeted test 수집 중 기록 경로가 교체되었습니다.')
                with _transaction(self.connection):
                    for run_id in {event['run_id'] for event in results}:
                        old = self.connection.execute('''SELECT invocation_id
                            FROM targeted_test_run_owners WHERE run_id=?''', (run_id,)).fetchone()
                        if old is not None and old[0] != identity:
                            raise ValueError('targeted test 실행 ID가 다른 호출에 속합니다.')
                        self.connection.execute('''INSERT OR IGNORE INTO targeted_test_run_owners
                            VALUES (?, ?)''', (run_id, identity))
                    self.observe(owner_repo, owner_plan, results)
                    self.connection.execute('''UPDATE targeted_test_collections
                        SET status='collected', error=NULL WHERE invocation_id=?''', (identity,))
            except (OSError, ValueError, sqlite3.Error) as error:
                waiting.add(owner)
                # The original binding itself is the retry journal if this write fails too.
                try:
                    with _transaction(self.connection):
                        self.connection.execute('''UPDATE targeted_test_collections
                            SET status='error', error=? WHERE invocation_id=?''',
                            (type(error).__name__, identity))
                except sqlite3.Error:
                    pass

    def observe(self, repo, plan_id, results):
        with _transaction(self.connection):
            for event in results:
                run_id, test_id, result = (event[key] for key in ('run_id', 'id', 'result'))
                if (not all(isinstance(value, str) and value.strip() for value in (run_id, test_id))
                        or result not in {'success', 'failure', 'error', 'skip', 'expected_failure',
                                          'unexpected_success', 'fixture_error', 'indeterminate'}):
                    raise ValueError('targeted test 실행 결과가 유효하지 않습니다.')
                old = self.connection.execute('''SELECT result FROM targeted_test_results
                    WHERE repo=? AND plan_id=? AND run_id=? AND test_id=?''',
                    (repo, plan_id, run_id, test_id)).fetchone()
                if old is not None:
                    if old[0] != result:
                        raise ValueError('같은 targeted test 실행의 결과가 상충합니다.')
                    continue
                self.connection.execute('''INSERT INTO targeted_test_results
                    (repo, plan_id, run_id, test_id, result) VALUES (?, ?, ?, ?, ?)''',
                    (repo, plan_id, run_id, test_id, result))
                if result in {'fixture_error', 'indeterminate'}:
                    continue
                previous = self.connection.execute('''SELECT failures FROM targeted_test_streaks
                    WHERE repo=? AND plan_id=? AND test_id=?''', (repo, plan_id, test_id)).fetchone()
                failures = ((previous[0] if previous else 0) + 1
                            if result in {'failure', 'error', 'unexpected_success'} else 0)
                repeated = failures >= 5
                self.connection.execute('''INSERT INTO targeted_test_streaks VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(repo, plan_id, test_id) DO UPDATE SET
                    failures=excluded.failures, repeated=excluded.repeated''',
                    (repo, plan_id, test_id, failures, repeated))

    def repeated(self, repo, plan_id):
        return tuple(row[0] for row in self.connection.execute('''SELECT test_id FROM targeted_test_streaks
            WHERE repo=? AND plan_id=? AND repeated=1 ORDER BY test_id''', (repo, plan_id)))

    def sequence(self, repo, plan_id):
        return self.connection.execute('''SELECT COALESCE(MAX(sequence), 0) FROM targeted_test_results
            WHERE repo=? AND plan_id=?''', (repo, plan_id)).fetchone()[0]

    def acknowledge(self, repo, plan_id):
        self.connection.execute('''UPDATE targeted_test_streaks SET repeated=0
            WHERE repo=? AND plan_id=?''', (repo, plan_id))
