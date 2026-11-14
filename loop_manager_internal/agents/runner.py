"""역할별 최소 Provider 호출과 작은 SQLite 호출 기록."""
from __future__ import annotations

from dataclasses import dataclass
import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from loop_manager_internal.agents.agent_provider_usage import codex_usage,claude_usage
from loop_manager_internal.agents.agent_profiles import agent_environment
from loop_manager_internal.execution.processes import run
from loop_manager_internal.execution.validation_feedback import redact_secrets


@dataclass(frozen=True)
class AgentResult:
    kind: str
    message: str
    session_id: str | None = None
    decision: dict | None = None


def reuse_auth(source,auth):
    if not source.is_file() or auth.exists() or auth.is_symlink() or source.resolve()==auth.resolve():
        return
    try:
        auth.symlink_to(source.resolve())
    except FileExistsError:
        return
    except OSError:
        # Windows에서 링크 권한이 없으면 비공개 홈 안에 인증 파일만 원자적으로 복사한다.
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=auth.parent,delete=False) as target:
                temporary=Path(target.name)
                with source.open('rb') as stream:
                    shutil.copyfileobj(stream,target)
            try:
                os.link(temporary,auth)
            except FileExistsError:
                pass
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


def instructions(root,role):
    paths = ['docs/AGENTS.common.md',f'docs/AGENTS.{role}.md']
    if role=='worker':
        paths.append('docs/AGENTS.ponytail.md')
    texts = []
    for path in paths:
        text = (Path(root)/path).read_text(encoding='utf-8')
        if not text.strip() or '\0' in text:
            raise ValueError(f'유효하지 않은 runtime 지침입니다: {path}')
        texts.append(text)
    content = '\n\n'.join(texts)
    return content,hashlib.sha256(content.encode()).hexdigest()


def arguments(provider,role,model,effort,guidance,*,session_id=None,fast=False):
    read_only = role=='reviewer'
    if provider=='codex':
        settings = ['-c',f'model_reasoning_effort={json.dumps(effort)}',
                    '-c',f'developer_instructions={json.dumps(guidance,ensure_ascii=False)}',
                    '-c','features.plugins=false','-c','features.apps=false',
                    '-c',f'service_tier={json.dumps("fast" if fast else "default")}']
        return ['codex','--ask-for-approval','never','exec','--json',
                '--sandbox','read-only' if read_only else 'workspace-write','-m',model,*settings,
                *(['resume',session_id] if session_id else []),'-']
    if provider=='claude':
        settings = {'disableAllHooks':True,'fastMode':fast,
                    'sandbox':{'enabled':True,'failIfUnavailable':True,'allowUnsandboxedCommands':False}}
        return ['claude','--print','--output-format','stream-json','--verbose','--model',model,
                '--effort',effort,'--permission-mode','auto','--permission-prompts','none',
                '--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                '--settings',json.dumps(settings),'--append-system-prompt',guidance,
                *(['--tools','Read,Glob,Grep'] if read_only else []),
                *(['--resume',session_id] if session_id else [])]
    raise ValueError('지원하지 않는 Provider입니다.')


def failure_kind(code,message):
    lower = message.lower()
    if 'session' in lower and any(word in lower for word in ('not found','does not exist','missing')):
        return 'SESSION_LOST'
    if any(word in lower for word in ('usage limit','rate limit','quota','rate_limit')):
        return 'USAGE_LIMIT'
    if any(word in lower for word in ('authentication','unauthorized','api key','login','permission','sandbox unavailable',
                                     'filenotfounderror','command not found','not recognized')):
        return 'CONFIGURATION'
    return 'TRANSIENT' if code else 'INVALID_OUTPUT'


def decode_events(provider,path):
    message,session,usage,error = '',None,None,False
    with Path(path).open(encoding='utf-8',errors='replace') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event,dict):
                continue
            if provider=='codex':
                if event.get('type')=='thread.started':
                    session = event.get('thread_id')
                if event.get('type')=='item.completed' and isinstance(event.get('item'),dict):
                    item = event['item']
                    if item.get('type')=='agent_message':
                        message = item.get('text','')
                if event.get('type')=='turn.completed':
                    usage = codex_usage(event)
                if event.get('type') in ('error','turn.failed'):
                    error = True
            else:
                if event.get('session_id'):
                    session = event['session_id']
                if event.get('type')=='result':
                    message = event.get('result','')
                    usage = claude_usage(event)
                    error = bool(event.get('is_error'))
    return message,session,usage,error


