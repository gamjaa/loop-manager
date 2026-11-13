"""모든 프로젝트의 공통 Telegram 큐. 전송 오류는 개발 상태를 바꾸지 않는다."""
import json
import time
from urllib.request import Request,urlopen

from loop_manager_internal.execution.validation_feedback import redact_secrets


def telegram(token,chat_id,message,silent):
    request = Request(f'https://api.telegram.org/bot{token}/sendMessage',
                      data=json.dumps({'chat_id':chat_id,'text':message,
                                       'disable_notification':silent}).encode(),
                      headers={'Content-Type':'application/json'},method='POST')
    with urlopen(request,timeout=15) as response:
        value = json.load(response)
    if value.get('ok') is not True:
        raise RuntimeError('Telegram이 전송 성공을 확인하지 않았습니다.')


def deliver(store,settings,*,transport=telegram):
    if not settings.telegram_token:
        with store.transaction() as db:
            db.execute("UPDATE notification_outbox SET state='CANCELLED',payload=NULL WHERE state='PENDING'")
        return
    for row in store.rows("SELECT * FROM notification_outbox WHERE state='PENDING' "
                          'AND (retry_after IS NULL OR retry_after<=?) ORDER BY rowid',(time.time(),)):
        message = redact_secrets(row['payload'] or '')
        parts,part,units=[],[],0
        for char in message:
            size=2 if ord(char)>0xffff else 1
            if units+size>3500:
                parts.append(''.join(part))
                part,units=[],0
            part.append(char)
            units+=size
        parts.append(''.join(part) or '상태 변경')
        try:
            for index in range(row['delivered_parts'],len(parts)):
                if not store.rows("SELECT 1 FROM notification_outbox WHERE event_key=? AND state='PENDING'",(row['event_key'],)):
                    break
                transport(settings.telegram_token,settings.telegram_chat,parts[index],bool(row['silent']))
                with store.transaction() as db:
                    db.execute("UPDATE notification_outbox SET delivered_parts=? WHERE event_key=? AND state='PENDING'",
                               (index+1,row['event_key']))
            with store.transaction() as db:
                db.execute("UPDATE notification_outbox SET state='SENT',payload=NULL,retry_after=NULL WHERE event_key=? AND state='PENDING'",
                           (row['event_key'],))
        except (OSError,ValueError,RuntimeError):
            with store.transaction() as db:
                db.execute("UPDATE notification_outbox SET attempts=attempts+1,retry_after=? WHERE event_key=? AND state='PENDING'",
                           (time.time()+min(3600,30*2**min(row['attempts'],7)),row['event_key']))
