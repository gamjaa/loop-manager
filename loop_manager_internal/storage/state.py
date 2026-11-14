"""신규 단일 WAL 저장소. 실행 단계와 외부 작업의 의도를 분리한다."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


APPLICATION_ID = 0x4C4D5231
SCHEMA_VERSION = 2
STATUSES = ('READY', 'WORKING', 'VERIFYING', 'REVIEWING', 'PR_OPEN',
            'BLOCKED', 'CANCELLED', 'MERGED')
TERMINAL = ('CANCELLED', 'MERGED')

SCHEMA = """
CREATE TABLE projects (
 repo TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, remote_url TEXT NOT NULL,
 base_branch TEXT NOT NULL, provider TEXT NOT NULL CHECK(provider IN ('codex','claude')),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 revision INTEGER NOT NULL DEFAULT 1, observed_base_sha TEXT,
 request_id TEXT, request_kind TEXT, feedback_mode TEXT,
 request_result TEXT, last_error TEXT
);
CREATE TABLE pause_scopes (
 repo TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'runtime',
 paused INTEGER NOT NULL CHECK(paused IN (0,1)), sequence INTEGER NOT NULL,
 PRIMARY KEY(repo,scope)
);
CREATE TABLE requirement_states (
 repo TEXT NOT NULL REFERENCES projects(repo), requirement_id TEXT NOT NULL,
 source_path TEXT NOT NULL, source_oid TEXT NOT NULL,
 last_planned_source_oid TEXT, last_planned_base_sha TEXT,
 judgment TEXT, judgment_revision TEXT, evidence TEXT,
 PRIMARY KEY(repo,requirement_id)
);
CREATE TABLE exec_plans (
 repo TEXT NOT NULL REFERENCES projects(repo), plan_id TEXT NOT NULL,
 source_path TEXT NOT NULL, source_oid TEXT NOT NULL, title TEXT NOT NULL,
 primary_requirement_id TEXT, sources TEXT NOT NULL, source_requirements TEXT NOT NULL DEFAULT '{}', depends_on TEXT NOT NULL,
 complexity TEXT NOT NULL CHECK(complexity IN ('low','medium','high')),
 risk TEXT NOT NULL CHECK(risk IN ('low','medium','high','critical')),
 priority TEXT NOT NULL CHECK(priority IN ('critical','high','normal','low')),
 draft INTEGER NOT NULL CHECK(draft IN (0,1)),
 status TEXT NOT NULL DEFAULT 'READY' CHECK(status IN
 ('READY','WORKING','VERIFYING','REVIEWING','PR_OPEN','BLOCKED','CANCELLED','MERGED')),
 branch TEXT NOT NULL, pr_url TEXT, block_reason TEXT,
 PRIMARY KEY(repo,plan_id)
);
CREATE TABLE exec_plan_runs (
 repo TEXT NOT NULL, plan_id TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0, input_fingerprint TEXT, specification_fingerprint TEXT,
 worktree TEXT, candidate_sha TEXT, worker_result TEXT, published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
 progress_at REAL NOT NULL DEFAULT (CAST(strftime('%s','now') AS REAL)), retry_after REAL, last_error TEXT,
 process_pid INTEGER, process_identity TEXT,
 manual_mode TEXT CHECK(manual_mode IN ('PREPARING','MANUAL_WORKING','SUBMITTED')),
 request_id TEXT, request_kind TEXT,
 feedback_mode TEXT CHECK(feedback_mode IN ('CLI_FINAL','TELEGRAM_PENDING')),
 request_result TEXT, request_sequence INTEGER NOT NULL DEFAULT 0, request_generation INTEGER,
 PRIMARY KEY(repo,plan_id),
 FOREIGN KEY(repo,plan_id) REFERENCES exec_plans(repo,plan_id)
);
CREATE TABLE agent_calls (
 invocation_id TEXT PRIMARY KEY, repo TEXT NOT NULL REFERENCES projects(repo),
 scope TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('planner','worker','reviewer')),
 provider TEXT NOT NULL CHECK(provider IN ('codex','claude')), model TEXT NOT NULL,
 session_id TEXT, instruction_hash TEXT NOT NULL, generation INTEGER,
 input_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
 output_tokens INTEGER, result TEXT, created_at REAL NOT NULL,
 process_pid INTEGER, process_identity TEXT,
 CHECK(input_tokens IS NULL OR input_tokens >= 0),
 CHECK(cache_read_tokens IS NULL OR cache_read_tokens >= 0),
 CHECK(cache_write_tokens IS NULL OR cache_write_tokens >= 0),
 CHECK(output_tokens IS NULL OR output_tokens >= 0)
);
CREATE TABLE validation_receipts (
 repo TEXT NOT NULL, plan_id TEXT NOT NULL, stage TEXT NOT NULL CHECK(stage IN ('verify','review')),
 input_fingerprint TEXT NOT NULL, generation INTEGER NOT NULL,
 success INTEGER NOT NULL CHECK(success IN (0,1)), evidence TEXT NOT NULL,
 PRIMARY KEY(repo,plan_id,stage),
 FOREIGN KEY(repo,plan_id) REFERENCES exec_plans(repo,plan_id)
);
CREATE TABLE managed_prs (
 repo TEXT NOT NULL REFERENCES projects(repo), pr_number INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('planning','implementation')),
 primary_requirement_id TEXT, plan_ids TEXT NOT NULL,
 branch TEXT NOT NULL, url TEXT NOT NULL, head_sha TEXT, base_sha TEXT,
 state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED','MERGED')),
 draft INTEGER NOT NULL CHECK(draft IN (0,1)), mergeability TEXT,
 conflict_head_sha TEXT, conflict_base_sha TEXT, input_revision TEXT,
 PRIMARY KEY(repo,pr_number)
);
CREATE UNIQUE INDEX one_open_planning_pr ON managed_prs(repo,primary_requirement_id)
 WHERE kind='planning' AND state='OPEN';
