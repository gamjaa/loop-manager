"""운영 DB 연결의 짧은 처리와 transaction을 파일별로 순서대로 실행한다."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4


_wait_depth = ContextVar('db_wait_depth', default=0)


@contextmanager
def _waiting():
    from loop_manager_internal.verification.execution_clock import current_execution_clock
    clock = current_execution_clock.get()
    started = time.monotonic()
    depth = _wait_depth.get()
    token = _wait_depth.set(depth + 1)
    inactive = clock is not None and not clock.active_processes
    try:
        yield
    finally:
        _wait_depth.reset(token)
        if not depth and inactive and not clock.active_processes:
            clock.local_wait += time.monotonic() - started


class DatabaseWaitTimeout(sqlite3.OperationalError):
    """DB 차례를 기다렸지만 상태를 변경하지 못했다."""


class _RetryRequest(DatabaseWaitTimeout):
    """Only an explicitly replayable DB request may consume this signal."""


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise DatabaseWaitTimeout('DB 요청의 공통 대기 시간이 초과됐습니다.')


def _check_wait(deadline):
    from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
    _check_runtime_handoff()
    _check_deadline(deadline)


def _pause(deadline, interval=.01):
    _check_wait(deadline)
    with _waiting():
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    _check_wait(deadline)


def _byte_lock(handle, offset, *, unlock=False):
    """Queue metadata/leases use byte locks; byte zero retains the access lock."""
    handle.seek(offset)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(handle.fileno(), fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB,
                        1, offset)
    except OSError as error:
        if not unlock and (error.errno in (errno.EACCES, errno.EAGAIN)
                           or getattr(error, 'winerror', None) in (32, 33)):
            return False
        raise
    return True


class _Gate:
    def __init__(self, path):
        self.path = path
        self.condition = threading.Condition()
        self.queue = deque()
        self.owner = None
        self.thread = None
        self.handle = None
        self.ticket = None

    @contextmanager
    def _queue_metadata(self, deadline):
        while not _byte_lock(self.handle, 1):
            _pause(deadline)
        try:
            yield
        finally:
            _byte_lock(self.handle, 1, unlock=True)

    def _join_process_queue(self, deadline):
        # Each two-byte record has a readable status and an OS-locked lease.
        # Never read another process's locked byte (Windows denies that I/O).
        with self._queue_metadata(deadline):
            size = self.handle.seek(0, os.SEEK_END)
            live = False
            for offset in range(2, size, 2):
                self.handle.seek(offset)
                if self.handle.read(1) == b'Q':
                    if not _byte_lock(self.handle, offset + 1):
                        live = True
                        break
                    _byte_lock(self.handle, offset + 1, unlock=True)
            if not live:
                self.handle.truncate(2)
                size = 2
            self.handle.seek(size)
            self.handle.write(b'Q0')
            self.handle.flush()
            _byte_lock(self.handle, size + 1)
            self.ticket = size

    def _first_process(self, deadline):
        with self._queue_metadata(deadline):
            for offset in range(2, self.ticket, 2):
                self.handle.seek(offset)
                if self.handle.read(1) != b'Q':
                    continue
                if not _byte_lock(self.handle, offset + 1):
                    return False
                # A terminated waiter/owner cannot leave a permanent queue entry.
                _byte_lock(self.handle, offset + 1, unlock=True)
                self.handle.seek(offset)
                self.handle.write(b'-')
                self.handle.flush()
            return True

    def acquire(self, owner, deadline):
        ticket = object()
        with self.condition:
            if self.thread == threading.get_ident():
                raise RuntimeError('DB 처리 중 다른 연결의 차례를 기다릴 수 없습니다.')
            self.queue.append(ticket)
            try:
                while self.owner is not None or self.queue[0] is not ticket:
                    _check_wait(deadline)
                    remaining = deadline - time.monotonic()
                    with _waiting():
                        self.condition.wait(max(0, min(remaining, .05)))
                # Ordinary non-waiting completion/lease cleanup remains possible
                # after runtime handoff. Explicit requests also bound preparation.
                if owner._request_deadline is not None:
                    _check_wait(deadline)
                self.owner, self.thread = owner, threading.get_ident()
            finally:
                self.queue.remove(ticket)
                self.condition.notify_all()
        try:
            descriptor = os.open(str(self.path) + '.access.lock', os.O_RDWR | os.O_CREAT, 0o600)
            self.handle = os.fdopen(descriptor, 'r+b', buffering=0)
            self._join_process_queue(deadline)
            while True:
                if owner._request_deadline is not None:
                    _check_wait(deadline)
                if not self._first_process(deadline):
                    _pause(deadline)
                    continue
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
                    _pause(deadline)
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.handle is not None:
            # Closing releases the lease even on cancellation. The next waiter
            # reclaims stale records without needing a cleanup lock/deadline.
            self.handle.close()
            self.handle = None
            self.ticket = None
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
            def execute_and_materialize():
                super(_Cursor, self).execute(sql, parameters)
                if self.description is not None:
                    self._rows = deque(super(_Cursor, self).fetchall())
            self.connection._call(execute_and_materialize)
            if not self.connection.in_transaction:
                self.connection._explicit_transaction = False
            elif not self.connection._depth and sql.lstrip().upper().startswith(('BEGIN', 'SAVEPOINT')):
                self.connection._explicit_transaction = True
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
        self._request_deadline = None
        self._request_running = False
        self._retry_error = None
        self._closed = False
        return self

    @contextmanager
    def access(self):
        if self._closed:
            raise sqlite3.ProgrammingError('닫힌 DB 연결입니다.')
        if not self._held:
            self._deadline = (self._request_deadline if self._request_deadline is not None
                              else time.monotonic() + self._timeout)
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
                try:
                    if transaction or self._request_deadline is not None:
                        wait_error = _RetryRequest if self._request_deadline is not None else DatabaseWaitTimeout
                        if self._request_deadline is not None:
                            self._retry_error = wait_error('SQLite 잠금이 변경돼 DB 요청을 다시 준비해야 합니다.')
                            raise self._retry_error from error
                        raise wait_error('SQLite 잠금이 변경돼 DB 요청을 다시 준비해야 합니다.') from error
                    super().rollback()
                    _pause(self._deadline)
                except BaseException:
                    if not self._depth:
                        super().rollback()
                        self._explicit_transaction = False
                    raise

    def run_request(self, operation):
        """Atomically retry a short DB-only callable, re-reading state each time.

        The callable receives this connection and must revalidate current ownership,
        generation and CAS predicates on every invocation. Agent/Git/verify/network
        effects belong outside this boundary. Arbitrary context bodies are never
        replayed. Nested requests join the outer request without their own retry.
        Transaction control belongs to this boundary, not to SQL in the callable.
        """
        if self._request_deadline is not None:
            with self:
                return operation(self)
        if self.in_transaction or self._depth:
            raise RuntimeError('재시도 DB 요청은 호출자의 transaction 밖에서 시작해야 합니다.')
        self._request_deadline = time.monotonic() + self._timeout
        try:
            while True:
                _check_wait(self._request_deadline)
                self._retry_error = None
                try:
                    with self:
                        self._request_running = True
                        try:
                            result = operation(self)
                            if self._retry_error is not None:
                                raise self._retry_error
                        finally:
                            self._request_running = False
                    return result
                except _RetryRequest:
                    # __exit__ rolled back and returned the turn. Rejoining the
                    # queue uses the original deadline, behind existing waiters.
                    _pause(self._request_deadline)
        finally:
            self._request_deadline = None
            self._retry_error = None

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
            self._deadline = (self._request_deadline if self._request_deadline is not None
                              else time.monotonic() + self._timeout)
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
        if self._request_running:
            return
        if self._contexts and self._contexts[-1] is not None:
            return
        if not self.in_transaction:
            return super().commit()
        with self.access():
            super().execute('PRAGMA busy_timeout=0')
            try:
                if self._request_deadline is not None:
                    _check_wait(self._deadline)
            except BaseException:
                super().rollback()
                self._explicit_transaction = False
                raise
            while True:
                try:
                    result = super().commit()
                    self._explicit_transaction = False
                    return result
                except sqlite3.OperationalError as error:
                    if getattr(error, 'sqlite_errorcode', 0) & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    try:
                        if self._request_deadline is not None:
                            raise _RetryRequest('DB commit 경합으로 요청을 다시 준비해야 합니다.') from error
                        _pause(self._deadline)
                    except BaseException:
                        super().rollback()
                        self._explicit_transaction = False
                        raise

    def rollback(self):
        if self._request_running and not (self._contexts and self._contexts[-1] is not None):
            raise RuntimeError('재시도 DB 요청의 rollback은 요청 경계가 소유합니다.')
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
        if self._request_deadline is not None:
            raise RuntimeError('재시도 DB 요청 안에서 연결을 닫을 수 없습니다.')
        super().close()
        self._closed = True
        self._release()

    def backup(self, target, **kwargs):
        if self.in_transaction:
            raise RuntimeError('DB backup 전에 현재 transaction을 종료해야 합니다.')
        with self.access():
            super().execute('PRAGMA busy_timeout=0')
            progress = kwargs.pop('progress', None)
            def check(status, remaining, total):
                _check_wait(self._deadline)
                if progress is not None:
                    progress(status, remaining, total)
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    _pause(self._deadline)
            # sqlite3's internal backup sleep is opaque to the execution clock.
            # Disable it and account only the explicit, confirmed busy pause.
            kwargs.pop('sleep', None)
            _check_wait(self._deadline)
            return super().backup(target, progress=check, sleep=0, **kwargs)


def open_connection(path, *, read_only=False, create=True, timeout=30.0, factory=CoordinatedConnection):
    path = Path(path).resolve()
    uri = read_only or not create
    mode = 'ro' if read_only else 'rw'
    database = path.as_uri() + '?mode=' + mode if uri else str(path)
    connection = sqlite3.connect(database, timeout=0, uri=uri, factory=factory)
    connection.configure_access(path, timeout)
    connection._read_only = read_only
    return connection
