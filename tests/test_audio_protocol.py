"""Exercise confirmation queues and file closure across a real spawned process."""

import sys
import time
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from spytorec import audio_process as audio, state
from spytorec.session import RecordingSession


class _Recorder:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def record(self, numframes):
        time.sleep(numframes / 48000)
        return np.random.default_rng(3).uniform(-0.5, 0.5, (numframes, 2))


class _Mic:
    id = 'fake'
    name = 'Synthetic Loopback'

    def recorder(self, **kwargs):
        return _Recorder()


def _synthetic_worker(*args):
    sys.modules['soundcard'] = types.SimpleNamespace(all_microphones=lambda **kw: [_Mic()])
    audio._audio_worker(*args)


class SpawnedAudioTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(mock.patch.object(audio, '_audio_worker', _synthetic_worker))
        self.addCleanup(audio.shutdown_audio_process)

    def start_pipeline(self):
        audio.start_audio_process('fake', 48000, 2, boundary={
            'capture_delay_ms': 0, 'boundary_window_ms': 0})

    def test_recording_is_readable_as_soon_as_stop_confirms(self):
        self.start_pipeline()
        path = self.root / 'recording.flac'
        audio.start_recording(path)
        time.sleep(0.15)
        audio.stop_recording()
        self.assertGreater(sf.info(path).frames, 1024)
        self.assertEqual(sf.info(path).format, 'FLAC')
        # Another recording can start on the same capture process.
        second = self.root / 'second.flac'
        audio.start_recording(second)
        time.sleep(0.05)
        audio.stop_recording()
        self.assertGreater(sf.info(second).frames, 0)

    def test_missing_device_fails_startup_and_cleans_up_process(self):
        with self.assertRaisesRegex(audio.AudioProcessError, 'Audio device not found'):
            audio.start_audio_process('missing', 48000, 2)
        self.assertFalse(audio.is_audio_alive())
        self.assertIsNone(audio._control_q)

    def test_writer_open_failure_reaches_parent(self):
        self.start_pipeline()
        with self.assertRaises(audio.AudioProcessError):
            audio.start_recording(self.root / 'missing_directory' / 'track.flac')

    def test_a_dead_process_can_be_replaced_and_record_again(self):
        self.enterContext(mock.patch.object(state, 'is_recording', False))
        self.enterContext(mock.patch.object(state, 'watchdog_file_ref', None))
        report = mock.Mock()
        submit = mock.Mock()
        session = RecordingSession(self.root, {
            'mic_id': 'fake', 'sr': 48000, 'ch': 2,
            'boundary': {'capture_delay_ms': 0, 'boundary_window_ms': 0},
        }, submit, report)
        session.start_pipeline()
        session.start_track({'name': 'Interrupted'}, '24', 0)
        time.sleep(0.15)
        interrupted = session.path
        audio._audio_proc.terminate()
        audio._audio_proc.join(timeout=2)
        original_bytes = interrupted.read_bytes()
        session.recover()
        self.assertEqual(interrupted.read_bytes(), original_bytes)
        self.assertEqual(report.call_args.args[1], str(interrupted.resolve()))
        session.start_track({'name': 'Recovered'}, '24', 0)
        path = session.path
        time.sleep(0.05)
        session.finish_track()
        self.assertGreater(sf.info(path).frames, 0)
        submit.assert_called_once()
