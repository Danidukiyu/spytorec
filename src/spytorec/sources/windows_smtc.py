"""Windows track source: reads Spotify's now-playing session from the system
media controls (GlobalSystemMediaTransportControls). No Web API, credentials,
Premium, or quota.

SMTC gives no Spotify track id (one is synthesised from artist+title, so
blocklist ``id:`` entries only match within the same machine), no art CDN url
(the raw thumbnail is cached to a temp file, served as ``file://``), and no
release date (year tag falls back to "0000"). Position updates only when the
app pushes one, not on a clock, so the progress figure steps rather than
ticks; and for ~0.5s after a skip it updates fields independently, sometimes
blanking one - see the ``_is_incomplete`` / ``_last_good`` guard.
"""

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Dict, Optional

from spytorec.sources.base import TrackSource, make_playback

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PlaybackStatus,
    )
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions
    _WINSDK_AVAILABLE = True
except ImportError:
    _WINSDK_AVAILABLE = False

# Matches both the Microsoft Store AUMID (SpotifyAB.SpotifyMusic_...!Spotify)
# and the plain Win32 install, which tends to report just "Spotify.exe".
_APP_ID_HINT = "spotify"
_THUMB_MAX_BYTES = 4 * 1024 * 1024


class SmtcSource(TrackSource):
    name = "Windows / SMTC"
    needs_auth = False

    def __init__(self, cfg):
        self.cfg = cfg
        self._tmp_dir: Optional[Path] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_good: Optional[Dict] = None

    def connect(self) -> None:
        if not _WINSDK_AVAILABLE:
            raise RuntimeError(
                "winsdk is not installed, or this isn't Windows (pip install winsdk)"
            )
        # One loop for the source's lifetime; asyncio.run() per poll wastes
        # work and fails if the caller already has a running loop.
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._request_manager())
        except Exception as e:
            self._loop.close()
            self._loop = None
            raise RuntimeError(f"could not reach the SMTC session manager: {e}") from e

        self._tmp_dir = Path(tempfile.mkdtemp(prefix="spytorec_art_"))

    @staticmethod
    async def _request_manager():
        # winsdk's *_async() returns an IAsyncOperation, not a coroutine: await
        # it inside an async def, don't hand it to run_until_complete().
        return await _SessionManager.request_async()

    def close(self) -> None:
        if self._loop is not None:
            self._loop.close()
            self._loop = None
        if not self._tmp_dir:
            return
        try:
            for f in self._tmp_dir.glob("*"):
                f.unlink(missing_ok=True)
            self._tmp_dir.rmdir()
        except OSError as e:
            logging.debug(f"SMTC temp art cleanup failed: {e}")

    def get_playback(self) -> Optional[Dict]:
        if self._loop is None:
            return None
        try:
            return self._loop.run_until_complete(self._get_playback_async())
        except Exception as e:
            logging.debug(f"SMTC read failed: {e}")
            return None

    async def _get_playback_async(self) -> Optional[Dict]:
        manager = await self._request_manager()
        session = self._find_spotify_session(manager)
        if session is None:
            return None

        props = await session.try_get_media_properties_async()

        playback_info = session.get_playback_info()
        is_playing = bool(
            playback_info and playback_info.playback_status == _PlaybackStatus.PLAYING
        )

        # For ~0.5s after a skip SMTC updates fields independently and may blank
        # one; reuse the last complete frame to avoid a junk id and a phantom
        # one-second recording.
        if self._is_incomplete(props):
            if self._last_good is not None:
                stale = dict(self._last_good)
                stale["is_playing"] = is_playing
                return stale
            return None

        progress_ms, duration_ms = self._read_timeline(session)
        art_url = await self._save_thumbnail(props.thumbnail)

        track_id = "local:" + hashlib.sha1(
            f"{props.artist}\x1f{props.title}".encode("utf-8", "ignore")
        ).hexdigest()[:16]

        playback = make_playback(
            is_playing=is_playing,
            progress_ms=progress_ms,
            track_id=track_id,
            name=props.title,
            duration_ms=duration_ms,
            track_number=props.track_number,
            artists=[props.artist],
            album_name=props.album_title,
            release_date=None,  # SMTC exposes no release date
            art_url=art_url,
        )
        self._last_good = playback
        return playback

    @staticmethod
    def _is_incomplete(props) -> bool:
        """True for a media-properties frame that isn't safe to act on -
        SMTC serves these mid-skip, with the title or artist not yet filled in.
        """
        return props is None or not props.title or not props.artist

    @staticmethod
    def _find_spotify_session(manager):
        """Picks the Spotify session out of every app currently in SMTC.

        get_current_session() is whichever app last had "now playing" focus,
        which may not be Spotify - so we scan get_sessions() by app id instead.
        """
        try:
            sessions = manager.get_sessions()
        except Exception as e:
            logging.debug(f"SMTC get_sessions failed: {e}")
            return None

        for session in sessions:
            app_id = (getattr(session, "source_app_user_model_id", "") or "").lower()
            if _APP_ID_HINT in app_id:
                return session
        return None

    @staticmethod
    def _read_timeline(session):
        try:
            timeline = session.get_timeline_properties()
            if timeline is None:
                return 0, 0
            progress_ms = int(timeline.position.total_seconds() * 1000)
            duration_ms = int((timeline.end_time - timeline.start_time).total_seconds() * 1000)
            return max(0, progress_ms), max(0, duration_ms)
        except Exception as e:
            logging.debug(f"SMTC timeline read failed: {e}")
            return 0, 0

    async def _save_thumbnail(self, thumbnail) -> Optional[str]:
        if thumbnail is None or self._tmp_dir is None:
            return None
        try:
            stream = await thumbnail.open_read_async()
            size = stream.size
            if size <= 0 or size > _THUMB_MAX_BYTES:
                return None

            buffer = Buffer(size)
            await stream.read_async(buffer, size, InputStreamOptions.READ_AHEAD)
            reader = DataReader.from_buffer(buffer)
            data = bytearray(size)
            reader.read_bytes(data)

            path = self._tmp_dir / "cover.img"
            path.write_bytes(bytes(data))
            return path.as_uri()
        except Exception as e:
            logging.debug(f"SMTC thumbnail read failed: {e}")
            return None
