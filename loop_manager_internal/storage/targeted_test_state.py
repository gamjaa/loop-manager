"""실제 targeted test 실행의 중복 없는 연속 실패 기록."""
from __future__ import annotations

from loop_manager_internal.storage.runtime_state import _transaction


def ensure_schema(connection):
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

    def observe(self, repo, plan_id, results):
        with _transaction(self.connection):
            for event in results:
                run_id, test_id, result = (event[key] for key in ('run_id', 'id', 'result'))
                if (not all(isinstance(value, str) and value.strip() for value in (run_id, test_id))
                        or result not in {'success', 'failure', 'error', 'skip', 'expected_failure', 'unexpected_success'}):
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
                previous = self.connection.execute('''SELECT failures, repeated FROM targeted_test_streaks
                    WHERE repo=? AND plan_id=? AND test_id=?''', (repo, plan_id, test_id)).fetchone()
                failures = ((previous[0] if previous else 0) + 1
                            if result in {'failure', 'error'} else 0)
                repeated = bool(failures >= 3 or (previous and previous[1]))
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
