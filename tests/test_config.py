"""Tests for configuration and console setup."""

import io
import sys
import unittest
from unittest import mock

from spytorec.config import _widen_console_encoding


class WidenConsoleEncodingTests(unittest.TestCase):

    def test_a_narrow_stream_carries_the_ui_glyphs_afterwards(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        with mock.patch.object(sys, 'stdout', stream), \
             mock.patch.object(sys, 'stderr', None):
            _widen_console_encoding()

        self.assertEqual(stream.encoding, 'utf-8')
        stream.write("✓ ✗ ▀█")
        stream.flush()

    def test_a_stream_that_will_not_reconfigure_is_not_fatal(self):
        """A plain buffer has no reconfigure; a detached one refuses."""
        refuses = mock.Mock()
        refuses.reconfigure.side_effect = ValueError("detached")
        with mock.patch.object(sys, 'stdout', io.StringIO()), \
             mock.patch.object(sys, 'stderr', refuses):
            _widen_console_encoding()


if __name__ == "__main__":
    unittest.main()
