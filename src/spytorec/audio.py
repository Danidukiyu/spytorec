"""Audio monitoring, metering callback, queue management, and writer worker."""

import time
import queue
import logging
import threading
import warnings

warnings.filterwarnings("ignore", module="soundcard")

import numpy as np
import soundcard as sc

from spytorec import state


# Cached config value for the hot path (set during init)
_smooth_meter = True
_monitor_stop_event = threading.Event()


def init_audio_config(cfg):
    """Cache config values used in the audio callback hot path."""
    global _smooth_meter
    _smooth_meter = cfg['UI'].getboolean('smooth_meter_animation')


def _audio_monitor_worker(mic_id: str, sr: int, ch: int):
    """Dedicated thread that polls soundcard for audio frames, meters them, and queues them."""
    try:
        mics = sc.all_microphones(include_loopback=True)
        mic = next((m for m in mics if m.id == mic_id), None)
        if not mic:
            logging.error(f"Could not find device id: {mic_id}")
            state.set_state(state.STATE_ERROR, "Audio device not found")
            return

        with mic.recorder(samplerate=sr, channels=min(2, ch)) as recorder:
            logging.info(f"Monitor started on device {mic.name} at {sr}Hz")
            
            while not _monitor_stop_event.is_set():
                try:
                    indata = recorder.record(numframes=1024)
                except Exception as e:
                    logging.debug(f"Record error: {e}")
                    time.sleep(0.01)
                    continue
                    
                if len(indata) == 0:
                    time.sleep(0.01)
                    continue

                state.last_heartbeat = time.time()

                if indata.shape[1] >= 2:
                    state.raw_l = float(np.sqrt(np.mean(indata[:, 0]**2)))
                    state.raw_r = float(np.sqrt(np.mean(indata[:, 1]**2)))
                else:
                    state.raw_l = state.raw_r = float(np.sqrt(np.mean(indata[:, 0]**2)))

                alpha = 0.4 if _smooth_meter else 1.0
                state.smoothed_rms_l = (alpha * state.raw_l) + ((1 - alpha) * state.smoothed_rms_l)
                state.smoothed_rms_r = (alpha * state.raw_r) + ((1 - alpha) * state.smoothed_rms_r)

                state.peak_l = max(state.raw_l, state.peak_l * 0.99)
                state.peak_r = max(state.raw_r, state.peak_r * 0.99)

                if state.raw_l > 0.01 and abs(state.raw_l - state.raw_r) < 0.0001:
                    state.mono_warning_frames += 1
                else:
                    state.mono_warning_frames = max(0, state.mono_warning_frames - 2)

                # Pipe audio data to the writer queue
                cur = state.get_state()
                raw_chunk = indata.copy()
                
                if cur == state.STATE_RECORDING:
                    # During recording: drop frames only if queue is completely full
                    try:
                        state.audio_queue.put_nowait(raw_chunk)
                    except queue.Full:
                        pass
                elif cur == state.STATE_MONITORING and state.raw_l > 0.001:
                    # Pre-roll buffer: keep a rolling window so we capture the first beat
                    if state.audio_queue.full():
                        try:
                            state.audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        state.audio_queue.put_nowait(raw_chunk)
                    except queue.Full:
                        pass

    except Exception as e:
        logging.error(f"Monitor worker error: {e}")


def audio_writer_worker():
    """Background thread that writes continuous audio data to the active AudioWriter."""
    while True:
        try:
            chunk = state.audio_queue.get()
            if state.get_state() == state.STATE_RECORDING and state.active_writer:
                try:
                    state.active_writer.write(chunk)
                except Exception as e:
                    logging.debug(f"Audio writer error: {e}")
        except Exception:
            pass


def stop_monitor_stream() -> None:
    """Stops the active audio monitor stream safely."""
    try:
        _monitor_stop_event.set()
        if state.active_monitor_stream and state.active_monitor_stream.is_alive():
            state.active_monitor_stream.join(timeout=1.0)
        state.active_monitor_stream = None
    except Exception as e:
        logging.error(f"Monitor stop failed: {e}")


def start_monitor(mic_id: str, sr: int, ch: int, cfg) -> bool:
    """Spawns the monitoring worker thread."""
    if ch < 2 and cfg['SafetyChecks'].getboolean('validate_stereo'):
        logging.error("Stereo validation failed: need at least 2 channels")
        return False

    try:
        stop_monitor_stream()
        _monitor_stop_event.clear()

        state.active_monitor_stream = threading.Thread(
            target=_audio_monitor_worker, 
            args=(mic_id, sr, ch), 
            daemon=True
        )
        state.active_monitor_stream.start()
        return True
    except Exception as e:
        logging.error(f"Monitor start failed: {e}")
        return False


def start_writer_thread():
    """Starts the audio writer background thread."""
    threading.Thread(target=audio_writer_worker, daemon=True).start()
