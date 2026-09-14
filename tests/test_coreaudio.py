"""Tests for the capture device's unity-gain correction. The live case runs on macOS only."""

import ctypes
import sys
import unittest

from spytorec import coreaudio


class ForceUnityGainTests(unittest.TestCase):

    def test_no_op_off_macos(self):
        was_macos, was_ca, was_cf = coreaudio._IS_MACOS, coreaudio._ca, coreaudio._cf
        coreaudio._IS_MACOS, coreaudio._ca, coreaudio._cf = False, None, None
        try:
            self.assertEqual(coreaudio.force_unity_gain('BlackHole 2ch'), [])
            self.assertFalse(coreaudio._load())
        finally:
            coreaudio._IS_MACOS, coreaudio._ca, coreaudio._cf = was_macos, was_ca, was_cf

    def test_unknown_device_is_not_an_error(self):
        self.assertEqual(coreaudio.force_unity_gain('No Such Device 4c1f'), [])


@unittest.skipUnless(sys.platform == 'darwin', "needs CoreAudio")
class LiveCoreAudioTests(unittest.TestCase):
    """Against the machine's real devices."""

    def test_correction_restores_unity(self):
        """Turns a real device down, corrects it, and restores the level it was found at."""
        coreaudio._load()

        target = next((name for name in self._input_device_names()
                       if self._volume(name) is not None), None)
        if target is None:
            self.skipTest("no input device with a settable volume")

        original = self._volume(target)
        try:
            self._set_volume(target, 0.3)
            self.assertAlmostEqual(self._volume(target), 0.3, places=2)

            changed = coreaudio.force_unity_gain(target)
            self.assertTrue(changed, f"'{target}' reported no correction")
            self.assertAlmostEqual(self._volume(target), 1.0, places=3)

            # Already at unity: nothing more to do.
            self.assertEqual(coreaudio.force_unity_gain(target), [])
        finally:
            self._set_volume(target, original)

    def _device(self, name):
        return coreaudio._find_input_device(name)

    def _volume(self, name):
        dev = self._device(name)
        if dev is None:
            return None
        for scope in (coreaudio._SCOPE_OUTPUT, coreaudio._SCOPE_INPUT):
            if coreaudio._has(dev, coreaudio._VOLUME, scope) and \
                    coreaudio._settable(dev, coreaudio._VOLUME, scope):
                return coreaudio._get(dev, coreaudio._VOLUME, scope, ctypes.c_float)
        return None

    def _set_volume(self, name, level):
        dev = self._device(name)
        for scope in (coreaudio._SCOPE_OUTPUT, coreaudio._SCOPE_INPUT):
            if coreaudio._has(dev, coreaudio._VOLUME, scope) and \
                    coreaudio._settable(dev, coreaudio._VOLUME, scope):
                coreaudio._set(dev, coreaudio._VOLUME, scope, ctypes.c_float, level)

    def _input_device_names(self):
        addr = coreaudio._Address(coreaudio._DEVICES, coreaudio._SCOPE_GLOBAL, 0)
        size = ctypes.c_uint32()
        if coreaudio._ca.AudioObjectGetPropertyDataSize(
                coreaudio._SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)):
            return []

        devices = (ctypes.c_uint32 * (size.value // 4))()
        if coreaudio._ca.AudioObjectGetPropertyData(
                coreaudio._SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                ctypes.byref(size), devices):
            return []

        return [name for name in
                (coreaudio._device_name(d) for d in devices
                 if coreaudio._input_channels(d) > 0)
                if name]


if __name__ == '__main__':
    unittest.main()
