"""
Where a recording opens and closes in the captured audio.

The source reports a track change after the track began, and the audio trails
the player's clock further still. The clock says roughly where the boundary
is; the silent gap between two tracks says exactly, when one turns up near
the estimate. The same boundary ends the recording before it: the writer is
kept behind the capture, so the next track's opening can be taken back, and
a recording closed before that opening is captured stays open until it is.
Runs on the audio process's capture loop, unlocked.
"""

import math
import logging
from collections import deque

import numpy as np


# Frames per captured block. Also the granularity of the pre-roll buffer.
BLOCKSIZE = 1024

# A gap between tracks is digital silence; fade-ins and room tone dip below -100 dBFS
_SILENCE_PEAK = 1e-6  # -120 dBFS
_GAP_BLOCKS = 3  # ~64ms at 48kHz

# Ceiling on how long a recording waits for its boundary.
_MAX_DELAY_MS = 2000

# Boundaries the delay is the median of; the configured delay seeds two
_DELAY_SAMPLES = 9

# How far the writer is kept behind the capture
_TAIL_MS = 2000

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
    recording until the boundary arrives; push() returns what the writer takes,
    `_TAIL_MS` behind. end() returns the rest, short of the next track's opening;
    while `closing`, the next boundary has yet to arrive, and ending() returns
    what the closed recording takes until it does.

    `capture_delay_ms` is how far the audio trails the player's clock at a
    track change: the median of the recent boundaries waited for.
    `boundary_window_ms` is how far from the estimate a gap still counts.
    """

    def __init__(self, sr: int, preroll_seconds: float = 5.0,
                 capture_delay_ms: float = 600, boundary_window_ms: float = 800):
        self._block_ms = BLOCKSIZE / sr * 1000
        self._preroll_seconds = preroll_seconds
        self._capture_delay_ms = min(capture_delay_ms, _MAX_DELAY_MS)
        self._delays = deque([self._capture_delay_ms] * 2, maxlen=_DELAY_SAMPLES)
        self._boundary_window_ms = boundary_window_ms

        blocks = max(1, math.ceil(preroll_seconds * sr / BLOCKSIZE))
        self._preroll = deque(maxlen=blocks)

        # Written blocks the writer has yet to take, newest last
        self._tail = deque()
        self._tail_blocks = max(1, self._blocks(_TAIL_MS))

        self._open = False
        self._hold = None

        # Blocks the closed recording has yet to take
        self._ending = []

        logging.info(f"Audio buffer: {preroll_seconds}s ({blocks} blocks), "
                     f"capture delay {self._capture_delay_ms:.0f}ms")

    @property
    def capture_delay_ms(self) -> float:
        """The current estimate of how far the audio trails the player's clock."""
        return self._capture_delay_ms

    @property
    def holding(self) -> bool:
        """The open recording waits for its boundary."""
        return self._open and self._hold is not None

    @property
    def closing(self) -> bool:
        """The closed recording waits for the next boundary."""
        return self._hold is not None and self._hold.closing

    def begin(self, lead_ms: float):
        """Opens a recording at the start of the track, reported `lead_ms` in.
        Returns (blocks of the track already captured, whether now holding).
        """
        self._open = True
        self._tail.clear()

        if self.closing:
            # The recording before ends where this one opens
            return [], True

        self._hold = None
        opening = self._opening_from_buffer(lead_ms)
        if opening:
            return opening, False

        # None of the track is captured yet; hold until it arrives.
        self._hold = self._hold_for(lead_ms, closing=False)
        return [], True

    def push(self, block: np.ndarray) -> list:
        """Takes one captured block. Returns the blocks the writer should take."""
        self._preroll.append(block)

        if self._hold is not None:
            return self._hold_block(block)
        if not self._open:
            return []

        self._tail.append(block)
        if len(self._tail) > self._tail_blocks:
            return [self._tail.popleft()]
        return []

    def end(self, lead_ms: float = None) -> list:
        """
        Closes the recording, returning the rest of it for the writer.
        `lead_ms` is how far into the next track the source reports it, whose
        opening is left out; if none of it is captured yet, the recording is
        `closing` until it is. The pre-roll keeps filling.
        """
        was_open = self._open and self._hold is None
        tail = list(self._tail)
        self._open = False
        self._tail.clear()

        if self._hold is not None:
            # A boundary still ahead: the closing recording takes what was
            # held, and one that never opened takes nothing
            if self._hold.closing:
                self._ending.extend(self._hold.blocks)
            self._hold = None

        if not was_open or lead_ms is None:
            return tail

        taken = len(self._opening_from_buffer(lead_ms))
        if taken == 0:
            # The next track has yet to arrive; this one runs until it does
            self._hold = self._hold_for(lead_ms, closing=True)
            return tail

        if taken > len(tail):
            logging.warning(f"The next track's opening ran {taken - len(tail)} "
                            f"blocks past the tail; kept them")
        return tail[:max(0, len(tail) - taken)]

    def ending(self) -> list:
        """
        The blocks the closed recording takes, since the last call.
        Once `closing` turns false, it has all of them.
        """
        blocks, self._ending = self._ending, []
        return blocks

    # --- the opening is already in the buffer --------------------------------

    def _opening_from_buffer(self, lead_ms: float) -> list:
        """The opening of the track, as far as it is captured: from the first gap
        within the span the track can have been playing. Leaves the estimate alone.
        """
        window = self._window(lead_ms + _CLOCK_SLACK_MS)
        if not window:
            return []

        cut = _first_gap_end(window)

        if cut is None and _silent_run(window) >= _GAP_BLOCKS:
            # A gap still under way: the track opens where it ends
            return []

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

    def _hold_for(self, lead_ms: float, closing: bool) -> '_Hold':
        """A hold for the boundary of a track reported `lead_ms` in."""
        predicted_ms = max(0.0, self._capture_delay_ms - lead_ms)
        hold = _Hold(lead_ms, self._blocks(predicted_ms),
                     self._blocks(self._boundary_window_ms), closing)
        # A gap under way began in the buffer
        hold.run = _silent_run(self._preroll)
        return hold

    def _hold_block(self, block: np.ndarray) -> list:
        """Buffers one block, releasing the recordings either side once the track opens."""
        hold = self._hold
        hold.blocks.append(block)
        index = hold.seen
        hold.seen += 1

        if _is_silent(block):
            hold.run += 1
            gap_end = None
        else:
            gap_end = index if hold.run >= _GAP_BLOCKS else None
            hold.run = 0

        if gap_end is not None and (hold.overran or abs(gap_end - hold.target) <= hold.window):
            # The gap the clock predicted, or one under way when the window
            # closed: the track opens here
            self._learn_delay(hold.lead_ms + gap_end * self._block_ms)
            logging.debug(f"Opened on a gap {gap_end - hold.target} blocks from the estimate")
            opening = hold.blocks[-1:]
        elif index >= hold.target + hold.window and hold.run >= _GAP_BLOCKS:
            # A gap under way: the track opens where it ends. Its silence goes
            # to the closing recording as it comes, or nowhere.
            hold.overran = True
            if hold.closing:
                self._ending.extend(hold.blocks)
            hold.blocks.clear()
            return []
        elif index >= hold.target + hold.window and hold.run > 0:
            # Possibly a gap starting; wait to see
            return []
        elif index >= hold.target + hold.window:
            # No gap within the window: the clock places the opening
            logging.debug("No gap near the estimate; opening where the clock says")
            opening = _without_leading_silence(hold.blocks[hold.target:])
        else:
            return []

        self._hold = None
        if hold.closing:
            self._ending.extend(hold.blocks[:len(hold.blocks) - len(opening)])
        return opening if self._open else []

    def _learn_delay(self, observed_ms: float) -> None:
        """Adds an observed boundary to the estimate; past the ceiling it is discarded."""
        if observed_ms > _MAX_DELAY_MS:
            return

        self._delays.append(max(observed_ms, 0.0))
        self._capture_delay_ms = float(np.median(self._delays))


class _Hold:
    """
    Audio held for a boundary yet to arrive. `closing` when the recording
    before it takes the audio up to the boundary. `overran` once the window
    closed mid-gap; `blocks` then holds only what came since.
    """

    __slots__ = ('blocks', 'seen', 'run', 'overran', 'lead_ms', 'target', 'window', 'closing')

    def __init__(self, lead_ms: float, target: int, window: int, closing: bool):
        self.blocks = []
        self.seen = 0
        self.run = 0
        self.overran = False
        self.lead_ms = lead_ms
        self.target = target
        self.window = window
        self.closing = closing


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


def _silent_run(chunks) -> int:
    """How many silent blocks `chunks` ends with."""
    run = 0
    for chunk in reversed(chunks):
        if not _is_silent(chunk):
            break
        run += 1
    return run


def _without_leading_silence(chunks: list) -> list:
    i = 0
    while i < len(chunks) and _is_silent(chunks[i]):
        i += 1
    return chunks[i:]


def _is_silent(block: np.ndarray) -> bool:
    try:
        return bool(np.max(np.abs(block)) < _SILENCE_PEAK)
    except Exception:
        return False
