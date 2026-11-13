from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from loop_manager_internal.storage.state import Store
from loop_manager_internal.notifications.outbox import deliver


class OutboxTests(unittest.TestCase):
    def test_retry_continues_after_confirmed_part_and_clears_sent_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'loop-manager.db')
            store.initialize()
            store.register('owner/repo','/project','remote','main','codex')
            with store.transaction() as db:
                store.notify(db,'event','owner/repo','G-001','a'*4000)
                store.notify(db,'event','owner/repo','G-001','중복')
            settings=SimpleNamespace(telegram_token='token',telegram_chat='chat')
            transport=Mock(side_effect=[None,OSError('일시 실패')])
            deliver(store,settings,transport=transport)
            row=store.rows('SELECT * FROM notification_outbox')[0]
            self.assertEqual(row['state'],'PENDING')
            self.assertEqual(row['delivered_parts'],1)
            with store.transaction() as db:
                db.execute('UPDATE notification_outbox SET retry_after=NULL')
            transport=Mock()
            deliver(store,settings,transport=transport)
            self.assertEqual(transport.call_count,1)
            self.assertEqual(len(transport.call_args.args[2]),500)
            row=store.rows('SELECT * FROM notification_outbox')[0]
            self.assertEqual(row['state'],'SENT')
            self.assertIsNone(row['payload'])
            deliver(store,settings,transport=transport)
            self.assertEqual(transport.call_count,1)
