"""Tests for the non-blocking keyboard listener, over a real pty."""

import os
import pty
import sys
import time
import unittest


def _read_burst(burst: bytes, budget: float = 2.0) -> str:
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
        self.assertEqual(_read_burst(b"12\r"), "12\n")

    def test_lowercases_single_keypresses(self):
        self.assertEqual(_read_burst(b"Q"), "q")


if __name__ == "__main__":
    unittest.main()
