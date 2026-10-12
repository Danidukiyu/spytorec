"""End-to-end test of where a recording starts, through the audio worker itself.

The stand-in track is a linear frequency sweep, so the pitch at the head of the
finished file says where the recording opened.
"""

import sys
import time
import queue
import shutil
import tempfile
import threading
import types
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
import soundfile as sf

from spytorec.boundary import BLOCKSIZE
from spytorec import audio_process


SR = 48000
CH = 2
F0 = 200.0            # sweep start, Hz
SWEEP_RATE = 400.0    # Hz per second
LATE_S = 1.5          # how late the source reports the track
LIVE_S = 2.0          # audio captured after it does
DELAY_MS = 600        # how far the audio trails the player's clock
WINDOW = 4096         # analysis window, ~85ms


def _sweep_block(t0: float) -> np.ndarray:
    """One block of the sweep, starting at `t0` seconds into it."""
    t = t0 + np.arange(BLOCKSIZE) / SR
    phase = 2 * np.pi * (F0 * t + 0.5 * SWEEP_RATE * t ** 2)
    mono = np.sin(phase).astype(np.float32) * 0.5
    return np.repeat(mono[:, None], CH, axis=1)


def _tone_block(hz: float) -> np.ndarray:
    """One block of another track."""
    mono = (np.sin(2 * np.pi * hz * np.arange(BLOCKSIZE) / SR) * 0.5).astype(np.float32)
    return np.repeat(mono[:, None], CH, axis=1)


_SILENCE = np.zeros((BLOCKSIZE, CH), dtype=np.float32)


class _FakeRecorder:
    """Stands in for soundcard's recorder: hands out the blocks the test feeds it."""

    def __init__(self, feed: queue.Queue, closing: threading.Event):
        self._feed = feed
        self._closing = closing

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        while not self._closing.is_set():
            try:
                return self._feed.get(timeout=0.01)
            except queue.Empty:
                continue
        return np.zeros((numframes, CH), dtype=np.float32)


class _FakeMic:
    id = 'fake'
    name = 'Fake Loopback'
    isloopback = True

    def __init__(self, feed, closing):
        self._feed = feed
        self._closing = closing

    def recorder(self, samplerate, channels):
        return _FakeRecorder(self._feed, self._closing)


