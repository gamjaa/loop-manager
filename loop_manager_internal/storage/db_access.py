"""운영 DB 연결의 짧은 처리와 transaction을 파일별로 순서대로 실행한다."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4


@contextmanager
def _waiting():
    from loop_manager_internal.verification.execution_clock import current_execution_clock
    clock = current_execution_clock.get()
    started = time.monotonic()
    try:
        yield
    finally:
        if clock is not None and not clock.active_processes:
            clock.local_wait += time.monotonic() - started


class DatabaseWaitTimeout(sqlite3.OperationalError):
    """DB 차례를 기다렸지만 상태를 변경하지 못했다."""


class _Gate:
    def __init__(self, path):
        self.path = path
        self.condition = threading.Condition()
        self.queue = deque()
        self.owner = None
        self.thread = None
        self.handle = None

    def acquire(self, owner, deadline):
        ticket = object()
        with self.condition:
            if self.thread == threading.get_ident():
                raise RuntimeError('DB 처리 중 다른 연결의 차례를 기다릴 수 없습니다.')
            self.queue.append(ticket)
            try:
                while self.owner is not None or self.queue[0] is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DatabaseWaitTimeout('DB 접근 순번 대기 시간이 초과됐습니다.')
                    from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
                    _check_runtime_handoff()
                    self.condition.wait(min(remaining, .05))
                self.owner, self.thread = owner, threading.get_ident()
            finally:
                self.queue.remove(ticket)
                self.condition.notify_all()
        try:
            self.handle = open(str(self.path) + '.access.lock', 'a+b')
            if os.name == 'nt' and self.handle.seek(0, os.SEEK_END) == 0:
                self.handle.write(b'0')
                self.handle.flush()
            while True:
                try:
                    self.handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN) and getattr(error, 'winerror', None) not in (32, 33):
                        raise
                    if time.monotonic() >= deadline:
                        raise DatabaseWaitTimeout('다른 프로세스의 DB 접근 종료 대기 시간이 초과됐습니다.') from error
                    from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
                    _check_runtime_handoff()
                    time.sleep(min(.05, max(0, deadline - time.monotonic())))
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        with self.condition:
            self.owner = self.thread = None
            self.condition.notify_all()


_gates = {}
_registry_lock = threading.Lock()


def _gate(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _registry_lock:
        return _gates.setdefault(key, _Gate(Path(key)))


def finish_implicit_request(path):
    """Close a completed stage's implicit write before observing external input."""
    gate = _gate(path)
    with gate.condition:
        owner = gate.owner if gate.thread == threading.get_ident() else None
    if owner is not None:
        if owner._depth or owner._explicit_transaction:
            raise RuntimeError('명시적 DB transaction 안에서 runtime 원격을 관측할 수 없습니다.')
        owner.commit()


class _Cursor(sqlite3.Cursor):
    def __init__(self, connection):
        super().__init__(connection)
        self._rows = deque()

    def execute(self, sql, parameters=()):
        self._rows = deque()
        with self.connection.access():
            self.connection._call(lambda: super(_Cursor, self).execute(sql, parameters))
            if not self.connection.in_transaction:
                self.connection._explicit_transaction = False
            elif not self.connection._depth and sql.lstrip().upper().startswith(('BEGIN', 'SAVEPOINT')):
                self.connection._explicit_transaction = True
            if self.description is not None:
                self._rows.extend(super().fetchall())
        return self

    def executemany(self, sql, parameters):
        self._rows = deque()
        with self.connection.access():
            parameters = tuple(parameters)
            self.connection._call(lambda: super(_Cursor, self).executemany(sql, parameters))
        return self

    def executescript(self, script):
        self._rows = deque()
        with self.connection.access():
            # sqlite3.executescript는 호출자의 transaction을 먼저 commit한다.
            statement = ''
            for character in script:
                statement += character
                if character == ';' and sqlite3.complete_statement(statement):
                    self.connection._call(lambda: super(_Cursor, self).execute(statement))
                    statement = ''
            if statement.strip():
                self.connection._call(lambda: super(_Cursor, self).execute(statement))
        return self

    def fetchone(self):
        return self._rows.popleft() if self._rows else None

    def fetchmany(self, size=None):
        size = self.arraysize if size is None else size
        return [self._rows.popleft() for _ in range(min(max(0, size), len(self._rows)))]

    def fetchall(self):
        rows = list(self._rows)
        self._rows.clear()
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        if not self._rows:
            raise StopIteration
        return self._rows.popleft()

    def close(self):
        self._rows = deque()
        return super().close()


