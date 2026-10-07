"""한 테스트의 오류 변형들이 같은 실제 복구 경계에서 독립적으로 시작한다."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile

from loop_manager_internal.verification.timing import phase


def _tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        digest.update(os.fsencode(path.relative_to(root)))
        digest.update(str(info.st_mode).encode())
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(path.read_bytes())
        elif not path.is_dir():
            raise RuntimeError('지원하지 않는 테스트 준비 파일')
        digest.update(b'\0')
    return digest.hexdigest()


def _temporary_tables(connection):
    tables = []
    for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_temp_master ORDER BY name"):
        if sql is None and kind == 'index':
            continue  # Recreated by the owning table's constraint.
        if kind != 'table' or not sql.startswith('CREATE TABLE '):
            raise RuntimeError('지원하지 않는 복구 경계 TEMP schema')
        quoted = '"' + name.replace('"', '""') + '"'
        rows = tuple(tuple(row) for row in connection.execute('SELECT * FROM temp.' + quoted))
        tables.append((name, sql, rows))
    return tuple(tables)


def _restore_temporary_tables(connection, saved):
    for name, _sql, _rows in _temporary_tables(connection):
        quoted = '"' + name.replace('"', '""') + '"'
        connection.execute('DROP TABLE temp.' + quoted)
    for name, sql, rows in saved:
        connection.execute(sql.replace('CREATE TABLE ', 'CREATE TEMP TABLE ', 1))
        if rows:
            quoted = '"' + name.replace('"', '""') + '"'
            placeholders = ','.join('?' for _ in rows[0])
            connection.executemany('INSERT INTO temp.' + quoted + ' VALUES (' + placeholders + ')', rows)
    connection.commit()


class RecoveryBoundary:
    """소유한 임시 경로만 복원한다. 경로·receipt 치환 및 전역 cache는 없다.

    같은 테스트 안의 직렬 변형 전용이다. 다른 테스트/worker는 별도 경로를
    소유한다. SQLite와 모든 파일·원격·journal은 directory
    아래에 준비해야 한다. 실행 중인 Agent·Git 프로세스에서는 저장하지 않는다.
    """
    def __enter__(self):
        self.stack = ExitStack()
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='recovery-case-'))
        self.archive = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='recovery-boundary-')))
        self.saved = False
        self.database = sqlite3.connect(':memory:')
        self.stack.callback(self.database.close)
        return self

    def save(self, connection):
        if self.saved:
            raise RuntimeError('이미 저장한 복구 경계')
        databases = list(connection.execute('PRAGMA database_list'))
        self.database_path = databases[0][2]
        if (any(row[1] not in {'main', 'temp'} for row in databases) or self.database_path and (
                not Path(self.database_path).resolve().is_relative_to(Path(self.directory).resolve())
                or connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete')):
            raise RuntimeError('복구 경계 밖의 DB 또는 지원하지 않는 journal mode')
        with phase('git_fixture:boundary_save'):
            connection.commit()
            self.temporary_tables = _temporary_tables(connection)
            connection.backup(self.database)
            self.sql = tuple(self.database.iterdump())
            with phase('git_fixture:boundary_confirmation'):
                self.digest = _tree_digest(Path(self.directory))
            with phase('git_fixture:boundary_copy'):
                shutil.copytree(self.directory, self.archive / 'files', symlinks=True)
            with phase('git_fixture:boundary_confirmation'):
                if _tree_digest(self.archive / 'files') != self.digest:
                    raise RuntimeError('복구 경계 복사 불일치')
            self.saved = True
            self.stack.callback(connection.close)

    def restore(self, connection):
        if not self.saved:
            raise RuntimeError('저장한 복구 경계가 없습니다')
        with phase('git_fixture:boundary_identity'):
            if (_tree_digest(self.archive / 'files') != self.digest
                    or tuple(self.database.iterdump()) != self.sql):
                raise RuntimeError('복구 경계 identity 또는 digest 불일치')
        with phase('git_fixture:boundary_restore'):
            if self.database_path:
                connection.close()
            # root는 이 인스턴스가 직접 만든 TemporaryDirectory에만 고정된다.
            def writable(function, path, _error):
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                function(path)
            shutil.rmtree(self.directory, onerror=writable)
            with phase('git_fixture:boundary_copy'):
                shutil.copytree(self.archive / 'files', self.directory, symlinks=True)
            with phase('git_fixture:boundary_confirmation'):
                if _tree_digest(Path(self.directory)) != self.digest:
                    raise RuntimeError('복구 시작 파일 재현 불일치')
            if self.database_path:
                connection = sqlite3.connect(self.database_path)
                connection.row_factory = sqlite3.Row
                self.stack.callback(connection.close)
            else:
                connection.rollback()
                self.database.backup(connection)
            _restore_temporary_tables(connection, self.temporary_tables)
            with phase('git_fixture:boundary_confirmation'):
                if tuple(connection.iterdump()) != self.sql or _temporary_tables(connection) != self.temporary_tables:
                    raise RuntimeError('복구 시작 상태 재현 불일치')
            return connection

    def __exit__(self, *error):
        with phase('git_fixture:boundary_cleanup'):
            return self.stack.__exit__(*error)
