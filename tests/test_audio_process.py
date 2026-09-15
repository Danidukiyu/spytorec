"""Tests for finding the saved capture device."""

import unittest
from types import SimpleNamespace

from spytorec.audio_process import find_device


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


if __name__ == '__main__':
    unittest.main()
