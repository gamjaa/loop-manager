"""Resolved Worker profile compatibility and pending transition decisions."""

import json
from unittest import TestCase

from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.execution.worker_session import (SessionBinding, SessionTransition, decide_session,
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


class WorkerSessionCodecTests(TestCase):
    def test_binding_codec_preserves_nullable_legacy_value_and_rejects_invalid_shape(self):
        binding = SessionBinding('codex', 'model', 'high')
        self.assertIsNone(SessionBinding.deserialize(None))
        self.assertEqual(SessionBinding.deserialize(binding.serialize()), binding)

        invalid = (
            'not json',
            '[]',
            json.dumps({'provider': 'codex', 'effort': 'high'}),
            json.dumps({'provider': 'codex', 'model': 7, 'effort': 'high'}),
            json.dumps({'provider': None, 'model': 'model', 'effort': 'high'}),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionBinding.deserialize(value)

    def test_transition_codec_validates_kind_required_fields_and_nested_bindings(self):
        transition = SessionTransition(
            'change-one', 'replacement', 'model_changed', None, None,
            SessionBinding('claude', 'new-model', 'medium'),
        )
        self.assertIsNone(SessionTransition.deserialize(None))
        self.assertEqual(SessionTransition.deserialize(transition.serialize()), transition)

        invalid_data = (
            {'identity': 'change-one', 'kind': 'start', 'cause': 'start',
             'previous_session': None, 'previous_binding': None,
             'target': {'provider': 'claude', 'model': 'new-model', 'effort': 'medium'}},
            {'identity': 'change-one', 'kind': 'fallback', 'cause': 'session_unavailable',
             'previous_session': 'old-session', 'previous_binding': [],
             'target': {'provider': 'claude', 'model': 'new-model', 'effort': 'medium'}},
            {'identity': 'change-one', 'kind': 'replacement', 'cause': 'model_changed',
             'previous_session': None, 'previous_binding': None,
             'target': {'provider': 'claude', 'model': 'new-model'}},
            {'identity': 'change-one', 'kind': 'replacement', 'cause': 'model_changed',
             'previous_session': 7, 'previous_binding': None,
             'target': {'provider': 'claude', 'model': 'new-model', 'effort': 'medium'}},
            {'identity': 'change-one', 'kind': 'replacement', 'cause': 'model_changed',
             'previous_session': None, 'previous_binding': None, 'target': []},
            {'identity': 'change-one', 'kind': 'replacement', 'cause': 'model_changed',
             'previous_session': None, 'previous_binding': None},
        )
        for data in invalid_data:
            with self.subTest(data=data), self.assertRaises(ValueError):
                SessionTransition.deserialize(json.dumps(data))
        for value in ('not json', '[]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionTransition.deserialize(value)
