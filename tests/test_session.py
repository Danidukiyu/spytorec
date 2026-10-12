"""Recording ownership, confirmed closure, recovery and format consistency."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from spytorec import state
from spytorec.audio_process import AudioProcessError
from spytorec.recording import finalize
from spytorec.session import RecordingSession, effective_output_format, playback_lead_ms
from tests.test_recording import TRACK, config


class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.audio = self.enterContext(mock.patch('spytorec.session.audio'))
        self.enterContext(mock.patch.object(state, 'is_recording', False))
        self.enterContext(mock.patch.object(state, 'watchdog_file_ref', None))
        self.submit = mock.Mock()
        self.report = mock.Mock()
        self.session = RecordingSession(self.root, {'mic_id': 'fake'}, self.submit, self.report)
        self.audio.start_recording.side_effect = lambda path, *_: path.write_bytes(b'open')

    def start(self):
        self.session.start_track(TRACK, '24', 0)
        return self.session.path

    def test_submission_sees_closed_audio(self):
        path = self.start()
        self.audio.stop_recording.side_effect = lambda *_: path.write_bytes(b'closed')
        self.submit.side_effect = lambda _, p: self.assertEqual(p.read_bytes(), b'closed')
        self.session.finish_track(200)
        self.submit.assert_called_once()
        self.assertIsNone(self.session.path)
        self.assertFalse(state.is_recording)

    def test_unconfirmed_close_is_not_submitted_or_deleted(self):
        path = self.start()
        self.audio.stop_recording.side_effect = AudioProcessError('close timed out')
        with self.assertRaises(AudioProcessError):
            self.session.finish_track()
        self.submit.assert_not_called()
        self.assertEqual(self.session.path, path)
        self.assertTrue(path.exists())

    def test_recovery_closes_process_before_reporting_file_and_restarts_capture(self):
        self.enterContext(mock.patch('spytorec.session.time.time_ns', return_value=42))
        path = self.start()
        order = []
        self.audio.shutdown_audio_process.side_effect = lambda: order.append('closed')
        self.report.side_effect = lambda *args: order.append('reported')
        self.audio.start_audio_process.side_effect = lambda **kw: order.append('restarted')
        self.session.recover()
        self.assertEqual(order, ['closed', 'reported', 'restarted'])
        self.assertEqual(self.report.call_args.args[1], str(path.resolve()))
        self.assertTrue(path.exists())
        self.assertFalse(state.is_recording)
        self.assertIsNone(state.watchdog_file_ref)
        # Capture is usable for the next track, with a new temporary file.
        self.session.start_track(dict(TRACK, id='two'), '24', 0)
        self.assertNotEqual(self.session.path, path)
        self.assertTrue(state.is_recording)

    def test_failed_start_never_claims_recording_and_preserves_possible_file(self):
        def fail(path, *_):
            path.write_bytes(b'partial')
            raise AudioProcessError('disk full')
        self.audio.start_recording.side_effect = fail
        with self.assertRaises(AudioProcessError):
            self.start()
        path = self.session.path
        self.assertFalse(state.is_recording)
        self.session.recover()
        self.assertEqual(path.read_bytes(), b'partial')

    def test_shutdown_preserves_file_when_close_confirmation_fails(self):
        path = self.start()
        self.audio.stop_recording.side_effect = AudioProcessError('writer failed')
        with self.assertLogs(level='WARNING'):
            self.session.shutdown()
        self.submit.assert_not_called()
        self.assertTrue(path.exists())
        self.assertEqual(self.report.call_args.args[1], str(path.resolve()))

    def test_discard_waits_for_close_before_deleting(self):
        path = self.start()
        self.audio.stop_recording.side_effect = lambda *_: self.assertTrue(path.exists())
        self.session.finish_track(discard=True)
        self.assertFalse(path.exists())
        self.submit.assert_not_called()

    def test_mp3_fallback_produces_a_valid_tagged_flac(self):
        with self.assertLogs(level='WARNING'):
            output_format = effective_output_format('MP3')
        cfg = config(self.root)
        self.audio.start_recording.side_effect = lambda path, *_: sf.write(
            path, np.random.default_rng(1).uniform(-0.5, 0.5, (24000, 2)),
            48000, subtype='PCM_24')
        results = []
        self.session.submit = lambda track, path: results.append(
            finalize(path, self.root, track, '{title}', output_format, cfg))
        path = self.start()
        self.assertEqual(path.suffix, '.flac')
        self.session.finish_track()
        self.assertTrue(results[0]['ok'])
        self.assertEqual(sf.info(self.root / 'Artist' / 'Album' / 'Song.flac').format, 'FLAC')

    def test_zero_position_is_respected_instead_of_using_the_poll_gap(self):
        with mock.patch('spytorec.session.time.monotonic', return_value=10.1):
            self.assertAlmostEqual(playback_lead_ms({'progress_ms': 0}, 10, 500), 100)
            self.assertAlmostEqual(playback_lead_ms({}, 10, 500), 600)

    def test_position_includes_time_spent_draining_the_previous_track(self):
        with mock.patch('spytorec.session.time.monotonic', return_value=10.4):
            self.assertAlmostEqual(playback_lead_ms({'progress_ms': 200}, 10, 500), 600)
