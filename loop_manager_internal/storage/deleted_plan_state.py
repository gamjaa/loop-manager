"""Remove a proven withdrawn task; retain only its non-reusable identity."""
from __future__ import annotations

import json

from .runtime_state import _transaction


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS deleted_plan_ids (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, PRIMARY KEY (repo, plan_id))''')

    connection.execute('''CREATE TABLE IF NOT EXISTS deleted_plan_files (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, path TEXT NOT NULL,
        PRIMARY KEY(repo,plan_id,path))''')


def forget(connection, repo, plan_id, *, artifacts=()):
    """Caller holds resource leases and has finished physical cleanup and publication."""
    with _transaction(connection):
        tables = {row[0]: {column[1] for column in connection.execute(f'PRAGMA table_info("{row[0]}")')}
                  for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
        if plan is not None and (plan['status'] == 'MERGED' or plan['agent_role']):
            raise ValueError('완료된 작업 또는 실행 writer의 기록은 제거할 수 없습니다.')
        for row in connection.execute('SELECT plan_id,depends_on FROM exec_plans WHERE repo=? AND plan_id<>?', (repo, plan_id)):
            if plan_id in json.loads(row['depends_on']):
                raise ValueError('삭제된 작업의 선행 관계가 아직 남아 있습니다.')
        if connection.execute('SELECT 1 FROM manual_work_members WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone():
            raise ValueError('수동 소유 작업은 자동 제거할 수 없습니다.')
        calls = [row[0] for row in connection.execute('SELECT invocation_id FROM agent_invocations WHERE repo=? AND plan_id=?', (repo, plan_id))]
        # Audit immutability remains in force outside this explicit erasure transaction.
        triggers = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('validation_results_delete_immutable','validation_feedback_events_delete_immutable',"
            "'common_defect_reports_delete_immutable','common_defect_links_delete_immutable',"
            "'common_defect_merges_delete_immutable')").fetchall()
        for trigger in triggers:
            connection.execute(f'DROP TRIGGER "{trigger[0]}"')
        if 'validation_delivery_items' in tables:
            connection.execute('''DELETE FROM validation_delivery_items WHERE attempt_id IN
                (SELECT id FROM validation_delivery_attempts WHERE repo=? AND plan_id=?)
                OR sequence IN (SELECT sequence FROM validation_feedback_events WHERE repo=? AND plan_id=?)''',
                (repo, plan_id, repo, plan_id))
        for table in ('common_defect_reports', 'common_defect_links'):
            if table in tables:
                connection.execute(f'DELETE FROM "{table}" WHERE result_id IN '
                    '(SELECT id FROM validation_results WHERE repo=? AND plan_id=?)', (repo, plan_id))
        if 'common_defect_links' in tables:
            connection.execute('DELETE FROM common_defect_links WHERE repo=? AND correction_plan_id=?', (repo, plan_id))
        # Invocation children lack repo/plan columns; delete before their parent.
        for table in ('targeted_test_run_owners', 'targeted_test_collections', 'agent_isolation_evidence'):
            if table in tables:
                connection.executemany(f'DELETE FROM "{table}" WHERE invocation_id=?', ((key,) for key in calls))
        for table in ('recovery_records', 'persistent_execution_failures', 'notified_block_incidents'):
            if table in tables:
                connection.execute(f'DELETE FROM "{table}" WHERE repo=? AND subject IN (?,?)',
                                   (repo, plan_id, 'plan:' + plan_id))
        if 'diagnostic_preservations' in tables:
            connection.execute("DELETE FROM diagnostic_preservations WHERE repo=? AND scope='plan' AND owner=?", (repo, plan_id))
        if 'notification_outbox' in tables:
            for event in connection.execute('SELECT event_key,payload FROM notification_outbox WHERE repo=?', (repo,)).fetchall():
                payload = json.loads(event['payload'])
                if payload.get('execplan_id') == plan_id:
                    connection.execute('DELETE FROM notification_outbox WHERE event_key=?', (event['event_key'],))
        if 'manager_metadata' in tables:
            for prefix in ('workspace_seen:', 'remote_adoption:'):
                connection.execute('DELETE FROM manager_metadata WHERE key=?', (prefix + repo + ':' + plan_id,))
        if plan is not None:
            project = connection.execute('SELECT plan_path FROM projects WHERE repo=?', (repo,)).fetchone()
            if project and project[0] == plan['path']:
                from .runtime_state import RuntimeStateRepository
                RuntimeStateRepository(connection).clear(repo)
        # All task stores use this exact ownership pair, including usage and audits.
        owned = [table for table, columns in tables.items()
                 if {'repo', 'plan_id'} <= columns and table not in {'exec_plans', 'deleted_plan_ids', 'deleted_plan_files'}]
        for table in sorted(owned, key=lambda name: name == 'validation_results'):
            connection.execute(f'DELETE FROM "{table}" WHERE repo=? AND plan_id=?', (repo, plan_id))
        connection.execute('DELETE FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id))
        connection.execute('INSERT OR IGNORE INTO deleted_plan_ids VALUES (?,?)', (repo, plan_id))
        connection.executemany('INSERT OR IGNORE INTO deleted_plan_files VALUES (?,?,?)',
                               ((repo, plan_id, str(path)) for path in artifacts))
        for trigger in triggers:
            connection.execute(trigger[1])

