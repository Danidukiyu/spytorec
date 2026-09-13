"""Process-isolated audio pipeline.

The entire audio capture and encoding runs in a dedicated child process
with its own GIL, completely isolated from UI rendering, Spotify polling,
and keyboard handling in the main process.

Communication:
  control_queue (main → audio):  ("start", file_path, bit_depth, lead_ms) | ("stop",) | ("shutdown",)
  meter_queue   (audio → main):  (rms_l, rms_r, peak_l, peak_r, raw_l, raw_r)
  event_queue   (audio → main):  ("opened", replayed_ms, waited, capture_delay_ms)

Every captured block goes through a BoundaryTracker, which opens each
recording on the track's first beat (see boundary.py).
"""

import time
import logging
import warnings
import multiprocessing as mp
from pathlib import Path

import numpy as np

from spytorec.boundary import BLOCKSIZE, BoundaryTracker

def _audio_worker(control_q: mp.Queue, meter_q: mp.Queue, event_q: mp.Queue,
                  mic_id: str, sr: int, ch: int, smooth_meter: bool,
                  mic_name: str = None, boundary: dict = None):
    """Child process entry point. Captures WASAPI audio and encodes to FLAC.
    
    This function runs in its own process with its own GIL, so it is never
    affected by Python-level activity in the main process (UI, Spotify, etc).
    """
    # Import heavy audio libraries inside the child process only
    warnings.filterwarnings("ignore", module="soundcard")
    import soundcard as sc
    import soundfile as sf

    mic = find_device(sc.all_microphones(include_loopback=True), mic_id, mic_name)
    if not mic:
        logging.error(f"Audio process: device not found: {mic_id} ({mic_name})")
        return

    # State
    writer = None  # sf.SoundFile or None
    tracker = BoundaryTracker(sr, **(boundary or {}))
    smoothed_l, smoothed_r = 0.0, 0.0
    peak_l, peak_r = 0.0, 0.0
    alpha = 0.4 if smooth_meter else 1.0
    last_meter_send = 0.0

    try:
        with mic.recorder(samplerate=sr, channels=min(2, ch)) as recorder:
            logging.info(f"Audio process started on {mic.name} at {sr}Hz")

            while True:
                # ── 1. Check for control commands (non-blocking) ──
                try:
                    while True:  # drain all pending commands
                        cmd = control_q.get_nowait()
                        
                        if cmd[0] == "start":
                            _, file_path, bit_depth, lead_ms = cmd
                            subtype_map = {'16': 'PCM_16', '24': 'PCM_24', '32': 'PCM_24'}
                            subtype = subtype_map.get(bit_depth, 'PCM_16')
                            
                            # Close any existing writer first
                            if writer is not None:
                                try:
                                    writer.close()
                                except Exception:
                                    pass
                            
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
                        
                        elif cmd[0] == "stop":
                            tracker.end()
                            if writer is not None:
                                try:
                                    writer.close()
                                except Exception as e:
                                    logging.error(f"Audio process: close error: {e}")
                                writer = None
                                logging.info("Audio process: stopped recording")
                        
                        elif cmd[0] == "shutdown":
                            tracker.end()
                            if writer is not None:
                                try:
                                    writer.close()
                                except Exception:
                                    pass
                            logging.info("Audio process: shutting down")
                            return
                
                except Exception:
                    pass  # queue.Empty — no commands pending

                # ── 2. Capture audio ──
                try:
                    indata = recorder.record(numframes=BLOCKSIZE)
                except Exception:
                    time.sleep(0.01)
                    continue

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
                    _write(writer, due)
                    if was_holding and not tracker.holding:
                        _notify(event_q, "opened", _ms(due, sr), True,
                                tracker.capture_delay_ms)

    except Exception as e:
        logging.error(f"Audio process fatal error: {e}")
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
        try:
            writer.write(np.clip(block, -1.0, 1.0))
        except Exception as e:
            logging.error(f"Audio process: write error: {e}")


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


def start_audio_process(mic_id: str, sr: int, ch: int, smooth_meter: bool = True,
                        mic_name: str = None, boundary: dict = None):
    """Spawn the isolated audio child process.
    `mic_name` finds the device should `mic_id` no longer (see find_device).
    `boundary` holds the BoundaryTracker settings (see boundary.settings_from_config).
    """
    global _audio_proc, _control_q, _meter_q, _event_q

    _control_q = mp.Queue(maxsize=10)
    _meter_q = mp.Queue(maxsize=5)
    _event_q = mp.Queue(maxsize=50)

    _audio_proc = mp.Process(
        target=_audio_worker,
        args=(_control_q, _meter_q, _event_q, mic_id, sr, ch, smooth_meter, mic_name, boundary),
        daemon=True
    )
    _audio_proc.start()
    logging.info(f"Audio process spawned (pid={_audio_proc.pid})")
    return _audio_proc.is_alive()


def send_command(*cmd):
    """Send a control command to the audio process."""
    if _control_q is not None:
        try:
            _control_q.put(cmd, timeout=2.0)
        except Exception as e:
            logging.error(f"Failed to send audio command {cmd[0]}: {e}")


def start_recording(file_path, bit_depth='24', lead_ms=0.0):
    """Tell the audio process to start recording to the given path.
    `lead_ms` is how far into the track the source reports it.
    """
    send_command("start", str(file_path), bit_depth, lead_ms)


def stop_recording():
    """Tell the audio process to stop recording and finalize the file."""
    send_command("stop")


def shutdown_audio_process():
    """Gracefully shut down the audio child process."""
    global _audio_proc
    send_command("shutdown")
    if _audio_proc is not None:
        _audio_proc.join(timeout=5.0)
        if _audio_proc.is_alive():
            logging.warning("Audio process did not exit, terminating.")
            _audio_proc.terminate()
            _audio_proc.join()
        _audio_proc = None


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