def decision(message,role):
    raw = message.strip()
    if raw.startswith('```') and raw.endswith('```'):
        raw = raw.split('\n',1)[1].rsplit('```',1)[0]
    value = json.loads(raw)
    if not isinstance(value,dict) or value.get('result') not in {
            'planner':('PLANNED','SATISFIED','UNRESOLVED'),
            'worker':('CANDIDATE','CHECKPOINT','USER_DECISION','UNRESOLVABLE_CONFLICT'),
            'reviewer':('PASS','FAIL','USER_DECISION')}[role]:
        raise ValueError('역할의 구조화 결과가 올바르지 않습니다.')
    if not isinstance(value.get('evidence'),str) or not value['evidence'].strip():
        raise ValueError('판단 근거가 필요합니다.')
    return value


class Agent:
    def __init__(self,store,state_dir,runtime_root,settings):
        self.store,self.state_dir,self.root,self.settings = store,Path(state_dir),Path(runtime_root),settings

    def call(self,project,scope,role,worktree,prompt,*,generation=None,complexity='medium',risk='medium'):
        provider = project['provider']
        try:
            model,effort = self.settings.profile(provider,role,complexity,risk)
            guidance,instruction_hash = instructions(self.root,role)
        except (configparser.Error,ValueError,OSError) as error:
            return AgentResult('CONFIGURATION',redact_secrets(str(error)))
        previous = self.store.rows('SELECT session_id FROM agent_calls WHERE repo=? AND scope=? AND role=? '
                                   'AND provider=? AND model=? AND session_id IS NOT NULL ORDER BY created_at DESC LIMIT 1',
                                   (project['repo'],scope,role,provider,model))
        session = previous[0]['session_id'] if previous else None
        invocation = uuid.uuid4().hex
        environment = agent_environment(provider,role,state_dir=self.state_dir,
                                        environment_variables=dict(os.environ))
        variable,folder,auth_name = ('CODEX_HOME','.codex','auth.json') if provider=='codex' else (
            'CLAUDE_CONFIG_DIR','.claude','.credentials.json')
        home = self.state_dir/f'automation-{provider}'
        home.mkdir(mode=0o700,parents=True,exist_ok=True)
        from loop_manager_internal.agents.provider_home import _owned_directory
        try:
            _owned_directory(home)
        except (OSError,RuntimeError) as error:
            return AgentResult('CONFIGURATION',redact_secrets(str(error)))
        source_home = Path(os.environ.get(variable,str(Path.home()/folder)))
        source = source_home/auth_name
        auth = home/auth_name
        reuse_auth(source,auth)
        environment[variable] = str(home)
        with self.store.transaction() as db:
            db.execute('INSERT INTO agent_calls(invocation_id,repo,scope,role,provider,model,instruction_hash,generation,created_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',
                       (invocation,project['repo'],scope,role,provider,model,instruction_hash,generation,time.time()))
        with tempfile.TemporaryDirectory(prefix=f'agent-{invocation}-',dir=self.state_dir) as directory:
            environment['TMPDIR'] = environment['TMP'] = environment['TEMP'] = directory
            path = Path(directory)/'provider.jsonl'
            argv = arguments(provider,role,model,effort,guidance,session_id=session,
                             fast=self.settings.fast(provider))
            def started(pid,identity):
                with self.store.transaction() as db:
                    db.execute('UPDATE agent_calls SET process_pid=?,process_identity=? WHERE invocation_id=?',
                               (pid,identity,invocation))
            def stopped():
                with self.store.transaction() as db:
                    db.execute('UPDATE agent_calls SET process_pid=NULL,process_identity=NULL WHERE invocation_id=?',(invocation,))
            result = run(argv,worktree,self.settings.agent_timeout,input_text=prompt,
                         environment=environment,output_path=path,on_started=started,on_stopped=stopped)
            message,new_session,usage,errored = decode_events(provider,path)
            kind = 'TIMEOUT' if result.timed_out else failure_kind(result.code,message or result.output)
            parsed = None
            if result.code==0 and not result.timed_out and not errored:
                try:
                    parsed = decision(message,role)
                    kind = 'SUCCESS'
                except (ValueError,TypeError):
                    kind = 'INVALID_OUTPUT'
            with self.store.transaction() as db:
                db.execute('UPDATE agent_calls SET session_id=?,input_tokens=?,cache_read_tokens=?,cache_write_tokens=?,'
                           'output_tokens=?,result=? WHERE invocation_id=?',
                           (new_session or session,usage.input_tokens if usage else None,
                            usage.cached_input_tokens if usage else None,usage.cache_write_input_tokens if usage else None,
                            usage.output_tokens if usage else None,kind,invocation))
                if kind=='SESSION_LOST':
                    db.execute('UPDATE agent_calls SET session_id=NULL WHERE repo=? AND scope=? AND role=? AND provider=? AND session_id=?',
                               (project['repo'],scope,role,provider,session))
            return AgentResult(kind,redact_secrets(message or result.output)[-6000:],new_session,parsed)
