"""Audio monitoring, metering callback, queue management, and writer worker."""

import time
import queue
import logging
import threading

import numpy as np
import sounddevice as sd

from spytorec import state


# Cached config value for the hot path (set during init)
_smooth_meter = True


def init_audio_config(cfg):
    """Cache config values used in the audio callback hot path."""
    global _smooth_meter
    _smooth_meter = cfg['UI'].getboolean('smooth_meter_animation')


def live_monitor_callback(indata, frames, time_info, status) -> None:
    """Dual-purpose callback: Powers UI meters and pipes audio to FFmpeg."""
    try:
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
        if cur == state.STATE_RECORDING:
            # During recording: drop frames only if queue is completely full
            try:
                state.audio_queue.put_nowait(indata.tobytes())
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
                state.audio_queue.put_nowait(indata.tobytes())
            except queue.Full:
                pass

    except Exception as e:
        logging.debug(f"Monitor callback error: {e}")


def audio_writer_worker():
    """Background thread that writes continuous audio data to FFmpeg stdin."""
    pipe_broken_logged = False
    while True:
        try:
            chunk = state.audio_queue.get()
            if state.get_state() == state.STATE_RECORDING and state.ffmpeg_process and state.ffmpeg_process.stdin:
                try:
                    state.ffmpeg_process.stdin.write(chunk)
                    pipe_broken_logged = False
                except (BrokenPipeError, OSError):
                    if not pipe_broken_logged:
                        logging.debug("FFmpeg stdin pipe closed, waiting for new process")
                        pipe_broken_logged = True
                except Exception as e:
                    if not pipe_broken_logged:
                        logging.debug(f"Audio writer error: {e}")
                        pipe_broken_logged = True
        except Exception:
            pass


def audio_callback_factory(idx: int):
    """Factory for simple meter callbacks used during device discovery."""
    def cb(indata, frames, time_info, status):
        try:
            rms = np.sqrt(np.mean(indata**2))
            with state.meter_lock:
                state.meter_data[idx] = rms
                state.meter_peaks[idx] = max(state.meter_peaks.get(idx, 0.0), rms)
        except Exception:
            pass
    return cb


def stop_monitor_stream() -> None:
    """Stops the active audio monitor stream safely."""
    try:
        if state.active_monitor_stream:
            state.active_monitor_stream.stop()
            state.active_monitor_stream.close()
            state.active_monitor_stream = None
    except Exception as e:
        logging.error(f"Monitor stop failed: {e}")


def start_monitor(idx: int, sr: int, ch: int, cfg) -> bool:
    """Binds to audio device for continuous metering and capture."""
    if ch < 2 and cfg['SafetyChecks'].getboolean('validate_stereo'):
        logging.error("Stereo validation failed: need at least 2 channels")
        return False

    try:
        stop_monitor_stream()

        state.active_monitor_stream = sd.InputStream(
            device=idx,
            channels=min(2, ch),
            samplerate=sr,
            dtype='float32',
            callback=live_monitor_callback,
            blocksize=1024,
            latency='low'
        )
        state.active_monitor_stream.start()
        logging.info(f"Monitor started on device {idx} at {sr}Hz")
        return True
    except Exception as e:
        logging.error(f"Monitor bind failed: {e}")
        return False


def start_writer_thread():
    """Starts the audio writer background thread."""
    threading.Thread(target=audio_writer_worker, daemon=True).start()