CREATE TABLE external_jobs (
 job_key TEXT PRIMARY KEY, repo TEXT NOT NULL REFERENCES projects(repo),
 kind TEXT NOT NULL, target TEXT NOT NULL, expected_input TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','OBSERVED')),
 remote_result TEXT
);
CREATE TABLE notification_outbox (
 event_key TEXT PRIMARY KEY, repo TEXT NOT NULL REFERENCES projects(repo),
 scope TEXT NOT NULL, payload TEXT, silent INTEGER NOT NULL DEFAULT 0 CHECK(silent IN (0,1)),
 state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','SENT','CANCELLED')),
 retry_after REAL, attempts INTEGER NOT NULL DEFAULT 0, delivered_parts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE exec_plan_tombstones (
 repo TEXT NOT NULL REFERENCES projects(repo), plan_id TEXT NOT NULL,
 withdrawn_revision TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(repo,plan_id)
);
CREATE INDEX runnable_plans ON exec_plans(status,repo,priority);
CREATE INDEX calls_by_scope ON agent_calls(repo,scope,role);
CREATE INDEX pending_notifications ON notification_outbox(state,retry_after);
"""


class StateError(RuntimeError):
    """상태 변경·잠금·초기화 오류."""


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Store:
    def __init__(self, path: Path, *, busy_ms: int = 500):
        self.path = Path(path).resolve()
        self.busy_ms = busy_ms

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=self.busy_ms / 1000)) as db:
            application = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if application == APPLICATION_ID:
                self._validate(db)
                return
            if tables or application:
                raise StateError('기존 DB 형식은 지원하지 않습니다. 별도 보관 후 새 DB를 초기화하세요.')
            if db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise StateError('SQLite WAL 모드를 설정하지 못했습니다.')
            try:
                db.executescript(f'BEGIN IMMEDIATE;\n{SCHEMA}\n'
                                 f'PRAGMA application_id={APPLICATION_ID};\n'
                                 f'PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _validate(db):
        if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
            raise StateError('지원하지 않는 DB 스키마입니다. 운영 DB를 자동 변경하지 않습니다.')
        if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
            raise StateError('운영 DB가 WAL 모드가 아닙니다.')

    @contextmanager
    def connect(self, *, write=False):
        if not self.path.is_file():
            raise StateError('상태 DB가 없습니다. init을 먼저 실행하세요.')
        db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                             uri=True, timeout=self.busy_ms / 1000, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            self._validate(db)
            yield db
        except sqlite3.OperationalError as error:
            raise StateError(f'SQLite 접근 실패: {error}') from error
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def rows(self, sql, parameters=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, parameters)]

    def plan(self, repo, plan_id):
        rows = self.rows('SELECT p.*,r.generation,r.input_fingerprint,r.worktree,r.candidate_sha,'
                         'r.manual_mode,r.request_id,r.request_kind,r.feedback_mode,r.request_result,'
                         'r.retry_after,r.last_error,r.published,r.progress_at,r.specification_fingerprint FROM exec_plans p JOIN exec_plan_runs r '
                         'USING(repo,plan_id) WHERE repo=? AND plan_id=?', (repo, plan_id))
        if not rows:
            raise StateError(f'등록된 G가 없습니다: {plan_id}')
        return rows[0]

    def register(self, repo, path, remote_url, base_branch, provider):
        request_id = uuid.uuid4().hex
        with self.transaction() as db:
            existing = db.execute('SELECT * FROM projects WHERE repo=?', (repo,)).fetchone()
            if existing and existing['request_kind'] and not existing['request_result']:
                if (existing['request_kind'] == 'register'
                        and tuple(existing[key] for key in ('path','remote_url','base_branch','provider'))
                        == (str(path),remote_url,base_branch,provider)):
                    return existing['request_id']
                raise StateError('프로젝트에 처리 중인 요청이 있습니다.')
            db.execute('INSERT INTO projects(repo,path,remote_url,base_branch,provider,request_id,'
                       'request_kind,feedback_mode) VALUES(?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(repo) DO UPDATE SET path=excluded.path,remote_url=excluded.remote_url,'
                       'base_branch=excluded.base_branch,provider=excluded.provider,enabled=1,'
                       'revision=projects.revision+1,request_id=excluded.request_id,'
                       'request_kind=excluded.request_kind,feedback_mode=excluded.feedback_mode,request_result=NULL',
                       (repo,str(path),remote_url,base_branch,provider,request_id,'register','TELEGRAM_PENDING'))
        return request_id

    def pause(self, repo, paused):
        with self.transaction() as db:
            if repo != '*' and not db.execute('SELECT 1 FROM projects WHERE repo=?', (repo,)).fetchone():
                raise StateError('등록된 프로젝트가 없습니다.')
            db.execute("INSERT INTO pause_scopes(repo,paused,sequence) VALUES(?,?,1) "
                       "ON CONFLICT(repo,scope) DO UPDATE SET paused=excluded.paused,sequence=sequence+1",
                       (repo,int(paused)))

    def finish_project_request(self,repo,request_id,result):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM projects WHERE repo=? AND request_id=?',(repo,request_id)).fetchone()
            if row is None or row['request_result'] is not None:
                return False
            if row['request_kind']=='remove':
                pending=db.execute('SELECT plan_id,request_id FROM exec_plan_runs WHERE repo=? AND request_kind IS NOT NULL',(repo,)).fetchall()
                for request in pending:
                    self.finish_request_in(db,repo,request['plan_id'],request['request_id'],'프로젝트 등록 해제로 요청이 종료됐습니다. 작업물은 보존합니다.')
                db.execute("UPDATE exec_plan_runs SET manual_mode=NULL WHERE repo=? AND manual_mode='PREPARING'",(repo,))
            self.notify(db,f'cli:{repo}:{request_id}',repo,'project',result)
            db.execute('UPDATE projects SET request_kind=NULL,request_result=?,last_error=NULL WHERE repo=? AND request_id=?',
                       (result,repo,request_id))
            return True

    def acknowledge_output(self,repo,plan_id,request_id):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM exec_plan_runs WHERE repo=? AND plan_id=? AND request_id=?',
                           (repo,plan_id,request_id)).fetchone()
            if row is None or row['request_result'] is None:
                return
            db.execute("UPDATE exec_plan_runs SET feedback_mode='CLI_FINAL' WHERE repo=? AND plan_id=? AND request_id=?",
                       (repo,plan_id,request_id))
            db.execute("UPDATE notification_outbox SET state='CANCELLED',payload=NULL WHERE event_key=? AND state='PENDING'",
                       (f'cli:{repo}:{plan_id}:{request_id}',))

    def paused(self, repo):
        return bool(self.rows("SELECT 1 FROM pause_scopes WHERE repo IN ('*',?) AND paused=1", (repo,)))

    @staticmethod
    def cancel_notifications(db,repo,scope):
        db.execute("UPDATE notification_outbox SET state='CANCELLED',payload=NULL WHERE repo=? AND scope=? "
                   "AND state='PENDING' AND event_key NOT LIKE 'cli:%'",(repo,scope))

    @staticmethod
    def notify(db, event_key, repo, scope, message, *, silent=False):
        from loop_manager_internal.execution.validation_feedback import redact_secrets
        db.execute('INSERT INTO notification_outbox(event_key,repo,scope,payload,silent) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(event_key) DO NOTHING', (event_key,repo,scope,redact_secrets(message),int(silent)))

    def request(self, repo, plan_id, kind):
        if kind not in ('unblock','manual_start','manual_complete'):
            raise ValueError('지원하지 않는 G 요청입니다.')
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM projects WHERE repo=? AND enabled=1',(repo,)).fetchone():
                raise StateError('등록이 활성화된 프로젝트에만 요청할 수 있습니다.')
            row = db.execute('SELECT p.status,p.draft,r.* FROM exec_plans p JOIN exec_plan_runs r '
                             'USING(repo,plan_id) WHERE repo=? AND plan_id=?', (repo,plan_id)).fetchone()
            if row is None:
                raise StateError('base에 확정된 G가 없습니다.')
            if row['request_kind']:
                if row['request_kind'] == kind and not row['request_result']:
                    return row['request_id']
                raise StateError('해당 G의 이전 요청이 아직 정리되지 않았습니다.')
            if row['status'] in TERMINAL or row['draft']:
                raise StateError('완료·취소·draft G에는 요청할 수 없습니다.')
            if kind == 'unblock' and row['status'] != 'BLOCKED':
                raise StateError('unblock은 BLOCKED G에만 요청할 수 있습니다.')
            if kind == 'manual_start':
                if row['status'] != 'READY' or row['manual_mode'] is not None:
                    raise StateError('자동 실행이 예약되지 않은 READY G만 수동 준비할 수 있습니다.')
                dependencies = json.loads(db.execute('SELECT depends_on FROM exec_plans '
                                                     'WHERE repo=? AND plan_id=?', (repo,plan_id)).fetchone()[0])
                for predecessor in dependencies:
                    result = db.execute('SELECT status FROM exec_plans WHERE repo=? AND plan_id=?',
                                        (repo,predecessor)).fetchone()
                    if result is None or result[0] != 'MERGED':
                        raise StateError(f'선행 PR Merge가 필요합니다: {predecessor}')
            if kind == 'manual_complete' and row['manual_mode'] != 'MANUAL_WORKING':
                raise StateError('수동 Git 준비가 완료되지 않았습니다.')
            request_id = uuid.uuid4().hex
            db.execute('UPDATE exec_plan_runs SET request_id=?,request_kind=?,feedback_mode=?,request_result=NULL,'
                       'request_sequence=request_sequence+1,request_generation=generation,manual_mode=CASE WHEN ? THEN ? ELSE manual_mode END '
                       'WHERE repo=? AND plan_id=? AND generation=?',
                       (request_id,kind,'TELEGRAM_PENDING',kind=='manual_start','PREPARING',repo,plan_id,row['generation']))
            return request_id

    def finish_request(self, repo, plan_id, request_id, result, *, cli_final=False,
                       manual_mode=None, worktree=None):
        with self.transaction() as db:
            return self.finish_request_in(db,repo,plan_id,request_id,result,cli_final=cli_final,
                                          manual_mode=manual_mode,worktree=worktree)

    def finish_request_in(self,db,repo,plan_id,request_id,result,*,cli_final=False,manual_mode=None,worktree=None):
        row = db.execute('SELECT * FROM exec_plan_runs WHERE repo=? AND plan_id=? AND request_id=?',
                         (repo,plan_id,request_id)).fetchone()
        if row is None or row['request_result'] is not None:
            return False
        feedback = 'CLI_FINAL' if cli_final else row['feedback_mode']
        if feedback == 'TELEGRAM_PENDING':
            self.notify(db,f'cli:{repo}:{plan_id}:{request_id}',repo,plan_id,result)
        db.execute('UPDATE exec_plan_runs SET request_result=?,feedback_mode=?,request_kind=NULL,'
                   'manual_mode=COALESCE(?,manual_mode),worktree=COALESCE(?,worktree) '
                   'WHERE repo=? AND plan_id=? AND request_id=?',
                   (result,feedback,manual_mode,worktree,repo,plan_id,request_id))
        return True

    def claim(self, repo, plan_id, expected_status, next_status, fingerprint):
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM projects WHERE repo=? AND enabled=1',(repo,)).fetchone():
                return None
            if db.execute("SELECT 1 FROM pause_scopes WHERE repo IN ('*',?) AND paused=1", (repo,)).fetchone():
                return None
            row = db.execute('SELECT r.* FROM exec_plan_runs r JOIN exec_plans p USING(repo,plan_id) '
                             'WHERE repo=? AND plan_id=? AND p.status=? AND p.draft=0 '
                             'AND (r.retry_after IS NULL OR r.retry_after<=?)',
                             (repo,plan_id,expected_status,time.time())).fetchone()
            if row is None or row['manual_mode'] in ('PREPARING','MANUAL_WORKING'):
                return None
            if next_status in ('WORKING','VERIFYING','REVIEWING'):
                dependencies = json.loads(db.execute('SELECT depends_on FROM exec_plans '
                                                     'WHERE repo=? AND plan_id=?',(repo,plan_id)).fetchone()[0])
                for predecessor in dependencies:
                    previous = db.execute('SELECT status FROM exec_plans WHERE repo=? AND plan_id=?',
                                          (repo,predecessor)).fetchone()
                    if previous is None or previous[0] != 'MERGED':
                        return None
            changed = db.execute('UPDATE exec_plans SET status=? WHERE repo=? AND plan_id=? AND status=?',
                                 (next_status,repo,plan_id,expected_status)).rowcount
            if not changed:
                return None
            generation = row['generation'] + 1
            db.execute('UPDATE exec_plan_runs SET generation=?,input_fingerprint=?,retry_after=NULL,last_error=NULL '
                       'WHERE repo=? AND plan_id=?', (generation,fingerprint,repo,plan_id))
            if expected_status=='READY':
                db.execute('UPDATE exec_plan_runs SET progress_at=? WHERE repo=? AND plan_id=?',(time.time(),repo,plan_id))
            return generation

    def apply(self, repo, plan_id, generation, fingerprint, expected_status, next_status, *,
              candidate_sha=None, evidence=None, stage=None, success=True, reason=None):
        with self.transaction() as db:
            row = db.execute('SELECT p.status,r.* FROM exec_plans p JOIN exec_plan_runs r USING(repo,plan_id) '
                             'WHERE repo=? AND plan_id=?', (repo,plan_id)).fetchone()
            if (row is None or row['generation'] != generation or row['input_fingerprint'] != fingerprint
                    or row['status'] != expected_status):
                return False
            db.execute('UPDATE exec_plans SET status=?,block_reason=? WHERE repo=? AND plan_id=?',
                       (next_status,reason,repo,plan_id))
            if row['status']=='BLOCKED' or next_status in ('PR_OPEN','MERGED') or candidate_sha and candidate_sha!=row['candidate_sha']:
                self.cancel_notifications(db,repo,plan_id)
            if candidate_sha:
                db.execute('UPDATE exec_plan_runs SET progress_at=CASE WHEN candidate_sha IS NULL OR candidate_sha!=? '
                           'THEN ? ELSE progress_at END WHERE repo=? AND plan_id=?',(candidate_sha,time.time(),repo,plan_id))
                db.execute('UPDATE exec_plan_runs SET candidate_sha=? WHERE repo=? AND plan_id=?',
                           (candidate_sha,repo,plan_id))
            if next_status=='PR_OPEN':
                db.execute('UPDATE exec_plan_runs SET progress_at=? WHERE repo=? AND plan_id=?',(time.time(),repo,plan_id))
            if stage:
                if evidence is None:
                    raise ValueError('검증 근거가 필요합니다.')
                db.execute('INSERT INTO validation_receipts VALUES(?,?,?,?,?,?,?) '
                           'ON CONFLICT(repo,plan_id,stage) DO UPDATE SET input_fingerprint=excluded.input_fingerprint,'
                           'generation=excluded.generation,success=excluded.success,evidence=excluded.evidence',
                           (repo,plan_id,stage,fingerprint,generation,int(success),evidence))
            return True

    def cancel(self, repo, plan_id, revision, evidence):
        with self.transaction() as db:
            changed = db.execute("UPDATE exec_plans SET status='CANCELLED',block_reason=NULL "
                                 "WHERE repo=? AND plan_id=? AND status NOT IN ('MERGED','CANCELLED')",
                                 (repo,plan_id)).rowcount
            if changed:
                db.execute('UPDATE exec_plan_runs SET generation=generation+1,worker_result=NULL WHERE repo=? AND plan_id=?',
                           (repo,plan_id))
                db.execute('INSERT INTO exec_plan_tombstones VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                           (repo,plan_id,revision,evidence))
                db.execute("UPDATE notification_outbox SET state='CANCELLED',payload=NULL "
                           "WHERE repo=? AND scope=? AND state='PENDING'", (repo,plan_id))
                row=db.execute('SELECT request_id,request_kind FROM exec_plan_runs WHERE repo=? AND plan_id=?',(repo,plan_id)).fetchone()
                if row['request_kind']:
                    self.finish_request_in(db,repo,plan_id,row['request_id'],f'{plan_id} 요청 종료: 작업의 확정 철회로 취소됐습니다.')
            return bool(changed)
