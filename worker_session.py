"""Pure decisions for binding a Worker session to its resolved CLI profile."""

from __future__ import annotations

from dataclasses import dataclass
import json

from agent_invocation import ResolvedProfile


@dataclass(frozen=True)
class SessionBinding:
    provider: str
    model: str
    effort: str

    def serialize(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def deserialize(cls, value: str | None) -> SessionBinding | None:
        if value is None:
            return None
        data = json.loads(value)
        return cls(data['provider'], data['model'], data['effort'])


@dataclass(frozen=True)
class SessionTransition:
    identity: str
    kind: str
    cause: str
    previous_session: str | None
    previous_binding: SessionBinding | None
    target: SessionBinding

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
        data = json.loads(value)
        previous = data['previous_binding']
        return cls(data['identity'], data['kind'], data['cause'],
                   data['previous_session'], SessionBinding(**previous) if previous else None,
                   SessionBinding(**data['target']))


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
