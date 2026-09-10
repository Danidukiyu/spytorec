"""Abstract interface for 'what is Spotify playing right now' providers.

Every source normalises its backend into the single dict shape the recording
engine consumes (a subset of the Spotify Web API ``current_playback`` response)::

    {
        "is_playing": bool,
        "progress_ms": int,
        "item": {
            "id": str,
            "name": str,
            "duration_ms": int,
            "track_number": int,
            "artists": [{"name": str}, ...],
            "album": {
                "name": str,
                "release_date": "YYYY-MM-DD",   # "0000" when the backend can't supply it
                "images": [{"url": str}, ...],  # may be empty
            },
        },
    }

``get_playback()`` returns that dict, or ``None`` when nothing is playable
(nothing playing, backend not running, transient error).
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class TrackSource(ABC):
    """A source of current-playback information."""

    #: Human-readable name for logs / UI.
    name = "unknown"

    #: True when the engine must run the Spotify OAuth flow before use.
    needs_auth = False

    def connect(self) -> None:
        """Perform any one-time setup. Raise on unrecoverable failure."""

    @abstractmethod
    def get_playback(self) -> Optional[Dict]:
        """Return a normalised playback dict, or ``None``."""
        raise NotImplementedError

    def close(self) -> None:
        """Release any resources held by the source."""


def make_playback(*, is_playing: bool, progress_ms, track_id: str, name: str,
                  duration_ms, track_number, artists: Optional[List[str]],
                  album_name: str, release_date: Optional[str] = None,
                  art_url: Optional[str] = None) -> Dict:
    """Build a spec-compliant playback dict from loose backend values.

    Numeric arguments are coerced defensively so a source can pass through
    whatever its backend hands back (often strings) without pre-parsing.
    """
    def _int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    images = [{"url": art_url}] if art_url else []
    names = [a for a in (artists or []) if a] or ["Unknown Artist"]

    return {
        "is_playing": bool(is_playing),
        "progress_ms": _int(progress_ms),
        "item": {
            "id": track_id or "",
            "name": name or "Unknown",
            "duration_ms": _int(duration_ms),
            "track_number": _int(track_number),
            "artists": [{"name": n} for n in names],
            "album": {
                "name": album_name or "Unknown Album",
                "release_date": release_date or "0000",
                "images": images,
            },
        },
    }
