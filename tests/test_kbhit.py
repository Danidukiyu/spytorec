"""Tests for the non-blocking keyboard listener, over real terminal input."""

import os
import sys
import time
import ctypes
import unittest

# Absent on Windows
if os.name != 'nt':
    import pty


def _read_burst_posix(burst: bytes, budget: float = 2.0) -> str:
    """Types `burst` at a child that polls with KBHit, and returns what it saw.

    Runs in a forked child because KBHit puts the terminal in cbreak mode and
    reads fd 0, neither of which survives being shared with the test runner.
    """
    master, slave = pty.openpty()
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.close(read_fd)
        os.dup2(slave, 0)
        os.close(slave)
        sys.stdin = open(0, 'r')

        from spytorec.utils import KBHit
        kb = KBHit()
        kb.set_cbreak()
        seen = []
        deadline = time.time() + budget
        try:
            while time.time() < deadline:
                while kb.kbhit():
                    seen.append(kb.getch())
                time.sleep(0.02)
        finally:
            kb.set_normal_term()
        os.write(write_fd, "".join(seen).encode())
        os._exit(0)

    os.close(slave)
    os.close(write_fd)
    time.sleep(0.3)
    os.write(master, burst)
    os.waitpid(pid, 0)
    seen = os.read(read_fd, 256).decode()
    os.close(read_fd)
    os.close(master)
    return seen


@unittest.skipIf(os.name == 'nt', "posix pty test; Windows uses msvcrt")
class KBHitTests(unittest.TestCase):
    def test_reads_every_character_of_a_burst(self):
        """A burst arrives in one read: no character is left behind.

        The pty maps the typed CR to NL, which the wizard treats alike.
        """
        self.assertEqual(_read_burst_posix(b"12\r"), "12\n")

    def test_lowercases_single_keypresses(self):
        self.assertEqual(_read_burst_posix(b"Q"), "q")


# --- Windows -----------------------------------------------------------------

if os.name == 'nt':
    from ctypes import wintypes

    _KEY_EVENT = 0x0001
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ_WRITE = 0x00000003
    _OPEN_EXISTING = 3
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _KeyEventRecord(ctypes.Structure):
        _fields_ = [('bKeyDown', wintypes.BOOL),
                    ('wRepeatCount', wintypes.WORD),
                    ('wVirtualKeyCode', wintypes.WORD),
                    ('wVirtualScanCode', wintypes.WORD),
                    ('UnicodeChar', wintypes.WCHAR),
                    ('dwControlKeyState', wintypes.DWORD)]

    class _InputRecord(ctypes.Structure):
        _fields_ = [('EventType', wintypes.WORD),
                    ('KeyEvent', _KeyEventRecord)]

    _kernel32 = ctypes.windll.kernel32
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
    _kernel32.WriteConsoleInputW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_InputRecord), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]

    def _console_input():
        """A handle on the console input buffer (CONIN$), or None without a console."""
        handle = _kernel32.CreateFileW(
            "CONIN$", _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ_WRITE,
            None, _OPEN_EXISTING, 0, None)
        if handle == _INVALID_HANDLE_VALUE:
            return None
        return handle

    def _type_into_console(handle, text: str) -> None:
        """Types `text` at the console: a key-down and a key-up per character."""
        records = (_InputRecord * (2 * len(text)))()
        for i, ch in enumerate(text):
            for j, down in enumerate((True, False)):
                rec = records[2 * i + j]
                rec.EventType = _KEY_EVENT
                rec.KeyEvent.bKeyDown = down
                rec.KeyEvent.wRepeatCount = 1
                rec.KeyEvent.wVirtualKeyCode = ord(ch.upper())
                rec.KeyEvent.UnicodeChar = ch
        written = wintypes.DWORD()
        if not _kernel32.WriteConsoleInputW(handle, records, len(records),
                                            ctypes.byref(written)):
            raise OSError(ctypes.get_last_error(), "WriteConsoleInputW failed")

    def _read_burst_windows(handle, text: str, budget: float = 2.0) -> str:
        from spytorec.utils import KBHit
        _kernel32.FlushConsoleInputBuffer(handle)
        kb = KBHit()
        kb.set_cbreak()
        seen = []
        _type_into_console(handle, text)
        deadline = time.time() + budget
        try:
            while time.time() < deadline and len(seen) < len(text):
                while kb.kbhit():
                    seen.append(kb.getch())
                time.sleep(0.02)
        finally:
            kb.set_normal_term()
        return "".join(seen)


@unittest.skipUnless(os.name == 'nt', "Windows console input; POSIX uses a pty")
class WindowsKBHitTests(unittest.TestCase):

    def setUp(self):
        self.handle = _console_input()
        if self.handle is None:
            self.skipTest("no console attached to this process")
        self.addCleanup(_kernel32.CloseHandle, self.handle)

    def test_reads_every_character_of_a_burst(self):
        self.assertEqual(_read_burst_windows(self.handle, "12\r"), "12\r")

    def test_lowercases_single_keypresses(self):
        self.assertEqual(_read_burst_windows(self.handle, "Q"), "q")


if __name__ == "__main__":
    unittest.main()
