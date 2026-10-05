"""활성 verify에서만 snapshot별 Git 비용을 집계한다."""
from contextlib import contextmanager
from dataclasses import replace
import sys
import os
import subprocess
from unittest import mock

from loop_manager_internal.verification.timing import _current_writer, _phase_names


def _callers():
    frame = sys._getframe(1)
    callers = []
    while frame and len(callers) < 5:
        module = frame.f_globals.get('__name__', '')
        if (module.startswith(('loop_manager_internal.', 'tests.', 'test_'))
                and module not in {__name__, 'loop_manager_internal.agents.scope_observation',
                                   'loop_manager_internal.agents.scope_adapter',
                                   'loop_manager_internal.application.compatibility'}):
            callers.append(f'{module}:{frame.f_code.co_name}:{frame.f_lineno}')
        frame = frame.f_back
    del frame
    return tuple(callers)


@contextmanager
def git_command_timing():
    """실제 Git subprocess를 준비·관측 경계 밖까지 집계한다. 인자는 저장하지 않는다."""
    writer = _current_writer.get()
    if writer is None:
        yield
        return
    initialize, wait = subprocess.Popen.__init__, subprocess.Popen.wait
    pending, totals = {}, {}

    def start(process, args, *rest, **options):
        key = None
        if isinstance(args, (list, tuple)) and args and os.fsdecode(args[0]).replace('\\', '/').rsplit('/', 1)[-1].lower() in {'git', 'git.exe'}:
            index = 1
            while index < len(args) and args[index] in ('-C', '-c', '--git-dir', '--work-tree'):
                index += 2
            # 알 수 없는 option/value를 기록하지 않고 분류만 한다.
            command = str(args[index]) if index < len(args) else ''
            known = {'add', 'branch', 'check-ignore', 'checkout', 'clone', 'commit', 'config',
                     'diff', 'fetch', 'for-each-ref', 'init', 'ls-files', 'ls-remote', 'ls-tree',
                     'merge-base', 'push', 'rebase', 'remote', 'reset', 'rev-parse', 'show',
                     'status', 'symbolic-ref', 'update-index', 'update-ref', 'worktree', 'hash-object'}
            key = (getattr(writer, 'current_test', None), _phase_names.get(), _callers(),
                   command if command in known else 'other')
        then = writer.clock()
        try:
            return initialize(process, args, *rest, **options)
        finally:
            if key is not None:
                pending[id(process)] = (key, then)

    def finish(process, *args, **kwargs):
        try:
            return wait(process, *args, **kwargs)
        finally:
            if process.returncode is not None and id(process) in pending:
                key, then = pending.pop(id(process))
                cost = totals.setdefault(key, {'count': 0, 'seconds': 0.0})
                cost['count'] += 1
                cost['seconds'] += writer.clock() - then

    try:
        with mock.patch.object(subprocess.Popen, '__init__', start), mock.patch.object(subprocess.Popen, 'wait', finish):
            yield
    finally:
        for (test, phases, callers, command), cost in totals.items():
            writer.emit('git_cost', test=test, phases=phases, callers=callers, command=command, **cost)
        if pending:
            writer.emit('git_cost_incomplete', count=len(pending))


@contextmanager
def snapshot_timing(effects):
    writer = _current_writer.get()
    if writer is None:
        yield effects
        return
    frame = sys._getframe(1)
    ignored = {__name__, "contextlib", "loop_manager",
               "loop_manager_internal.agents.scope_observation",
               "loop_manager_internal.agents.scope_adapter",
               "loop_manager_internal.application.compatibility"}
    while frame and (frame.f_globals.get("__name__") in ignored or frame.f_code.co_name == "<lambda>"):
        frame = frame.f_back
    caller = f"{frame.f_globals.get('__name__')}:{frame.f_code.co_name}:{frame.f_lineno}" if frame else "unknown"
    del frame
    name = f"git_snapshot:{caller}" + (' <- ' + ' <- '.join(_callers()))
    context = dict(test=getattr(writer, 'current_test', None), phases=_phase_names.get())
    start = writer.emit("phase_start", name=name, snapshot="full", **context)
    commands = {}

    def measured(function, command):
        def call(*args, **kwargs):
            label = command(args)
            cost = commands.setdefault(label, {"count": 0, "seconds": 0.0})
            cost["count"] += 1
            then = writer.clock()
            try:
                return function(*args, **kwargs)
            finally:
                cost["seconds"] += writer.clock() - then
        return call

    def subprocess_command(args):
        # 인자·경로·출력은 기록하지 않는다. 이 경계는 git -C repo 명령만 받는다.
        return args[0][3]

    observed = replace(effects,
        git=measured(effects.git, lambda args: args[1]),
        git_bytes=measured(effects.git_bytes, lambda args: args[1]),
        _worktree_catalog=measured(effects._worktree_catalog, lambda args: "worktree"),
        _handoff_run=measured(effects._handoff_run, subprocess_command),
        run_config=measured(effects.run_config, subprocess_command))
    result = "error"
    try:
        yield observed
        result = "complete"
    finally:
        writer.emit("phase_end", name=name, start=start["at"], snapshot="full",
                    duration=writer.clock() - start["at"], result=result, commands=commands, **context)
