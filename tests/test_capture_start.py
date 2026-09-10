"""End-to-end test of where a recording starts, through the audio worker itself.

The stand-in track is a linear frequency sweep, so the pitch at the head of the
finished file says where the recording opened.
"""

import sys
import queue
import shutil
import tempfile
import threading
import types
import unittest
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
        self.worker = threading.Thread(
            target=audio_process._audio_worker,
            args=(self.control, queue.Queue(), self.events, 'fake', SR, CH, False),
            kwargs={'boundary': {'preroll_seconds': 5.0, 'capture_delay_ms': DELAY_MS,
                                 'boundary_window_ms': 800}},
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
        while not self.feed.empty():
            threading.Event().wait(0.01)
        threading.Event().wait(0.05)

    def _start(self, lead_ms: float) -> None:
        """Starts a recording; one more block lets the worker read the command."""
        self.control.put(("start", str(self.out), '24', lead_ms))
        self.feed.put(_SILENCE)
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
        """Small enough for finalize's size check to discard."""
        self._feed_blocks(_tone_block(3000), 1.0)

        self._start(200)
        out = self._stop()

        self.assertLess(out.stat().st_size, 8192)


if __name__ == '__main__':
    unittest.main()
