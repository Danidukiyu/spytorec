"""Tests for finding the saved capture device."""

import unittest
import queue
import threading
from types import SimpleNamespace
from unittest import mock

from spytorec import audio_process as audio
from spytorec.audio_process import find_device, AudioProcessError


def _mic(id, name):
    return SimpleNamespace(id=id, name=name)


class FindDeviceTest(unittest.TestCase):

    MICS = [_mic(129, 'Crayfish Microphone'), _mic(77, 'BlackHole 2ch')]

    def test_an_integer_id_matches_its_saved_text(self):
        self.assertIs(find_device(self.MICS, '77', 'BlackHole 2ch'), self.MICS[1])

    def test_a_renumbered_device_is_found_by_name(self):
        renumbered = [_mic(130, 'Crayfish Microphone'), _mic(78, 'BlackHole 2ch')]

        self.assertIs(find_device(renumbered, 77, 'BlackHole 2ch'), renumbered[1])
        self.assertIsNone(find_device(renumbered, 77, 'Soundflower'))


class CommandConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.control = queue.Queue()
        self.status = queue.Queue()
        self.enterContext(mock.patch.object(audio, '_control_q', self.control))
        self.enterContext(mock.patch.object(audio, '_status_q', self.status))
        self.alive = self.enterContext(mock.patch.object(audio, 'is_audio_alive', return_value=True))

    def test_start_waits_for_the_matching_confirmation(self):
        done = threading.Event()
        errors = []
        def send():
            try:
                audio.start_recording('recording.flac')
            except Exception as e:
                errors.append(e)
            finally:
                done.set()
        worker = threading.Thread(target=send)
        worker.start()
        self.addCleanup(worker.join, 6)
        _, request_id, _, cmd = self.control.get(timeout=1)
        self.assertEqual(cmd[0], 'start')
        self.status.put(('ack', request_id - 1, None))
        self.assertFalse(done.wait(0.05), 'an older reply must not confirm this recording')
        self.status.put(('ack', request_id, None))
        self.assertTrue(done.wait(1))
        self.assertEqual(errors, [])

    def test_start_surfaces_writer_open_error(self):
        def reply():
            _, request_id, _, _ = self.control.get(timeout=1)
            self.status.put(('ack', request_id, 'disk full'))
        worker = threading.Thread(target=reply)
        worker.start()
        self.addCleanup(worker.join, 2)
        with self.assertRaisesRegex(AudioProcessError, 'disk full'):
            audio.start_recording('recording.flac')

    def test_dead_process_rejects_commands(self):
        self.alive.return_value = False
        with self.assertRaisesRegex(AudioProcessError, 'not running'):
            audio.stop_recording()
        self.assertTrue(self.control.empty())

    def test_a_full_control_queue_is_an_error(self):
        self.enterContext(mock.patch.object(self.control, 'put', side_effect=queue.Full))
        with self.assertRaisesRegex(AudioProcessError, 'queue is full'):
            audio.start_recording('recording.flac')

    def test_missing_confirmation_has_a_deadline(self):
        with self.assertRaisesRegex(AudioProcessError, 'did not confirm'):
            audio._wait_status('ack', request_id=1, timeout=0.02)

    def test_capture_write_error_is_reported_without_a_pending_command(self):
        self.status.put(('error', None, 'write failed'))
        with self.assertRaisesRegex(AudioProcessError, 'write failed'):
            audio.check_audio_health()


if __name__ == '__main__':
    unittest.main()
