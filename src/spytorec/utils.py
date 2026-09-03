"""Shared utilities: filesystem helpers, keyboard input, and platform abstractions."""

import os
import sys
import re
from pathlib import Path
from contextlib import contextmanager
import logging

# Platform-specific imports
if os.name == 'nt':
    import msvcrt
    try:
        import win32file
        import win32con
        import pywintypes
        HAS_WIN32 = True
    except ImportError:
        HAS_WIN32 = False
else:
    import fcntl
    import termios
    import tty
    import select
    HAS_WIN32 = False


# --- Path Constants ---
BASE_DIR = Path.cwd()  # Use working directory instead of script location for package
CONFIG_FILE_PATH = BASE_DIR / "config.ini"
LOCK_FILE_PATH = BASE_DIR / "spyto.lock"


def resolve_path(path_str) -> Path:
    """Ensures all paths are absolute and relative to the working directory.
    Returns a resolved absolute path; relative inputs are anchored to BASE_DIR.
    """
    p = Path(path_str) if not isinstance(path_str, Path) else path_str
    resolved = p if p.is_absolute() else BASE_DIR / p
    return resolved.resolve()


def clean_filename(name: str) -> str:
    """Strips illegal filesystem characters from filenames (cross-platform safe)."""
    if not name:
        return "unknown"
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]', '', str(name))
    cleaned = cleaned.strip('. ')
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    return cleaned or "unknown"


@contextmanager
def file_lock(lock_path: Path, timeout: float = 5.0):
    """Cross-platform file locking context manager. Degrades gracefully if locking is unavailable."""
    lock_file = None
    locked = False
    try:
        if os.name == 'nt' and HAS_WIN32:
            lock_file = open(lock_path, 'w')
            try:
                win32file.LockFileEx(
                    win32file._get_osfhandle(lock_file.fileno()),
                    win32con.LOCKFILE_EXCLUSIVE_LOCK,
                    0,
                    -0x10000,
                    pywintypes.OVERLAPPED()
                )
                locked = True
            except Exception as lock_err:
                logging.warning(f"File locking (Win32) failed: {lock_err}")
        elif os.name != 'nt':
            lock_file = open(lock_path, 'w')
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except Exception as lock_err:
                logging.warning(f"File locking (fcntl) failed: {lock_err}")

        yield

    finally:
        if lock_file:
            try:
                if locked:
                    if os.name == 'nt' and HAS_WIN32:
                        try:
                            win32file.UnlockFileEx(
                                win32file._get_osfhandle(lock_file.fileno()),
                                0,
                                -0x10000,
                                pywintypes.OVERLAPPED()
                            )
                        except Exception:
                            pass
                    elif os.name != 'nt':
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                lock_file.close()
            except Exception:
                pass


class KBHit:
    """Cross-platform non-blocking keyboard listener."""

    def __init__(self):
        if os.name != 'nt':
            self.fd = sys.stdin.fileno()
            self.old_term = termios.tcgetattr(self.fd)

    def set_normal_term(self):
        if os.name != 'nt':
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old_term)

    def set_cbreak(self):
        if os.name != 'nt':
            tty.setcbreak(self.fd)

    def kbhit(self):
        if os.name == 'nt':
            return msvcrt.kbhit()
        else:
            dr, dw, de = select.select([sys.stdin], [], [], 0)
            return dr != []

    def getch(self):
        if os.name == 'nt':
            c = msvcrt.getch()
            try:
                return c.decode('utf-8').lower()
            except Exception:
                return ''
        else:
            return sys.stdin.read(1).lower()
