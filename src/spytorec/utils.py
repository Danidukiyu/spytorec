"""Shared utilities: filesystem helpers, keyboard input, and platform abstractions."""

import os
import sys
import re
from collections import deque
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
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


# Magic-byte signatures for the image formats album art actually arrives in.
_IMAGE_SIGNATURES = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'BM', 'image/bmp'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)


def sniff_image_mime(data: bytes) -> Optional[str]:
    """Identifies an image's MIME type from its leading bytes.
    Needed for sources with no HTTP Content-Type header (e.g. local thumbnails).
    """
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    return None


def fetch_image_bytes(url: str, timeout: float = 3.0,
                       max_bytes: int = 2 * 1024 * 1024) -> Optional[bytes]:
    """Fetches image bytes from an http(s):// or file:// URL, size/timeout
    capped. file:// covers the Windows SMTC source's local thumbnails.
    Returns None on any failure, oversize response, or unsupported scheme.
    """
    if not url:
        return None

    if url.startswith('file://'):
        try:
            from urllib.parse import urlparse
            from urllib.request import url2pathname
            # url2pathname resolves Windows' file:///C:/... form and unquotes
            path = Path(url2pathname(urlparse(url).path))
            if not path.is_file() or path.stat().st_size > max_bytes:
                return None
            return path.read_bytes()
        except OSError as e:
            logging.debug(f"Local image read failed: {e}")
            return None

    try:
        import requests
        response = requests.get(url, timeout=timeout, stream=True)
        if response.status_code != 200:
            return None
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b''.join(chunks)
    except Exception as e:
        logging.debug(f"Image fetch failed: {e}")
        return None


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
        self._pending = deque()
        self.old_term = None
        if os.name != 'nt':
            self.fd = sys.stdin.fileno()
            try:
                self.old_term = termios.tcgetattr(self.fd)
            except termios.error as e:
                # stdin is a pipe or a file: it has no terminal settings to
                # change, and os.read() still delivers what is there.
                logging.debug(f"stdin is not a terminal: {e}")

    def set_normal_term(self):
        if os.name != 'nt' and self.old_term is not None:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old_term)

    def set_cbreak(self):
        if os.name != 'nt' and self.old_term is not None:
            tty.setcbreak(self.fd)

    def _drain(self):
        """Moves whatever the terminal has ready into the pending buffer.

        Reads the raw fd rather than sys.stdin: select() reports on the fd
        alone, so characters sys.stdin has already decoded into its own buffer
        are invisible to it and every character after the first of a burst is
        stranded there.
        """
        while select.select([self.fd], [], [], 0)[0]:
            try:
                chunk = os.read(self.fd, 64)
            except (OSError, BlockingIOError):
                break
            if not chunk:
                break
            self._pending.extend(chunk.decode('utf-8', 'ignore'))

    def kbhit(self):
        if os.name == 'nt':
            return msvcrt.kbhit()
        self._drain()
        return bool(self._pending)

    def getch(self):
        if os.name == 'nt':
            c = msvcrt.getch()
            try:
                return c.decode('utf-8').lower()
            except Exception:
                return ''
        self._drain()
        return self._pending.popleft().lower() if self._pending else ''
