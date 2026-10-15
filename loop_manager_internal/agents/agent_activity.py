"""저장·생략 전의 Agent 출력으로만 무출력 시간을 관측한다."""
import json
import codecs
import io
from contextvars import copy_context
import threading
import time

from loop_manager_internal.runtime import operation_events as events

SILENCE_SECONDS = 600
LINE_LIMIT = 256 * 1024


class AgentActivity:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.last_output = clock()
        self.active_items = set()
        self.turn_completed = False
        self.uncertain_items = False
        self.reason = None
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.thread = None
        self.reported_command = False

    def received(self):
        with self.lock:
            self.last_output = self.clock()
            self.reported_command = False

    def event(self, line):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(event, dict):
            return
        with self.lock:
            kind = event.get('type')
            item = event.get('item')
            if isinstance(item, dict) and isinstance(item.get('id'), str):
                if kind == 'item.started' and item.get('type') in {
                        'command_execution', 'mcp_tool_call', 'tool_call'}:
                    self.active_items.add(item['id'])
                elif kind == 'item.completed':
                    self.active_items.discard(item['id'])
            if kind == 'turn.completed':
                self.turn_completed = True
                self.active_items.clear()
                self.uncertain_items = False

    def oversized_event(self):
        with self.lock:
            self.uncertain_items = True

    def check(self, process, stop):
        with self.lock:
            if self.reason or self.clock() - self.last_output < SILENCE_SECONDS:
                return
            exited = process.poll() is not None
            if (self.active_items or self.uncertain_items) and not self.turn_completed and not exited:
                if not self.reported_command:
                    events.emit('Agent 무출력: 명령 실행 중, 전체 제한 시간까지 대기',
                                active_items=len(self.active_items), uncertain_items=self.uncertain_items)
                    self.reported_command = True
                return
            self.reason = '결과 수집 대기' if self.turn_completed or exited else 'Agent 무응답'
        events.emit('Agent 무출력 timeout', level='WARN', phase=self.reason,
                    silence_seconds=SILENCE_SECONDS)
        stop(process)

    def start(self, process, stop):
        def watch():
            while not self.cancelled.wait(1):
                self.check(process, stop)
                if self.reason:
                    return
        context = copy_context()
        self.thread = threading.Thread(target=lambda: context.run(watch), daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancelled.set()
        if self.thread is not None:
            self.thread.join()


def received_lines(stream, activity, *, bounded=False, limit=LINE_LIMIT):
    def chunks():
        if hasattr(stream, 'buffer'):
            decoder = io.IncrementalNewlineDecoder(
                codecs.getincrementaldecoder('utf-8')(errors='replace'), translate=True)
            while raw := stream.buffer.read1(64 * 1024):
                activity.received()
                yield decoder.decode(raw)
            yield decoder.decode(b'', final=True)
        else:
            while part := stream.readline(limit + 1):
                activity.received()
                yield part

    pending = ''
    oversized = False
    for chunk in chunks():
        for part in chunk.splitlines(keepends=True):
            if not oversized:
                if not bounded or len(pending) + len(part) <= limit:
                    pending += part
                else:
                    if (pending or part).lstrip().startswith('{'):
                        activity.oversized_event()
                    pending = ''
                    oversized = True
            if part.endswith('\n'):
                if not oversized:
                    activity.event(pending)
                yield None if oversized else pending
                pending, oversized = '', False
    if pending or oversized:
        if not oversized:
            activity.event(pending)
        yield None if oversized else pending
