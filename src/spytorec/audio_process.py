"""Process-isolated audio pipeline.

The entire audio capture and encoding runs in a dedicated child process
with its own GIL, completely isolated from UI rendering, Spotify polling,
and keyboard handling in the main process.

Communication:
  control_queue (main → audio):  ("command", request_id, sent_at, operation)
  meter_queue   (audio → main):  (rms_l, rms_r, peak_l, peak_r, raw_l, raw_r)
  event_queue   (audio → main):  ("opened", replayed_ms, waited, capture_delay_ms)
  status_queue  (audio → main):  ("ready" | "ack" | "error", request_id, error)

Every captured block goes through a BoundaryTracker, which opens each
recording on the track's first beat (see boundary.py).
"""

import time
import logging
import warnings
import multiprocessing as mp
import queue
import itertools

import numpy as np

from spytorec.boundary import BLOCKSIZE, BoundaryTracker

def _audio_worker(control_q: mp.Queue, meter_q: mp.Queue, event_q: mp.Queue,
                  mic_id: str, sr: int, ch: int, smooth_meter: bool,
                  mic_name: str = None, boundary: dict = None, status_q=None):
    """Child process entry point. Captures WASAPI audio and encodes to FLAC.
    
    This function runs in its own process with its own GIL, so it is never
    affected by Python-level activity in the main process (UI, Spotify, etc).
    """
    # Import heavy audio libraries inside the child process only
    warnings.filterwarnings("ignore", module="soundcard")
    writer = None  # sf.SoundFile or None
    pending_stop = None
    smoothed_l, smoothed_r = 0.0, 0.0
    peak_l, peak_r = 0.0, 0.0
    alpha = 0.4 if smooth_meter else 1.0
    last_meter_send = 0.0

    try:
        import soundcard as sc
        import soundfile as sf
        mic = find_device(sc.all_microphones(include_loopback=True), mic_id, mic_name)
        if mic is None:
            raise RuntimeError(f"Audio device not found: {mic_id} ({mic_name})")
        tracker = BoundaryTracker(sr, **(boundary or {}))
        with mic.recorder(samplerate=sr, channels=min(2, ch)) as recorder:
            logging.info(f"Audio process started on {mic.name} at {sr}Hz")
            _status(status_q, 'ready')

            while True:
                # ── 1. Check for control commands (non-blocking) ──
                while pending_stop is None:
                    try:
                        cmd = control_q.get_nowait()
                    except queue.Empty:
                        break
                    request_id = None
                    command_age_ms = 0
                    if cmd[0] == 'command':
                        _, request_id, sent_at, cmd = cmd
                        command_age_ms = (time.monotonic() - sent_at) * 1000
                    try:
                        if cmd[0] == "start":
                            _, file_path, bit_depth, lead_ms = cmd
                            lead_ms += command_age_ms
                            subtype_map = {'16': 'PCM_16', '24': 'PCM_24', '32': 'PCM_24'}
                            subtype = subtype_map.get(bit_depth, 'PCM_16')
                            
                            # A missed stop must not silently replace an active writer.
                            if writer is not None:
                                raise RuntimeError('Previous recording is still open')
                            
                            writer = sf.SoundFile(
                                file=str(file_path),
                                mode='w',
                                samplerate=sr,
                                channels=min(2, ch),
                                subtype=subtype,
                                format='FLAC'
                            )
                            logging.info(f"Audio process: recording to {file_path}")

                            # The track's opening, as far as it is captured
                            opening, holding = tracker.begin(lead_ms)
                            _write(writer, opening)
                            if not holding:
                                _notify(event_q, "opened", _ms(opening, sr), False,
                                        tracker.capture_delay_ms)
                            _status(status_q, 'ack', request_id)
                        
                        elif cmd[0] == "stop":
                            lead_ms = cmd[1] if len(cmd) > 1 else None
                            if lead_ms is not None:
                                lead_ms += command_age_ms
                            remaining = 0 if lead_ms is None or tracker.holding else \
                                round(max(0, tracker.capture_delay_ms - lead_ms) * sr / 1000)
                            if writer is not None and remaining > 0:
                                pending_stop = (request_id, remaining)
                            else:
                                tracker.end()
                                if writer is not None:
                                    writer.close()
                                    writer = None
                                _status(status_q, 'ack', request_id)
                        
                        elif cmd[0] == "shutdown":
                            tracker.end()
                            if writer is not None:
                                writer.close()
                                writer = None
                            logging.info("Audio process: shutting down")
                            _status(status_q, 'ack', request_id)
                            return
                        else:
                            raise ValueError(f'Unknown audio command: {cmd[0]}')
                    except Exception as e:
                        _status(status_q, 'ack', request_id, str(e))
                        raise

                # ── 2. Capture audio ──
                indata = recorder.record(numframes=BLOCKSIZE)

                if len(indata) == 0:
                    time.sleep(0.01)
                    continue

                # ── 3. Metering ──
                if indata.shape[1] >= 2:
                    raw_l = float(np.sqrt(np.mean(indata[:, 0] ** 2)))
                    raw_r = float(np.sqrt(np.mean(indata[:, 1] ** 2)))
                else:
                    raw_l = raw_r = float(np.sqrt(np.mean(indata[:, 0] ** 2)))

                smoothed_l = (alpha * raw_l) + ((1 - alpha) * smoothed_l)
                smoothed_r = (alpha * raw_r) + ((1 - alpha) * smoothed_r)
                peak_l = max(raw_l, peak_l * 0.99)
                peak_r = max(raw_r, peak_r * 0.99)

                # Send metering to main process (throttled to ~20fps to avoid queue spam)
                now = time.time()
                if now - last_meter_send > 0.05:
                    try:
                        # Clear old meter data if the main process hasn't consumed it yet
                        while not meter_q.empty():
                            try:
                                meter_q.get_nowait()
                            except Exception:
                                break
                        meter_q.put_nowait((smoothed_l, smoothed_r, peak_l, peak_r, raw_l, raw_r))
                    except Exception:
                        pass
                    last_meter_send = now

                # ── 4. Write to FLAC (if recording) ──
                # The tracker returns what the writer takes: nothing while
                # holding for the boundary, the held opening once it passes
                was_holding = tracker.holding
                due = tracker.push(indata)
                if writer is not None:
                    if pending_stop is not None:
                        request_id, remaining = pending_stop
                        due = [indata[:remaining]]
                    _write(writer, due)
                    if was_holding and not tracker.holding:
                        _notify(event_q, "opened", _ms(due, sr), True,
                                        tracker.capture_delay_ms)
                    if pending_stop is not None:
                        remaining -= len(indata)
                        if remaining <= 0:
                            tracker.end()
                            writer.close()
                            writer = None
                            pending_stop = None
                            _status(status_q, 'ack', request_id)
                        else:
                            pending_stop = (request_id, remaining)

    except Exception as e:
        logging.error(f"Audio process fatal error: {e}")
        _status(status_q, 'error', message=str(e))
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        logging.info("Audio process exited")


