"""Where a recording opens in the captured audio.

The source reports a track change after the track began, and the audio trails
the player's clock further still. The clock says roughly where the boundary
is; the silent gap between two tracks says exactly, when one turns up near
the estimate. Runs on the audio process's capture loop, unlocked.
"""

import math
import logging
from collections import deque

import numpy as np


# Frames per captured block. Also the granularity of the pre-roll buffer.
BLOCKSIZE = 1024

# A gap between tracks is digital silence; a quiet fade-in stays above -80 dBFS
_SILENCE_RMS = 0.0001
_GAP_BLOCKS = 3  # ~64ms at 48kHz

# Ceiling on how long a recording waits for its boundary.
_MAX_DELAY_MS = 2000

# Slack on the clock, which can report a position its output has passed
_CLOCK_SLACK_MS = 150


def settings_from_config(cfg) -> dict:
    """The tracker's tunables from [Recording], each floored at zero."""
    def _number(key, default):
        try:
            return max(0.0, float(cfg['Recording'].get(key, default)))
        except ValueError:
            return float(default)

    return {
        'preroll_seconds': _number('preroll_seconds', '5'),
        'capture_delay_ms': min(_number('capture_delay_ms', '600'), _MAX_DELAY_MS),
        'boundary_window_ms': _number('boundary_window_ms', '800'),
    }


class BoundaryTracker:
    """Decides which captured blocks a recording gets, and from where.

    Every block goes through push(), recording or not, into a pre-roll buffer.
    begin() replays the track's opening from that buffer, or holds the
    recording until the boundary arrives; push() returns what the writer takes.

    `capture_delay_ms` is how far the audio trails the player's clock at a
    track change, re-estimated from each boundary waited for.
    `boundary_window_ms` is how far from the estimate a gap still counts.
    """

    def __init__(self, sr: int, preroll_seconds: float = 5.0,
                 capture_delay_ms: float = 600, boundary_window_ms: float = 800):
        self._block_ms = BLOCKSIZE / sr * 1000
        self._preroll_seconds = preroll_seconds
        self._capture_delay_ms = min(capture_delay_ms, _MAX_DELAY_MS)
        self._boundary_window_ms = boundary_window_ms

        blocks = max(1, math.ceil(preroll_seconds * sr / BLOCKSIZE))
        self._preroll = deque(maxlen=blocks)

        self._open = False
        self._hold = None

        logging.info(f"Audio buffer: {preroll_seconds}s ({blocks} blocks), "
                     f"capture delay {self._capture_delay_ms:.0f}ms")

    @property
    def capture_delay_ms(self) -> float:
        """The current estimate of how far the audio trails the player's clock."""
        return self._capture_delay_ms

    @property
    def holding(self) -> bool:
        return self._hold is not None

    def begin(self, lead_ms: float):
        """Opens a recording at the start of the track, reported `lead_ms` in.
        Returns (blocks of the track already captured, whether now holding).
        """
        self._open = True
        self._hold = None

        opening = self._opening_from_buffer(lead_ms)
        if opening:
            return opening, False

        # None of the track is captured yet; hold until it arrives.
        predicted_ms = max(0.0, self._capture_delay_ms - lead_ms)
        self._hold = _Hold(lead_ms, self._blocks(predicted_ms),
                           self._blocks(self._boundary_window_ms))
        return [], True

    def push(self, block: np.ndarray) -> list:
        """Takes one captured block. Returns the blocks the writer should take."""
        self._preroll.append(block)

        if not self._open:
            return []
        if self._hold is not None:
            return self._hold_block(block)
        return [block]

    def end(self) -> None:
        """Closes the recording. The pre-roll keeps filling."""
        self._open = False
        self._hold = None

    # --- the opening is already in the buffer --------------------------------

    def _opening_from_buffer(self, lead_ms: float) -> list:
        """The opening of the track, as far as it is captured: from the first gap
        within the span the track can have been playing. Leaves the estimate alone.
        """
        window = self._window(lead_ms + _CLOCK_SLACK_MS)
        if not window:
            return []

        cut = _first_gap_end(window)

        if cut is None:
            # No gap: the clock places the boundary
            age_ms = lead_ms - self._capture_delay_ms
            if age_ms <= 0:
                return []
            cut = max(0, len(window) - self._blocks(age_ms))

        return _without_leading_silence(window[cut:])

    def _window(self, span_ms: float) -> list:
        """The newest `span_ms` of captured audio, oldest first."""
        if span_ms <= 0:
            return []

        chunks = list(self._preroll)
        keep = min(len(chunks), self._blocks(min(span_ms, self._preroll_seconds * 1000)))
        return chunks[len(chunks) - keep:]

    def _blocks(self, ms: float) -> int:
        return max(0, int(round(ms / self._block_ms)))

    # --- the opening has yet to arrive ---------------------------------------

    def _hold_block(self, block: np.ndarray) -> list:
        """Buffers one block, releasing the recording once its track opens."""
        hold = self._hold
        hold.blocks.append(block)
        index = len(hold.blocks) - 1

        if _is_silent(block):
            hold.run += 1
            gap_end = None
        else:
            gap_end = index if hold.run >= _GAP_BLOCKS else None
            hold.run = 0

        if gap_end is not None and abs(gap_end - hold.target) <= hold.window:
            # The gap the clock predicted: the track opens here
            self._learn_delay(hold.lead_ms + gap_end * self._block_ms)
            logging.debug(f"Opened on a gap {gap_end - hold.target} blocks from the estimate")
            opening = hold.blocks[gap_end:]
        elif index >= hold.target + hold.window:
            # No gap within the window: the clock places the opening
            logging.debug("No gap near the estimate; opening where the clock says")
            opening = _without_leading_silence(hold.blocks[hold.target:])
        else:
            return []

        self._hold = None
        return opening

    def _learn_delay(self, observed_ms: float) -> None:
        """Folds an observed boundary into the estimate; past the ceiling it is discarded."""
        if observed_ms > _MAX_DELAY_MS:
            return

        self._capture_delay_ms = (self._capture_delay_ms + max(observed_ms, 0.0)) / 2


class _Hold:
    """Audio held by a recording whose boundary has yet to arrive."""

    __slots__ = ('blocks', 'run', 'lead_ms', 'target', 'window')

    def __init__(self, lead_ms: float, target: int, window: int):
        self.blocks = []
        self.run = 0
        self.lead_ms = lead_ms
        self.target = target
        self.window = window


def _first_gap_end(chunks: list):
    """Index of the first block after a run of `_GAP_BLOCKS` silent blocks."""
    run = 0

    for i, chunk in enumerate(chunks):
        if _is_silent(chunk):
            run += 1
        elif run >= _GAP_BLOCKS:
            return i
        else:
            run = 0

    return None


def _without_leading_silence(chunks: list) -> list:
    i = 0
    while i < len(chunks) and _is_silent(chunks[i]):
        i += 1
    return chunks[i:]


def _is_silent(block: np.ndarray) -> bool:
    try:
        return bool(np.sqrt(np.mean(block ** 2)) < _SILENCE_RMS)
    except Exception:
        return False