class CaptureStartTest(unittest.TestCase):
    """Drives the audio worker on a thread, with a fake device."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / 'capture.flac'

        self.feed = queue.Queue()
        self.closing = threading.Event()
        fake = types.ModuleType('soundcard')
        fake.all_microphones = lambda include_loopback=False: [_FakeMic(self.feed, self.closing)]
        real = sys.modules.get('soundcard')
        sys.modules['soundcard'] = fake
        self.addCleanup(self._restore_soundcard, real)

        self.control = queue.Queue()
        self.events = queue.Queue()
        self.status = queue.Queue()
        self.worker = threading.Thread(
            target=audio_process._audio_worker,
            args=(self.control, queue.Queue(), self.events, 'fake', SR, CH, False),
            kwargs={'boundary': {'preroll_seconds': 5.0, 'capture_delay_ms': DELAY_MS,
                                 'boundary_window_ms': 800}, 'status_q': self.status},
            daemon=True)
        self.worker.start()
        self.addCleanup(self._shutdown)

    @staticmethod
    def _restore_soundcard(real):
        if real is None:
            sys.modules.pop('soundcard', None)
        else:
            sys.modules['soundcard'] = real

    def _shutdown(self):
        self.control.put(("shutdown",))
        self.closing.set()
        self.worker.join(timeout=5)

    # --- driving the capture path -----------------------------------------

    def _feed_blocks(self, block: np.ndarray, seconds: float) -> None:
        for _ in range(int(seconds * SR / BLOCKSIZE)):
            self.feed.put(block)
        self._settle()

    def _play(self, from_s: float, seconds: float) -> float:
        """Feeds the sweep to the device. Returns where it got to."""
        blocks = int(seconds * SR / BLOCKSIZE)
        for i in range(blocks):
            self.feed.put(_sweep_block(from_s + i * BLOCKSIZE / SR))
        self._settle()
        return from_s + blocks * BLOCKSIZE / SR

    def _settle(self) -> None:
        """Waits for the worker to have consumed everything fed so far."""
        deadline = time.monotonic() + 5
        while not self.feed.empty():
            if time.monotonic() > deadline:
                self.fail('Audio worker did not consume test audio')
            threading.Event().wait(0.01)
        threading.Event().wait(0.05)

    def _start(self, lead_ms: float, wake_block=None) -> None:
        """Starts a recording; one more block lets the worker read the command."""
        self.control.put(("start", str(self.out), '24', lead_ms))
        self.feed.put(_SILENCE if wake_block is None else wake_block)
        self._settle()

    def _stop(self) -> Path:
        self.control.put(("stop",))
        self.feed.put(_SILENCE)
        self._settle()
        self._shutdown()
        return self.out

    def _record(self, lead_ms: float) -> Path:
        """Plays a track already `LATE_S` old when the source reports it."""
        self._feed_blocks(_SILENCE, 0.4)
        played = self._play(0.0, LATE_S)
        self._start(lead_ms)
        self._play(played, LIVE_S)
        return self._stop()

    def _reported(self, captured_s: float) -> float:
        """What the source reports for a track captured `captured_s` ago."""
        return captured_s * 1000 + DELAY_MS

    def _opened_event(self):
        """The ("opened", replayed_ms, waited, delay_ms) event, or None."""
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None

    # --- reading the result -----------------------------------------------

    def _started_at(self, path: Path) -> float:
        """Seconds into the track that the recording opens; the pitch reads half a window late."""
        samples, _ = sf.read(path, dtype='float32')
        head = samples[:WINDOW, 0]
        spectrum = np.abs(np.fft.rfft(head * np.hanning(len(head))))
        f_head = float(np.fft.rfftfreq(len(head), 1 / SR)[np.argmax(spectrum)])
        return (f_head - F0) / SWEEP_RATE - (WINDOW / SR) / 2

    def _duration(self, path: Path) -> float:
        return sf.info(path).duration

    # --- the track's opening is already captured ---------------------------

    def test_recording_opens_at_the_start_of_the_track(self):
        """The 1.5s the source took to report the change is not lost."""
        out = self._record(self._reported(LATE_S))

        self.assertAlmostEqual(self._started_at(out), 0.0, delta=0.05)
        event = self._opened_event()
        self.assertEqual(event[0], 'opened')
        self.assertFalse(event[2], 'replayed from the buffer, not waited for')
        self.assertAlmostEqual(event[1] / 1000, LATE_S, delta=0.05)

    def test_the_previous_track_is_not_pulled_in(self):
        """An over-estimate of the lateness stops at the gap."""
        self._feed_blocks(_tone_block(3000), 1.0)      # the previous track
        self._feed_blocks(_SILENCE, 0.4)
        played = self._play(0.0, LATE_S)

        self._start(self._reported(3.0))               # claims 3s where 1.5s is right
        self._play(played, LIVE_S)
        out = self._stop()

        self.assertAlmostEqual(self._started_at(out), 0.0, delta=0.05)
        self.assertAlmostEqual(self._duration(out), LATE_S + LIVE_S, delta=0.05)

    # --- the track's opening has not arrived yet ---------------------------

    def test_a_boundary_still_in_flight_is_waited_for(self):
        """The new track is reported while the old one is still playing."""
        self._feed_blocks(_tone_block(3000), 1.0)

        self._start(200)
        self.assertIsNone(self._opened_event(), 'should be waiting for the boundary')

        self._feed_blocks(_tone_block(3000), 0.3)       # still draining
        self._feed_blocks(_SILENCE, 0.4)
        self._play(0.0, LIVE_S)
        out = self._stop()

        event = self._opened_event()
        self.assertTrue(event and event[2], 'should have held for the boundary')
        self.assertAlmostEqual(self._started_at(out), 0.0, delta=0.05)
        self.assertAlmostEqual(self._duration(out), LIVE_S, delta=0.05)

    def test_tracks_that_run_together_open_where_the_clock_says(self):
        """With no gap to find, the estimate is all there is to go on."""
        self._feed_blocks(_tone_block(3000), 1.0)

        self._start(200)                                # boundary predicted 400ms out
        self._feed_blocks(_tone_block(3000), 0.4)       # still draining, and no gap
        self._play(0.0, LIVE_S)
        out = self._stop()

        # accurate to the delay estimate rather than to the sample
        self.assertAlmostEqual(self._started_at(out), 0.0, delta=0.15)

    def test_the_measurement_reads_a_late_start(self):
        """Reported at 0ms in, the recording holds for the capture delay and opens late."""
        played = self._play(0.0, LATE_S)

        self._start(0)
        self._play(played, LIVE_S)
        out = self._stop()

        self.assertAlmostEqual(self._started_at(out), LATE_S + DELAY_MS / 1000, delta=0.05)

    def test_a_recording_stopped_while_holding_is_empty(self):
        """Small enough for finalize's size check to reject."""
        self._feed_blocks(_tone_block(3000), 1.0)

        self._start(200)
        out = self._stop()

        self.assertLess(out.stat().st_size, 8192)

    def test_delayed_stop_preserves_the_tail_without_recording_the_next_track(self):
        self._feed_blocks(_SILENCE, 0.4)
        played = self._play(0, 1.0)
        self._start(self._reported(played), wake_block=_sweep_block(played))
        played += BLOCKSIZE / SR
        # The player has changed tracks, but 400ms of the old audio is still in flight.
        tail_frames = int(0.4 * SR)
        self.control.put(('stop', 200))
        # The worker is already blocked in record(): this block precedes the stop command.
        self.feed.put(_sweep_block(played))
        self._settle()
        played += BLOCKSIZE / SR
        for i in range(int(np.ceil(tail_frames / BLOCKSIZE))):
            block = _sweep_block(played + i * BLOCKSIZE / SR)
            remaining = max(0, tail_frames - i * BLOCKSIZE)
            if remaining < BLOCKSIZE:
                block[remaining:] = _tone_block(3000)[remaining:]
            self.feed.put(block)
        self._settle()
        self._feed_blocks(_tone_block(3000), 0.5)
        self._shutdown()
        self.assertEqual(sf.info(self.out).frames, round(played * SR) + tail_frames)
        samples, _ = sf.read(self.out)
        # The last samples are still the old sweep, not the 3kHz next track.
        t0 = played + (tail_frames - BLOCKSIZE) / SR
        np.testing.assert_allclose(samples[-BLOCKSIZE:], _sweep_block(t0), atol=2e-7)

    def test_transition_stop_trims_next_track_already_in_flight(self):
        """When the next track starts playing before the stop command arrives (Issue #18),
        none of the next track leaks into the first track, and the next track captures its opening.
        """
        out2 = Path(tempfile.mktemp(suffix='.flac'))
        self.addCleanup(out2.unlink, missing_ok=True)

        self._feed_blocks(_SILENCE, 0.4)
        played = self._play(0, 1.0)
        self._start(self._reported(played), wake_block=_sweep_block(played))
        played += BLOCKSIZE / SR

        for _ in range(int(1.0 * SR / BLOCKSIZE)):
            self.feed.put(_sweep_block(played))
            played += BLOCKSIZE / SR
        self._settle()

        # Track 1 ends: silence gap of 100ms (~5 blocks)
        for _ in range(5):
            self.feed.put(_SILENCE)

        # Track 2 begins: 500ms of 3kHz tone arrives before the stop command is received
        t2_blocks = int(0.5 * SR / BLOCKSIZE)
        for _ in range(t2_blocks):
            self.feed.put(_tone_block(3000))
        self._settle()

        # Stop track 1 with lead_ms ~ 500ms
        lead_ms = (t2_blocks * BLOCKSIZE / SR) * 1000
        self.control.put(('command', 101, time.monotonic(), ('stop', lead_ms)))
        self.feed.put(_SILENCE)
        self._settle()

        # Start track 2 with lead_ms
        self.control.put(('command', 102, time.monotonic(), ('start', str(out2), '24', lead_ms)))
        self.feed.put(_SILENCE)
        self._settle()

        # Feed more of track 2
        self._feed_blocks(_tone_block(3000), 1.0)

        # Stop track 2
        self.control.put(('command', 103, time.monotonic(), ('stop', None)))
        self.feed.put(_SILENCE)
        self._settle()

        # Verify track 1 does not contain the 3kHz tone
        data1, _ = sf.read(self.out)
        tail1 = data1[-BLOCKSIZE:]
        self.assertFalse(np.allclose(tail1, _tone_block(3000), atol=1e-3),
                         "Track 2 audio leaked into the end of Track 1")

        # Verify track 2 captured its opening
        data2, _ = sf.read(out2)
        head2 = data2[:BLOCKSIZE]
        np.testing.assert_allclose(head2, _tone_block(3000), atol=1e-3)

    def test_writer_open_failure_is_reported_to_the_parent(self):
        with mock.patch('soundfile.SoundFile', side_effect=OSError('disk full')), \
             self.assertLogs(level='ERROR'):
            self._start(0)
            self.worker.join(timeout=2)
        replies = list(self.status.queue)
        self.assertTrue(any(kind == 'ack' and 'disk full' in (error or '')
                            for kind, _, error in replies))
        self.assertFalse(self.worker.is_alive())

    def test_stop_confirmation_waits_for_writer_close(self):
        real_soundfile = sf.SoundFile
        closing_started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        def writer(**kwargs):
            real = real_soundfile(**kwargs)
            wrapped = mock.Mock(wraps=real)
            def close():
                closing_started.set()
                release.wait(3)
                real.close()
            wrapped.close.side_effect = close
            return wrapped
        self._feed_blocks(_SILENCE, 0.4)
        played = self._play(0, 1.0)
        with mock.patch('soundfile.SoundFile', side_effect=writer):
            self._start(self._reported(played))
            self.control.put(('command', 99, time.monotonic(), ('stop', None)))
            self.feed.put(_SILENCE)
            self.assertTrue(closing_started.wait(2))
            self.assertFalse(any(kind == 'ack' and rid == 99
                                 for kind, rid, _ in list(self.status.queue)))
            release.set()
            self._settle()
        self.assertIn(('ack', 99, None), list(self.status.queue))
        self.assertGreater(sf.info(self.out).frames, 0)

    def test_recording_write_failure_stops_capture_and_reports_an_error(self):
        real_soundfile = sf.SoundFile
        def writer(**kwargs):
            wrapped = mock.Mock(wraps=real_soundfile(**kwargs))
            wrapped.write.side_effect = OSError('write failed')
            return wrapped
        self._feed_blocks(_SILENCE, 0.4)
        played = self._play(0, 1.0)
        with mock.patch('soundfile.SoundFile', side_effect=writer), self.assertLogs(level='ERROR'):
            self._start(self._reported(played))
            self.worker.join(timeout=2)
        self.assertFalse(self.worker.is_alive())
        self.assertTrue(any(kind == 'error' and 'write failed' in error
                            for kind, _, error in list(self.status.queue)))


if __name__ == '__main__':
    unittest.main()
