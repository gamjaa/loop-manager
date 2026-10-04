"""활성 verify에서만 snapshot별 Git 비용을 집계한다."""
from contextlib import contextmanager
from dataclasses import replace
import sys

from loop_manager_internal.verification.timing import _current_writer


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
    name = f"git_snapshot:{caller}"
    start = writer.emit("phase_start", name=name, snapshot="full")
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
                    duration=writer.clock() - start["at"], result=result, commands=commands)
