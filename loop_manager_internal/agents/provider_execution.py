"""Current and invocation-bound provider inputs, without Agent generation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from loop_manager_internal.agents import agent_profiles, agent_write_isolation, common_guidance, provider_guidance
from loop_manager_internal.agents.agent_invocation import InvocationContext
from loop_manager_internal.config.managed_config import provider_fast_mode
from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
from loop_manager_internal.storage.common_guidance_state import GuidanceRepository


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def execution_identity(evidence, launch, profile, instruction: str, *, read_only: bool) -> dict[str, str]:
    if evidence.status != 'verified' or not instruction:
        raise agent_write_isolation.IsolationError('실제 Agent 실행 입력 증거가 확인되지 않았습니다.')
    # Auth values never enter hashes or storage. Per-call temporary paths have
    # the same owned boundary; normalize only the prepared launch's exact path.
    safe_variables = {key: value for key, value in launch.environment.items()
        if key.upper() in {'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PATH', 'PATHEXT',
                           'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'LANG', 'LC_ALL',
                           'CODEX_HOME', 'CODEX_CA_CERTIFICATE', 'CLAUDE_CONFIG_DIR'}}
    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str) and (value == str(launch.attempt) or value.startswith(str(launch.attempt) + '/')):
            return '<owned-call-directory>' + value[len(str(launch.attempt)):]
        return value
    policy = {'configuration': evidence.configuration_digest, 'read_only': read_only,
              'arguments': normalize(list(launch.arguments)),
              'boundary': normalize(json.loads(launch.specification)),
              'extensions': list(agent_write_isolation.CODEX_EXTENSION_ARGUMENTS),
              'version': 'native-tools-v2'}
    return {'provider': evidence.provider, 'model': profile.model, 'effort': profile.effort,
            'instruction': instruction, 'environment': digest({'platform': evidence.environment,
                'cli': evidence.cli_version, 'variables': safe_variables, 'runtime': str(launch.runtime)}),
            'policy': digest(policy)}


def observe(connection, *, repo: str, plan_id: str | None, worktree: Path,
            state_dir: Path, config_path: Path, provider: str,
            resolve_profile, snapshot_digest, validate_runtime=None, invocation_id: str | None = None):
    """Read actual completed evidence or prove the current prepared inputs."""
    if provider != 'codex':
        return None  # Existing Claude isolation refusal remains authoritative.
    launch = None
    try:
        ledger = InvocationRepository(connection)
        if invocation_id is not None:
            return ledger.execution_identity(invocation_id, repo=repo, plan_id=plan_id,
                role='reviewer', worktree=str(worktree.resolve()), provider=provider)
        guidance = common_guidance.resolve(GuidanceRepository(connection),
            InvocationContext(repo, plan_id), 'reviewer', worktree, state_dir)
        checked = []
        launch = agent_write_isolation.prepare_isolation(provider, 'reviewer', worktree, state_dir,
            True, checked.append, snapshot_digest, config_path=config_path, expected_repository=repo,
            environment_variables=agent_profiles.agent_environment(provider, 'reviewer', state_dir=state_dir))
        with agent_profiles.prepared_environment(provider, launch.environment):
            if validate_runtime is None:
                return None
            validate_runtime(config_path, provider, worktree)
            profile = resolve_profile(config_path, provider, 'reviewer', None, worktree)
        fast = provider_fast_mode(config_path, provider)
        context = ('-m', profile.model, '-c', f'model_reasoning_effort="{profile.effort}"',
            '-c', 'service_tier="fast"' if fast else 'service_tier="default"',
            '-c', f'features.fast_mode={str(fast).lower()}', *launch.arguments)
        instructions = []

        def check_native_lookup():
            agent_write_isolation.assert_preparation_lookup_current(
                checked[0], worktree, state_dir, config_path,
                environment_variables=launch.environment)

        provider_guidance.codex_arguments(guidance, worktree, environment=launch.environment,
            context_arguments=context, observe_instruction=instructions.append,
            on_lookup=check_native_lookup)
        evidence = agent_write_isolation.refresh_configuration_evidence(
            checked[0], launch, worktree, state_dir, config_path, repo, True, snapshot_digest,
            environment_variables=launch.environment)
        agent_write_isolation.assert_current(evidence, worktree, state_dir, config_path,
            repo, True, snapshot_digest, environment_variables=launch.environment)
        return execution_identity(evidence, launch, profile, instructions[0], read_only=True)
    except (RuntimeError, OSError, ValueError, TypeError, IndexError, sqlite3.Error):
        return None
    finally:
        if launch is not None:
            try:
                launch.attempt.rmdir()  # Never recursively remove unexpected files.
            except OSError:
                pass
