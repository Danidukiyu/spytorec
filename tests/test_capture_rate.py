"""Tests for choosing the rate a recording is captured at."""

import unittest
from unittest import mock

from spytorec import hardware
from spytorec.hardware import capture_rate


class CaptureRateTest(unittest.TestCase):

    def _device_at(self, rate):
        return mock.patch.object(hardware.coreaudio, 'nominal_rate', return_value=rate)

    def test_auto_records_at_the_device_rate(self):
        with self._device_at(44100):
            self.assertEqual(capture_rate('auto', 'BlackHole 2ch'), 44100)

    def test_auto_falls_back_where_the_device_rate_is_unknown(self):
        with self._device_at(None):
            self.assertEqual(capture_rate('auto', 'BlackHole 2ch'), 48000)

    def test_a_fixed_rate_is_kept(self):
        with self._device_at(48000):
            self.assertEqual(capture_rate('96000', 'BlackHole 2ch'), 96000)

    def test_a_fixed_rate_off_the_device_rate_warns_of_resampling(self):
        with self._device_at(44100), self.assertLogs(level='WARNING') as logs:
            self.assertEqual(capture_rate('48000', 'BlackHole 2ch'), 48000)

        self.assertIn('resampling', logs.output[0])


if __name__ == '__main__':
    unittest.main()
