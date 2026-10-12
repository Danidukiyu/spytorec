"""Own the active recording and the audio process across track changes."""

import logging
import logging.handlers
import time
import uuid

from spytorec import audio_process as audio, state


def audio_log_spec():
    """The parent's file-logging setup, restated for a spawned child.

    Spawned children start with no handlers and no configuration; without
    this, capture errors and shutdown notes would be lost from the log.
    """
    root = logging.getLogger()
    spec = {'enabled': False}
    handler = next((h for h in root.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)), None)
    if handler is not None:
        spec = {
            'enabled': True,
            'path': handler.baseFilename,
            'max_bytes': handler.maxBytes,
            'level': logging.getLevelName(root.level),
        }
    return spec


def effective_output_format(requested):
    """The isolated capture pipeline currently encodes native FLAC only."""
    if requested.lower() == 'mp3':
        logging.warning('MP3 capture is unavailable; using FLAC for this session')
    return 'flac'


def playback_lead_ms(playback, poll_started, poll_gap_ms):
    """Date the reported position, including time spent closing the old file."""
    progress = playback.get('progress_ms')
    if progress is None:
        progress = poll_gap_ms
    return max(0, progress) + (time.monotonic() - poll_started) * 1000


class RecordingSession:
    """Submit only closed recordings and preserve failures before restarting."""

    def __init__(self, out_dir, audio_settings, submit, report_failure):
        self.out_dir = out_dir
        self.audio_settings = audio_settings
        self.submit = submit
        self.report_failure = report_failure
        self.track = None
        self.path = None

    def start_pipeline(self):
        audio.start_audio_process(**self.audio_settings, log_spec=audio_log_spec())
        state.last_heartbeat = time.time()

    def start_track(self, track, bit_depth, lead_ms):
        if self.path is not None:
            raise RuntimeError('Previous recording has not been closed')
        # Retain ownership even if confirmation times out: capture may have opened it.
        self.track = track
        self.path = self.out_dir / f'.tmp_{uuid.uuid4().hex}.flac'
        audio.start_recording(self.path, bit_depth, lead_ms)
        state.is_recording = True
        state.watchdog_file_ref = self.path

    def finish_track(self, lead_ms=None, discard=False):
        if self.path is None:
            return
        audio.stop_recording(lead_ms)
        if discard:
            self.path.unlink(missing_ok=True)
        else:
            self.submit(self.track, self.path)
        self._clear()

    def recover(self):
        # Terminate a wedged process before exposing its file for recovery.
        audio.shutdown_audio_process()
        self._preserve_failure('Capture interrupted; recording retained for recovery')
        self.start_pipeline()

    def shutdown(self):
        try:
            self.finish_track()
        except Exception as e:
            logging.warning(f'Could not confirm final recording: {e}')
        finally:
            audio.shutdown_audio_process()
            self._preserve_failure('Capture did not confirm closure; recording retained')

    def _preserve_failure(self, message):
        if self.path is not None:
            recovery_path = str(self.path.resolve()) if self.path.exists() else None
            self.report_failure(self.track, recovery_path, message)
        self._clear()

    def _clear(self):
        self.track = self.path = None
        state.is_recording = False
        state.watchdog_file_ref = None
