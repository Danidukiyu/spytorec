"""Tests for deciding where a track opens in the captured audio."""

import unittest

import numpy as np

from spytorec.boundary import BLOCKSIZE, BoundaryTracker


SR = 48000
CH = 2
BLOCK_MS = BLOCKSIZE / SR * 1000          # ~21.3ms


def _block(level: float) -> np.ndarray:
    """One captured block at a constant amplitude."""
    return np.full((BLOCKSIZE, CH), level, dtype=np.float32)


class BoundaryTest(unittest.TestCase):

    def setUp(self):
        self.tracker = BoundaryTracker(SR, preroll_seconds=5.0,
                                       capture_delay_ms=600, boundary_window_ms=800)

    def _fill(self, count: int, level: float = 0.5) -> None:
        for _ in range(count):
            self.tracker.push(_block(level))

    def _feed_held(self, levels) -> int:
        """Feeds blocks to a holding recording, returning how many were released."""
        return sum(len(self.tracker.push(_block(level))) for level in levels)

    def test_a_gap_far_from_the_estimate_does_not_open_a_held_recording(self):
        """A silence nowhere near the estimate is not the boundary."""
        self.tracker = BoundaryTracker(SR, capture_delay_ms=1800)
        self._fill(40, level=0.4)
        _, holding = self.tracker.begin(0)            # boundary predicted ~1.8s out
        self.assertTrue(holding)

        written = self._feed_held([0.0] * 4 + [0.4] * 4)

        self.assertEqual(written, 0, 'released on a gap nowhere near the estimate')
        self.assertTrue(self.tracker.holding)

    def test_a_gap_running_past_the_window_opens_where_it_ends(self):
        """The window closes mid-gap: no silence is written ahead of the track."""
        self._fill(40, level=0.4)
        self.tracker.begin(0)                         # boundary predicted ~600ms out

        written = []
        for level in [0.0] * 120 + [0.5] * 4:         # a 2.5s gap
            written += self.tracker.push(_block(level))
        written += self.tracker.end()

        self.assertEqual(len(written), 4, 'wrote the gap')
        self.assertGreater(min(np.min(b) for b in written), 0.0)

    def test_a_short_silence_at_the_window_edge_is_not_a_gap(self):
        self._fill(40, level=0.4)
        self.tracker.begin(0)
        edge = round(600 / BLOCK_MS) + round(800 / BLOCK_MS)

        written = self._feed_held([0.4] * edge + [0.0] * 2 + [0.5] * 4)
        written += len(self.tracker.end())

        self.assertGreater(written, 4, 'held past a short silence')

    # --- a quiet fade-in is part of the track -----------------------------

    def test_a_quiet_fade_in_in_the_buffer_is_kept(self):
        """A -90 dBFS opening is the track, not the gap before it."""
        self._fill(10, level=0.4)
        self._fill(6, level=0.0)
        self._fill(3, level=3e-5)
        self._fill(23, level=0.5)

        opening, holding = self.tracker.begin(20 * BLOCK_MS + self.tracker.capture_delay_ms)

        self.assertFalse(holding)
        self.assertEqual(len(opening), 26, 'trimmed the fade-in')

    def test_a_quiet_fade_in_after_a_held_gap_is_kept(self):
        self._fill(40, level=0.4)
        self.tracker.begin(200)

        written = self._feed_held([0.4] * 16 + [0.0] * 4 + [3e-5] * 3 + [0.5] * 4)
        written += len(self.tracker.end())

        self.assertEqual(written, 7, 'trimmed the fade-in')

    # --- a recording ends where the next track opens ----------------------

    def _record(self, levels) -> list:
        """Opens a recording on the buffer, then captures `levels` into it."""
        self._fill(40, level=0.4)
        opening, holding = self.tracker.begin(20 * BLOCK_MS + self.tracker.capture_delay_ms)
        self.assertFalse(holding)
        return opening + [b for level in levels for b in self.tracker.push(_block(level))]

    def test_the_writer_is_kept_behind_the_capture(self):
        written = self._record([0.4] * 30)

        self.assertEqual(len(written), 20, 'wrote the tail before the recording ended')

    def test_the_next_track_opening_after_a_gap_is_left_out(self):
        written = self._record([0.4] * 30 + [0.0] * 5 + [0.8] * 12)
        written += self.tracker.end(12 * BLOCK_MS + self.tracker.capture_delay_ms)

        self.assertEqual(len(written), 20 + 30 + 5)
        self.assertLess(max(np.max(b) for b in written), 0.8, 'kept the next track')

    def test_the_next_track_opening_without_a_gap_is_left_out_by_the_clock(self):
        written = self._record([0.4] * 30 + [0.8] * 12)
        written += self.tracker.end(12 * BLOCK_MS + self.tracker.capture_delay_ms)

        self.assertEqual(len(written), 20 + 30)

    def test_a_recording_with_no_next_track_is_written_whole(self):
        written = self._record([0.4] * 30)
        written += self.tracker.end()

        self.assertEqual(len(written), 20 + 30)

    # --- a recording stopped early runs on to the next track --------------

    def _switch_early(self, levels):
        """
        Stops a recording 100ms into the next track, before any of it is captured,
        starts the next, then captures `levels`. Returns what each recording wrote.
        """
        before = self._record([0.4] * 30) + self.tracker.end(100)
        self.assertTrue(self.tracker.closing)

        opening, holding = self.tracker.begin(200)
        self.assertEqual(opening, [])
        self.assertTrue(holding)

        after = []
        for level in levels:
            after += self.tracker.push(_block(level))
            before += self.tracker.ending()
        return before, after + self.tracker.end()

    def test_the_end_of_a_track_stopped_early_is_kept(self):
        before, after = self._switch_early([0.4] * 20 + [0.0] * 5 + [0.8] * 10)

        self.assertFalse(self.tracker.closing)
        self.assertEqual(len(before), 20 + 30 + 20 + 5, 'lost the end of the track')
        self.assertEqual(len(after), 10)
        self.assertLess(max(np.max(b) for b in before), 0.8, 'kept the next track')
        self.assertGreater(min(np.min(b) for b in after), 0.4, 'took the track before')

    def test_without_a_gap_the_clock_ends_a_track_stopped_early(self):
        before, after = self._switch_early([0.4] * 40 + [0.8] * 40)

        self.assertFalse(self.tracker.closing)
        self.assertEqual(len(before) + len(after), 20 + 30 + 80)
        self.assertGreater(len(before), 20 + 30, 'lost the end of the track')

    def test_a_track_stopped_early_with_none_after_still_ends_on_the_boundary(self):
        before = self._record([0.4] * 30) + self.tracker.end(100)

        for level in [0.4] * 20 + [0.0] * 5 + [0.8] * 10:
            self.assertEqual(self.tracker.push(_block(level)), [])
            before += self.tracker.ending()

        self.assertFalse(self.tracker.closing)
        self.assertEqual(len(before), 20 + 30 + 20 + 5)

    def test_a_gap_begun_before_the_stop_still_ends_the_track(self):
        """The gap straddles the stop; its silence before the stop counts."""
        before = self._record([0.4] * 30 + [0.0] * 4) + self.tracker.end(100)

        for level in [0.0] * 2 + [0.8] * 40:
            self.tracker.push(_block(level))
            before += self.tracker.ending()

        self.assertFalse(self.tracker.closing)
        self.assertEqual(len(before), 20 + 30 + 4 + 2, 'ran into the next track')

    def test_a_gap_under_way_ends_the_track_when_it_ends(self):
        """Reported past the estimate, mid-gap: the track ends where the gap does."""
        lead = self.tracker.capture_delay_ms + 10 * BLOCK_MS
        before = self._record([0.4] * 30 + [0.0] * 4) + self.tracker.end(lead)
        self.assertTrue(self.tracker.closing)

        for level in [0.0] * 3 + [0.8] * 40:
            self.tracker.push(_block(level))
            before += self.tracker.ending()

        self.assertEqual(len(before), 20 + 30 + 4 + 3)
        self.assertLess(max(np.max(b) for b in before), 0.8, 'kept the next track')

    def test_a_gap_running_past_the_window_ends_the_track_when_it_ends(self):
        before = self._record([0.4] * 30) + self.tracker.end(100)

        for level in [0.4] * 20 + [0.0] * 120 + [0.8] * 10:
            self.tracker.push(_block(level))
            before += self.tracker.ending()

        self.assertFalse(self.tracker.closing)
        self.assertEqual(len(before), 20 + 30 + 20 + 120)
        self.assertLess(max(np.max(b) for b in before), 0.8, 'kept the next track')

    def test_stopping_again_ends_a_closing_track_where_it_is(self):
        before = self._record([0.4] * 30) + self.tracker.end(100)
        self._fill(10, level=0.4)

        self.tracker.end()
        before += self.tracker.ending()

        self.assertFalse(self.tracker.closing)
        self.assertEqual(len(before), 20 + 30 + 10)

    # --- the estimate corrects itself -------------------------------------

    def _late_boundary(self, late_blocks: int) -> None:
        """One recording held for a boundary `late_blocks` past the estimate."""
        self._fill(40, level=0.4)
        self.tracker.begin(200)
        target = round((self.tracker.capture_delay_ms - 200) / BLOCK_MS)
        self._feed_held([0.4] * (target + late_blocks - 5) + [0.0] * 5 + [0.5] * 4)
        self.tracker.end()

    def test_boundaries_found_late_move_the_estimate_out(self):
        before = self.tracker.capture_delay_ms

        self._late_boundary(10)
        self._late_boundary(10)

        self.assertGreater(self.tracker.capture_delay_ms, before)

    def test_one_late_boundary_leaves_the_estimate_alone(self):
        """A single outlier is not the delay."""
        before = self.tracker.capture_delay_ms

        self._late_boundary(30)

        self.assertEqual(self.tracker.capture_delay_ms, before)

    def test_a_gap_already_in_the_buffer_leaves_the_estimate_alone(self):
        """Only a boundary waited for measures the delay."""
        self._fill(10, level=0.4)
        self._fill(6, level=0.0)
        self._fill(26, level=0.5)
        before = self.tracker.capture_delay_ms

        opening, holding = self.tracker.begin(20 * BLOCK_MS + before)

        self.assertFalse(holding)
        self.assertEqual(len(opening), 26, 'should have opened on the gap')
        self.assertEqual(self.tracker.capture_delay_ms, before)

    def test_a_gap_past_the_ceiling_does_not_walk_the_estimate_up(self):
        self.tracker = BoundaryTracker(SR, capture_delay_ms=1900)
        self._fill(40, level=0.4)
        self.tracker.begin(1000)                      # boundary predicted ~900ms out

        self._feed_held([0.4] * 65 + [0.0] * 5 + [0.5] * 4)

        self.assertLessEqual(self.tracker.capture_delay_ms, 1900)


if __name__ == '__main__':
    unittest.main()
