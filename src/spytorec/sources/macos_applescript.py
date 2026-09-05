"""macOS track source: reads the local Spotify.app over AppleScript. No Web
API, credentials, Premium, or quota. The first call raises a one-time macOS
"control Spotify" (Automation) prompt. MediaRemote.framework is avoided - Apple
gated it behind a private entitlement in macOS 15.4+.
"""

import logging
import subprocess
from typing import Dict, Optional

from spytorec.sources.base import TrackSource, make_playback

# Field separator: ASCII RS (0x1e). Will never appear in track metadata.
_SEP = "\x1e"

# "is running" doesn't launch the app. duration is ms, player position is
# seconds. Trailing "END" keeps the field count when artwork url is empty.
_SCRIPT = r'''
if application "Spotify" is running then
    tell application "Spotify"
        set d to (ASCII character 30)
        set pState to (player state as string)
        if pState is "stopped" then return "stopped"
        set t to current track
        return "" & (id of t) & d & (name of t) & d & (artist of t) & d & (album of t) & d & (track number of t) & d & (duration of t) & d & (player position) & d & pState & d & (artwork url of t) & d & "END"
    end tell
else
    return "notrunning"
end if
'''

_FIELD_COUNT = 9


class AppleScriptSource(TrackSource):
    name = "macOS / Spotify.app (AppleScript)"
    needs_auth = False

    def __init__(self, cfg):
        self.cfg = cfg

    def connect(self) -> None:
        result = self._invoke()
        if result is None:
            raise RuntimeError("osascript not available (is this macOS?)")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"osascript exit {result.returncode}")

    @staticmethod
    def _invoke() -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                ["osascript", "-e", _SCRIPT],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logging.debug(f"osascript invocation failed: {exc}")
            return None

    def get_playback(self) -> Optional[Dict]:
        result = self._invoke()
        if result is None:
            return None
        if result.returncode != 0:
            logging.debug(f"osascript error: {result.stderr.strip()}")
            return None

        # strip newlines/spaces only - bare str.strip() also eats the RS separator
        out = result.stdout.strip("\r\n\t ")
        if not out or out in ("notrunning", "stopped"):
            return None

        parts = out.split(_SEP)
        if len(parts) < _FIELD_COUNT:
            logging.debug(f"unexpected AppleScript payload: {out!r}")
            return None

        raw_id, name, artist, album, track_no, duration_ms, position_s, pstate, art_url = parts[:_FIELD_COUNT]
        # id looks like "spotify:track:6rqhFgbbKwnb9MLmUQDhG6"
        track_id = raw_id.rsplit(":", 1)[-1] if raw_id else ""

        try:
            progress_ms = int(float(position_s) * 1000)
        except (TypeError, ValueError):
            progress_ms = 0

        return make_playback(
            is_playing=(pstate == "playing"),
            progress_ms=progress_ms,
            track_id=track_id,
            name=name,
            duration_ms=duration_ms,
            track_number=track_no,
            artists=[artist],
            album_name=album,
            release_date=None,  # not exposed by the Spotify AppleScript dictionary
            art_url=art_url or None,
        )