class CoordinatedConnection(sqlite3.Connection):
    def configure_access(self, path, timeout):
        self._gate = _gate(path)
        self._timeout = timeout
        self._held = False
        self._depth = 0
        self._contexts = []
        self._read_only = False
        self._explicit_transaction = False
        self._deadline = None
        self._closed = False
        return self

    @contextmanager
    def access(self):
        if self._closed:
            raise sqlite3.ProgrammingError('닫힌 DB 연결입니다.')
        if not self._held:
            self._deadline = time.monotonic() + self._timeout
            with _waiting():
                self._gate.acquire(self, self._deadline)
            self._held = True
        try:
            yield
        finally:
            if self._closed or (not self.in_transaction and not self._depth):
                self._release()

    def _release(self):
        if self._held:
            self._held = False
            self._gate.release()
            self._deadline = None

    def _call(self, function):
        while True:
            # Native waits share the same request deadline with queue waiting.
            super().execute('PRAGMA busy_timeout=0')
            transaction = self.in_transaction
            try:
                return function()
            except sqlite3.OperationalError as error:
                code = getattr(error, 'sqlite_errorcode', 0) & 255
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                if transaction:
                    raise DatabaseWaitTimeout('SQLite transaction의 잠금이 변경돼 상태를 다시 준비해야 합니다.') from error
                super().rollback()
                if time.monotonic() >= self._deadline:
                    raise DatabaseWaitTimeout('SQLite 잠금 대기 시간이 초과됐습니다.') from error
                from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
                _check_runtime_handoff()
                with _waiting():
                    time.sleep(min(.01, max(0, self._deadline-time.monotonic())))

    def cursor(self, factory=None):
        if factory is not None and factory is not _Cursor:
            raise ValueError('운영 DB cursor는 공통 접근 경계를 사용해야 합니다.')
        return super().cursor(_Cursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script):
        return self.cursor().executescript(script)

    def __enter__(self):
        if self._closed:
            raise sqlite3.ProgrammingError('닫힌 DB 연결입니다.')
        if not self._held:
            self._deadline = time.monotonic() + self._timeout
            with _waiting():
                self._gate.acquire(self, self._deadline)
            self._held = True
        try:
            savepoint = ('db_' + uuid4().hex if self.in_transaction
                         and (self._depth or self._explicit_transaction) else None)
            if savepoint:
                self.execute(f'SAVEPOINT {savepoint}')
            elif not self.in_transaction:
                self.execute('BEGIN' if self._read_only else 'BEGIN IMMEDIATE')
            self._contexts.append(savepoint)
            self._depth += 1
        except BaseException:
            if not self.in_transaction and not self._depth:
                self._release()
            raise
        return self

    def __exit__(self, kind, value, traceback):
        savepoint = self._contexts[-1]
        try:
            if savepoint:
                if kind is not None:
                    self.execute(f'ROLLBACK TO {savepoint}')
                self.execute(f'RELEASE {savepoint}')
            else:
                if kind is None:
                    self.commit()
                else:
                    self.rollback()
        except BaseException:
            if not savepoint:
                super().rollback()
            raise
        finally:
            self._contexts.pop()
            self._depth -= 1
            if self._closed or (not self._depth and not self.in_transaction):
                self._release()

    def commit(self):
        if self._contexts and self._contexts[-1] is not None:
            return
        if not self.in_transaction:
            return super().commit()
        with self.access():
            while True:
                try:
                    result = super().commit()
                    self._explicit_transaction = False
                    return result
                except sqlite3.OperationalError as error:
                    if getattr(error, 'sqlite_errorcode', 0) & 255 != sqlite3.SQLITE_BUSY:
                        raise
                    if time.monotonic() >= self._deadline:
                        raise DatabaseWaitTimeout('DB commit 잠금 대기 시간이 초과됐습니다.') from error
                    from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
                    _check_runtime_handoff()
                    with _waiting():
                        time.sleep(min(.01, max(0, self._deadline-time.monotonic())))

    def rollback(self):
        if self._contexts and self._contexts[-1] is not None:
            self.execute(f'ROLLBACK TO {self._contexts[-1]}')
            return
        if not self.in_transaction:
            return super().rollback()
        with self.access():
            result = super().rollback()
            self._explicit_transaction = False
            return result

    def close(self):
        super().close()
        self._closed = True
        self._release()

    def backup(self, target, **kwargs):
        if self.in_transaction:
            raise RuntimeError('DB backup 전에 현재 transaction을 종료해야 합니다.')
        with self.access():
            progress = kwargs.pop('progress', None)
            def check(status, remaining, total):
                from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
                _check_runtime_handoff()
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= self._deadline:
                    raise DatabaseWaitTimeout('DB backup 잠금 대기 시간이 초과됐습니다.')
                if progress is not None:
                    progress(status, remaining, total)
            return super().backup(target, progress=check, **kwargs)


def open_connection(path, *, read_only=False, create=True, timeout=30.0, factory=CoordinatedConnection):
    path = Path(path).resolve()
    uri = read_only or not create
    mode = 'ro' if read_only else 'rw'
    database = path.as_uri() + '?mode=' + mode if uri else str(path)
    connection = sqlite3.connect(database, timeout=timeout, uri=uri, factory=factory)
    connection.configure_access(path, timeout)
    connection._read_only = read_only
    return connection
