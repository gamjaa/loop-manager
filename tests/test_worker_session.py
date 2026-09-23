"""Resolved Worker profile compatibility and pending transition decisions."""

from unittest import TestCase

from agent_invocation import ResolvedProfile
from worker_session import (SessionBinding, SessionTransition, decide_session,
                            unavailable_transition)


class WorkerSessionDecisionTests(TestCase):
    def test_profile_transition_table(self):
        bound = SessionBinding('codex', 'real-model', 'medium')
        cases = (
            ('codex', 'real-model', 'medium', 'resume', None, 'saved'),
            ('codex', 'real-model', 'high', 'effort_resume', 'effort_changed', 'saved'),
            ('codex', 'real-model', 'low', 'effort_resume', 'effort_changed', 'saved'),
            ('codex', 'other-model', 'medium', 'replacement', 'model_changed', None),
            ('claude', 'real-model', 'medium', 'replacement', 'provider_changed', None),
        )
        for provider, model, effort, kind, cause, session in cases:
            with self.subTest(provider=provider, model=model, effort=effort):
                decision = decide_session('saved', bound, None, provider,
                                          ResolvedProfile(model, effort), 'change-one')
                self.assertEqual((decision.kind, decision.cause, decision.session_id),
                                 (kind, cause, session))
        self.assertEqual(decide_session('saved', None, None, 'codex',
                         ResolvedProfile('real-model', 'medium'), 'change-one').cause,
                         'unbound_session_recovery')
        self.assertEqual(decide_session(None, None, None, 'codex',
                         ResolvedProfile('real-model', 'medium'), 'change-one').kind, 'start')

    def test_pending_target_survives_config_change_and_round_trip(self):
        old = SessionBinding('codex', 'old', 'medium')
        pending = decide_session('old-session', old, None, 'claude',
                                 ResolvedProfile('new', 'high'), 'change-one').transition
        self.assertIsNotNone(pending)
        restored = SessionTransition.deserialize(pending.serialize())
        decision = decide_session(None, old, restored, 'codex',
                                  ResolvedProfile('old', 'low'), 'ignored')
        self.assertEqual(decision.transition, pending)
        self.assertEqual(decision.target, SessionBinding('claude', 'new', 'high'))
        self.assertIsNone(decision.session_id)
        fallback = unavailable_transition('new-session', decision.target,
                                          decision.target, 'fallback-one')
        self.assertEqual((fallback.kind, fallback.cause, fallback.previous_session),
                         ('fallback', 'session_unavailable', 'new-session'))