def find_device(mics, mic_id, mic_name=None):
    """The saved device: by id, compared as text, or failing that by name.
    CoreAudio renumbers its integer ids whenever a device is opened elsewhere.
    """
    by_id = next((m for m in mics if str(m.id) == str(mic_id)), None)
    if by_id is not None or not mic_name:
        return by_id
    return next((m for m in mics if m.name == mic_name), None)


def _write(writer, blocks) -> None:
    for block in blocks:
        writer.write(np.clip(block, -1.0, 1.0))


def _ms(blocks, sr: int) -> float:
    return sum(len(b) for b in blocks) / sr * 1000


def _notify(event_q, *event) -> None:
    """Reports to the main process without waiting; dropped when the queue is full."""
    try:
        event_q.put_nowait(event)
    except Exception:
        pass


# ── Public API for the main process ──────────────────────────────────

_audio_proc = None
_control_q = None
_meter_q = None
_event_q = None
_status_q = None
_request_ids = itertools.count(1)


class AudioProcessError(RuntimeError):
    """Capture failed or did not confirm a command before its deadline."""


def _status(status_q, kind, request_id=None, message=None):
    # Lifecycle messages must not compete with disposable metering/events.
    if status_q is not None:
        status_q.put((kind, request_id, message))


def _wait_status(kind, request_id=None, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reply_kind, reply_id, error = _status_q.get(
                timeout=min(0.1, max(0, deadline - time.monotonic())))
        except queue.Empty:
            if not is_audio_alive():
                raise AudioProcessError('Audio process exited before confirming capture')
            continue
        if reply_kind == 'error':
            raise AudioProcessError(error)
        if reply_kind == kind and reply_id == request_id:
            if error:
                raise AudioProcessError(error)
            return
    raise AudioProcessError(f'Audio process did not confirm {kind} within {timeout}s')


def start_audio_process(mic_id: str, sr: int, ch: int, smooth_meter: bool = True,
                        mic_name: str = None, boundary: dict = None):
    """Spawn the isolated audio child process.
    `mic_name` finds the device should `mic_id` no longer (see find_device).
    `boundary` holds the BoundaryTracker settings (see boundary.settings_from_config).
    """
    global _audio_proc, _control_q, _meter_q, _event_q, _status_q

    if _audio_proc is not None:
        shutdown_audio_process()

    _control_q = mp.Queue(maxsize=10)
    _meter_q = mp.Queue(maxsize=5)
    _event_q = mp.Queue(maxsize=50)
    _status_q = mp.Queue()

    _audio_proc = mp.Process(
        target=_audio_worker,
        args=(_control_q, _meter_q, _event_q, mic_id, sr, ch, smooth_meter,
              mic_name, boundary, _status_q),
        daemon=True
    )
    try:
        _audio_proc.start()
        logging.info(f"Audio process spawned (pid={_audio_proc.pid})")
        _wait_status('ready', timeout=10.0)
    except Exception:
        shutdown_audio_process()
        raise
    return True


def send_command(*cmd):
    """Send a command and wait for capture/close confirmation."""
    if _control_q is None or not is_audio_alive():
        raise AudioProcessError('Audio process is not running')
    request_id = next(_request_ids)
    try:
        _control_q.put(('command', request_id, time.monotonic(), cmd), timeout=2.0)
    except queue.Full as e:
        raise AudioProcessError('Audio command queue is full') from e
    _wait_status('ack', request_id)


def start_recording(file_path, bit_depth='24', lead_ms=0.0):
    """Tell the audio process to start recording to the given path.
    `lead_ms` is how far into the track the source reports it.
    """
    send_command("start", str(file_path), bit_depth, lead_ms)


def stop_recording(lead_ms=None):
    """Close and confirm the recording, draining delayed audio if lead_ms is given.

    At a track change, lead_ms is the new track's elapsed playback time.
    None stops immediately (skip, error recovery, or shutdown).
    """
    send_command("stop", lead_ms)


def shutdown_audio_process():
    """Gracefully shut down the audio child process."""
    global _audio_proc, _control_q, _meter_q, _event_q, _status_q
    if _audio_proc is not None:
        if _audio_proc.is_alive():
            try:
                send_command("shutdown")
            except AudioProcessError as e:
                logging.warning(f'Audio shutdown: {e}')
        if _audio_proc.pid is not None:
            _audio_proc.join(timeout=5.0)
            if _audio_proc.is_alive():
                logging.warning("Audio process did not exit, terminating.")
                _audio_proc.terminate()
                _audio_proc.join(timeout=5.0)
                if _audio_proc.is_alive():
                    raise AudioProcessError('Audio process could not be terminated')
        _audio_proc.close()
        _audio_proc = None
    for q in (_control_q, _meter_q, _event_q, _status_q):
        if q is not None:
            q.cancel_join_thread()
            q.close()
    _control_q = _meter_q = _event_q = _status_q = None


def check_audio_health():
    """Surface capture/write errors even while no command is being sent."""
    if not is_audio_alive():
        raise AudioProcessError('Audio process exited unexpectedly')
    while _status_q is not None:
        try:
            kind, _, error = _status_q.get_nowait()
        except queue.Empty:
            break
        if kind == 'error':
            raise AudioProcessError(error)


def read_meters():
    """Read the latest metering data from the audio process (non-blocking).
    
    Returns (smoothed_l, smoothed_r, peak_l, peak_r, raw_l, raw_r) or None.
    """
    if _meter_q is None:
        return None
    try:
        return _meter_q.get_nowait()
    except Exception:
        return None


def read_events():
    """Yields every ("opened", replayed_ms, waited, capture_delay_ms) event since the last call."""
    if _event_q is None:
        return
    while True:
        try:
            yield _event_q.get_nowait()
        except Exception:
            return


def is_audio_alive():
    """Check if the audio child process is still running."""
    return _audio_proc is not None and _audio_proc.is_alive()
