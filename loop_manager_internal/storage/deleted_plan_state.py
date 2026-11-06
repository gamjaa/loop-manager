"""Remove a proven withdrawn task; retain only its non-reusable identity."""
from __future__ import annotations

import json

from .runtime_state import _transaction
from .task_calls import CallDeferred, pending_for_plan


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS deleted_plan_ids (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, PRIMARY KEY (repo, plan_id))''')

    connection.execute('''CREATE TABLE IF NOT EXISTS deleted_plan_files (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, path TEXT NOT NULL,
        PRIMARY KEY(repo,plan_id,path))''')


def assert_no_historical_merge(connection, repo, plan_id):
    """A removed execution row/archive file cannot erase durable Merge proof."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_catalog_history'").fetchone():
        history = connection.execute('SELECT merge_confirmed FROM execplan_catalog_history WHERE repo=? AND plan_id=?',
                                     (repo, plan_id)).fetchone()
        if history is not None and history['merge_confirmed']:
            raise CallDeferred('실행 row 밖의 확인된 Merge 이력은 최종 제거할 수 없습니다.')
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_archive_observations'").fetchone() and connection.execute(
            "SELECT 1 FROM execplan_archive_observations WHERE repo=? AND scope=? AND category='complete'",
            (repo, plan_id)).fetchone():
        raise CallDeferred('완료된 archive 관측은 최종 제거할 수 없습니다.')


def forget(connection, repo, plan_id, *, artifacts=(), diagnostic_snapshot=None, historical_dependencies=()):
    """Caller holds resource leases and has finished physical cleanup and publication."""
    with _transaction(connection):
        assert_no_historical_merge(connection, repo, plan_id)
        tables = {row[0]: {column[1] for column in connection.execute(f'PRAGMA table_info("{row[0]}")')}
                  for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
        if plan is not None and (plan['status'] == 'MERGED' or plan['agent_role']):
            raise ValueError('완료된 작업 또는 실행 writer의 기록은 제거할 수 없습니다.')
        if pending_for_plan(connection, repo, plan_id):
            raise CallDeferred('호출 예약 또는 writer의 실제 종료·결과 인계가 미확정인 작업은 제거할 수 없습니다.')
        from .execplan_catalog_state import verified_merged_archive_execution_ids
        rows = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id<>?', (repo, plan_id)).fetchall()
        historical = verified_merged_archive_execution_ids(connection, repo, rows, include_missing=True)
        observed_history = {item['plan_id']: item for item in historical_dependencies}
        for row in rows:
            if plan_id not in json.loads(row['depends_on']):
                continue
            if row['plan_id'] in observed_history:
                if dict(row) != observed_history[row['plan_id']] or row['status'] != 'MERGED':
                    raise CallDeferred('과거 Merge 참조의 원문/실행 identity가 변경됐습니다.')
            elif row['plan_id'] not in historical:
                raise ValueError('삭제된 작업의 선행 관계가 아직 남아 있습니다.')
        from . import deleted_diagnostic_state
        observed_diagnostics = deleted_diagnostic_state.capture(connection, repo, plan_id)
        if diagnostic_snapshot is None:
            if observed_diagnostics['refs'] or observed_diagnostics['worktrees']:
                raise CallDeferred('진단 ref·worktree의 현재 부재를 확인한 뒤 기록을 제거해야 합니다.')
            diagnostic_snapshot = observed_diagnostics
        deleted_diagnostic_state.forget(connection, repo, plan_id, diagnostic_snapshot)
        from .manual_work_state import forget_plan as forget_manual_plan
        forget_manual_plan(connection, repo, plan_id)
        from .common_defect_state import assert_plan_erasure_available
        assert_plan_erasure_available(connection, repo, plan_id)
        from .maintenance_state import forget_plan as forget_maintenance_plan
        forget_maintenance_plan(connection, repo, plan_id)
        if 'execplan_archive_observations' in tables:
            resource_owner = plan
            if resource_owner is None and 'execplan_cancellations' in tables:
                canceled = connection.execute('SELECT recovery_evidence FROM execplan_cancellations WHERE repo=? AND plan_id=?',
                                              (repo, plan_id)).fetchone()
                if canceled is not None:
                    try:
                        saved = json.loads(canceled['recovery_evidence'])['row']
                        if (not isinstance(saved, dict) or (saved['repo'], saved['plan_id']) != (repo, plan_id)
                                or any(field not in saved or saved[field] is not None and not isinstance(saved[field], str)
                                       for field in ('branch', 'worktree'))):
                            raise ValueError('unattributed cancellation resources')
                        resource_owner = saved
                    except (KeyError, TypeError, ValueError) as error:
                        raise CallDeferred('취소 감사의 archive 자원 귀속을 확인할 수 없습니다.') from error
            resources = {resource_owner[key] for key in ('branch', 'worktree')
                         if resource_owner is not None and resource_owner[key]}
            owner_column = 'owner_plan_id' in tables['execplan_archive_observations']
            for observation in connection.execute(
                'SELECT * FROM execplan_archive_observations WHERE repo=?', (repo,),
            ).fetchall():
                owner = observation['owner_plan_id'] if owner_column else None
                if observation['scope'] == plan_id and observation['category'] == 'complete':
                    raise CallDeferred('완료된 archive 관측은 삭제 확정 대상에서 보호합니다.')
                if (observation['scope'].startswith('git:') and owner is None
                        and observation['resource'] in resources):
                    raise CallDeferred('삭제 대상 자원의 archive Git 관측 귀속을 확인할 수 없습니다.')
                if observation['scope'] == plan_id or owner == plan_id:
                    connection.execute('DELETE FROM execplan_archive_observations WHERE repo=? AND scope=?',
                                       (repo, observation['scope']))
        calls = [row[0] for row in connection.execute('SELECT invocation_id FROM agent_invocations WHERE repo=? AND plan_id=?', (repo, plan_id))]
        # Audit immutability remains in force outside this explicit erasure transaction.
        triggers = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('validation_results_delete_immutable','validation_feedback_events_delete_immutable',"
            "'common_defect_reports_delete_immutable','common_defect_links_delete_immutable',"
            "'common_defect_merges_delete_immutable')").fetchall()
        for trigger in triggers:
            connection.execute(f'DROP TRIGGER "{trigger[0]}"')
        recovery_requests = set()
        if 'local_environment_recovery_requests' in tables:
            from .local_recovery_state import forget_plan
            recovery_requests = forget_plan(connection, repo, plan_id)
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
        incident_events = set()
        if 'notified_block_incidents' in tables:
            incident_events.update(row[0] for row in connection.execute(
                'SELECT event_key FROM notified_block_incidents WHERE repo=? AND subject IN (?,?)',
                (repo, plan_id, 'plan:' + plan_id),
            ))
            if 'block_notice_events' in tables:
                incident_events.update(row[0] for row in connection.execute(
                    'SELECT n.event_key FROM block_notice_events n JOIN notified_block_incidents i ON i.id=n.incident_id '
                    'WHERE i.repo=? AND i.subject IN (?,?)', (repo, plan_id, 'plan:' + plan_id),
                ))
                connection.execute('DELETE FROM block_notice_events WHERE incident_id IN '
                    '(SELECT id FROM notified_block_incidents WHERE repo=? AND subject IN (?,?))',
                    (repo, plan_id, 'plan:' + plan_id))
        for table in ('recovery_records', 'persistent_execution_failures', 'notified_block_incidents'):
            if table in tables:
                connection.execute(f'DELETE FROM "{table}" WHERE repo=? AND subject IN (?,?)',
                                   (repo, plan_id, 'plan:' + plan_id))
        if 'notification_outbox' in tables:
            for event in connection.execute('SELECT * FROM notification_outbox WHERE repo=?', (repo,)).fetchall():
                if event['event_key'] in incident_events:
                    owned_event = True
                else:
                    try:
                        payload = json.loads(event['payload'])
                    except (TypeError, ValueError) as error:
                        raise CallDeferred('알림의 삭제 대상 귀속을 확인할 수 없습니다.') from error
                    if not isinstance(payload, dict):
                        raise CallDeferred('알림의 삭제 대상 귀속을 확인할 수 없습니다.')
                    if event['event_type'] in ('plans_published', 'draft_decisions', 'planning_result'):
                        if event['event_type'] == 'planning_result' and (payload.get('execplan_id') or payload.get('started_execplan_id')):
                            raise CallDeferred('과거 planning 알림의 작업·프로젝트 귀속을 분리할 수 없습니다.')
                        plans = payload.get('plans')
                        if not isinstance(plans, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in plans):
                            raise CallDeferred('공유 알림의 삭제 대상 귀속을 확인할 수 없습니다.')
                        owned_event = any(item['id'] == plan_id for item in plans)
                        if owned_event and any(item['id'] != plan_id for item in plans):
                            retained = {**payload, 'plans': [item for item in plans if item['id'] != plan_id]}
                            rendered = None
                            if (event['attempts'] == 0 and event['confirmed_pieces'] == 0
                                    and event['rendered_text'] is None and event['delivery_started_at'] is None
                                    and event['sent_at'] is None):
                                pass  # No frozen body or transmission exists to remap.
                            else:
                                from loop_manager_internal.notifications.rendering import _message, _pieces
                                try:
                                    original = _message(event['event_type'], payload)
                                    rendered = _message(event['event_type'], retained)
                                except (KeyError, TypeError, ValueError) as error:
                                    raise CallDeferred('공유 알림의 원래 전송 본문을 확인할 수 없습니다.') from error
                                if (event['rendered_text'] != original
                                        or len(rendered) > len(original)):
                                    raise CallDeferred('공유 알림의 원래 본문·전송 확인을 증명할 수 없습니다.')
                                if event['sent_at'] is not None:
                                    if event['confirmed_pieces'] < len(_pieces(original)):
                                        raise CallDeferred('공유 알림의 전체 전송 확인을 증명할 수 없습니다.')
                                elif (event['attempts'] > 0 and 0 <= event['confirmed_pieces'] < len(_pieces(original))
                                        and event['delivery_started_at'] is None and event['canceled_at'] is None
                                        and isinstance(event['last_error'], str) and event['last_error'].strip()):
                                    confirmed = ''.join(_pieces(original)[:event['confirmed_pieces']])
                                    if not rendered.startswith(confirmed) or len(rendered) <= len(confirmed):
                                        raise CallDeferred('공유 알림의 확인된 조각과 미전송 잔여분을 보존할 수 없습니다.')
                                else:
                                    raise CallDeferred('부분 전송된 공유 알림은 대상별 전송 근거 분리가 필요합니다.')
                            connection.execute('UPDATE notification_outbox SET payload=?,rendered_text=? WHERE event_key=?',
                                (json.dumps(retained, ensure_ascii=False), rendered, event['event_key']))
                            owned_event = False
                    elif event['event_type'] == 'recovery_unresolved':
                        owned_event = payload.get('request_id') in recovery_requests
                    else:
                        owned_event = payload.get('execplan_id') == plan_id
                        if payload.get('scope') == 'project' and (owned_event or payload.get('started_execplan_id') == plan_id):
                            raise CallDeferred('프로젝트 알림의 삭제 대상과 독립 사건을 분리할 수 없습니다.')
                if owned_event:
                    if 'block_notice_events' in tables:
                        if connection.execute('SELECT 1 FROM block_notice_events WHERE event_key=?', (event['event_key'],)).fetchone():
                            raise CallDeferred('삭제할 알림이 독립 차단 사건에도 연결돼 있습니다.')
                    connection.execute('DELETE FROM notification_outbox WHERE event_key=?', (event['event_key'],))
            if any(connection.execute('SELECT 1 FROM notification_outbox WHERE event_key=? AND repo<>?', (key, repo)).fetchone()
                   for key in incident_events):
                raise CallDeferred('삭제 대상 차단 사건의 알림 repository가 일치하지 않습니다.')
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

