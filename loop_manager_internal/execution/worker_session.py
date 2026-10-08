"""Pure decisions for binding a Worker session to its resolved CLI profile."""

from __future__ import annotations

from dataclasses import dataclass
import json

from loop_manager_internal.agents.agent_invocation import ResolvedProfile


class WorkerSessionDataError(ValueError):
    """A persisted Worker session value does not match its stored contract."""


_SESSION_TRANSITION_KINDS = frozenset({'replacement', 'fallback'})


def _required_string(data: dict, field: str, owner: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WorkerSessionDataError(f'{owner}.{field} must be a non-empty string')
    return value


def _optional_string(data: dict, field: str, owner: str) -> str | None:
    value = data.get(field)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise WorkerSessionDataError(f'{owner}.{field} must be a string or null')
    return value


def _required_fields(data: dict, fields: tuple[str, ...], owner: str) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        raise WorkerSessionDataError(
            f'{owner} is missing required field(s): {", ".join(missing)}'
        )


def _load_object(value: str, owner: str) -> dict:
    if not isinstance(value, str):
        raise WorkerSessionDataError(f'{owner} must be a JSON string')
    try:
        data = json.loads(value)
    except json.JSONDecodeError as error:
        raise WorkerSessionDataError(f'{owner} must contain valid JSON') from error
    if not isinstance(data, dict):
        raise WorkerSessionDataError(f'{owner} must contain a JSON object')
    return data


def _binding_from_object(data: object, owner: str) -> SessionBinding:
    if not isinstance(data, dict):
        raise WorkerSessionDataError(f'{owner} must be a JSON object')
    fields = ('provider', 'model', 'effort')
    _required_fields(data, fields, owner)
    return SessionBinding(*(_required_string(data, field, owner) for field in fields))


@dataclass(frozen=True)
class SessionBinding:
    provider: str
    model: str
    effort: str

    def __post_init__(self):
        for field in ('provider', 'model', 'effort'):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise WorkerSessionDataError(
                    f'SessionBinding.{field} must be a non-empty string'
                )

    def serialize(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def deserialize(cls, value: str | None) -> SessionBinding | None:
        if value is None:
            return None
        data = _load_object(value, 'SessionBinding')
        return _binding_from_object(data, 'SessionBinding')


@dataclass(frozen=True)
class SessionTransition:
    identity: str
    kind: str
    cause: str
    previous_session: str | None
    previous_binding: SessionBinding | None
    target: SessionBinding

    def __post_init__(self):
        for field in ('identity', 'cause'):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise WorkerSessionDataError(
                    f'SessionTransition.{field} must be a non-empty string'
                )
        if (not isinstance(self.kind, str)
                or self.kind not in _SESSION_TRANSITION_KINDS):
            raise WorkerSessionDataError('SessionTransition.kind is unsupported')
        if (self.previous_session is not None
                and (not isinstance(self.previous_session, str)
                     or not self.previous_session.strip())):
            raise WorkerSessionDataError(
                'SessionTransition.previous_session must be a string or null'
            )
        if (self.previous_binding is not None
                and not isinstance(self.previous_binding, SessionBinding)):
            raise WorkerSessionDataError(
                'SessionTransition.previous_binding must be a binding or null'
            )
        if not isinstance(self.target, SessionBinding):
            raise WorkerSessionDataError('SessionTransition.target must be a binding')

    def serialize(self) -> str:
        return json.dumps({
            'identity': self.identity, 'kind': self.kind, 'cause': self.cause,
            'previous_session': self.previous_session,
            'previous_binding': self.previous_binding.__dict__ if self.previous_binding else None,
            'target': self.target.__dict__,
        }, sort_keys=True)

    @classmethod
    def deserialize(cls, value: str | None) -> SessionTransition | None:
        if value is None:
            return None
        owner = 'SessionTransition'
        data = _load_object(value, owner)
        _required_fields(data, (
            'identity', 'kind', 'cause', 'previous_session',
            'previous_binding', 'target',
        ), owner)
        kind = _required_string(data, 'kind', owner)
        if kind not in _SESSION_TRANSITION_KINDS:
            raise WorkerSessionDataError(f'{owner}.kind is unsupported')
        previous = data['previous_binding']
        return cls(
            _required_string(data, 'identity', owner), kind,
            _required_string(data, 'cause', owner),
            _optional_string(data, 'previous_session', owner),
            (_binding_from_object(previous, f'{owner}.previous_binding')
             if previous is not None else None),
            _binding_from_object(data['target'], f'{owner}.target'),
        )


@dataclass(frozen=True)
class SessionDecision:
    kind: str
    cause: str | None
    session_id: str | None
    target: SessionBinding
    transition: SessionTransition | None = None


def decide_session(session_id: str | None, binding: SessionBinding | None,
                   pending: SessionTransition | None, provider: str,
                   profile: ResolvedProfile, transition_id: str) -> SessionDecision:
    """A pending target wins over later configuration changes until observed."""
    if pending is not None:
        return SessionDecision(pending.kind, pending.cause, None, pending.target, pending)
    target = SessionBinding(provider, profile.model, profile.effort)
    if session_id is None:
        return SessionDecision('start', None, None, target)
    if binding is None:
        kind, cause = 'replacement', 'unbound_session_recovery'
    elif (binding.provider, binding.model) != (target.provider, target.model):
        kind = 'replacement'
        cause = ('provider_changed' if binding.provider != target.provider
                 else 'model_changed')
    else:
        return SessionDecision(
            'effort_resume' if binding.effort != target.effort else 'resume',
            'effort_changed' if binding.effort != target.effort else None,
            session_id, target,
        )
    transition = SessionTransition(transition_id, kind, cause, session_id, binding, target)
    return SessionDecision(kind, cause, None, target, transition)


def unavailable_transition(session_id: str, binding: SessionBinding | None,
                           target: SessionBinding, transition_id: str) -> SessionTransition:
    return SessionTransition(transition_id, 'fallback', 'session_unavailable',
                             session_id, binding, target)
