"""Tests for the application state machine."""

import unittest

from spytorec import state


class StateTransitionTest(unittest.TestCase):

    def setUp(self):
        state.current_state = state.STATE_INIT
        self.addCleanup(setattr, state, 'current_state', state.STATE_INIT)

    def _walk(self, *targets):
        for target in targets:
            state.set_state(target)

    def test_skipping_the_track_being_recorded(self):
        self._walk(state.STATE_MONITORING, state.STATE_RECORDING)

        self.assertTrue(state.set_state(state.STATE_SKIPPED))
        self.assertEqual(state.get_state(), state.STATE_SKIPPED)

    def test_changing_to_a_blocked_track_while_recording(self):
        self._walk(state.STATE_MONITORING, state.STATE_RECORDING, state.STATE_SWITCHING)

        self.assertTrue(state.set_state(state.STATE_SKIPPED))
        self.assertEqual(state.get_state(), state.STATE_SKIPPED)

    def test_re_entering_the_current_state_is_not_a_transition(self):
        self._walk(state.STATE_MONITORING, state.STATE_RECORDING)

        with self.assertNoLogs(level='WARNING'):
            self.assertFalse(state.set_state(state.STATE_RECORDING))
        self.assertEqual(state.get_state(), state.STATE_RECORDING)


if __name__ == '__main__':
    unittest.main()
