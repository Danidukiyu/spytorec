"""Tests for the pluggable track-source layer (roadmap item 3, phases 1-2, 4)."""

import asyncio
import unittest
from datetime import timedelta
from unittest import mock

from spytorec.sources.base import TrackSource, make_playback
from spytorec.sources.macos_applescript import AppleScriptSource, _SEP
from spytorec.sources.windows_smtc import SmtcSource, _WINSDK_AVAILABLE


def _fake_cfg():
    return {"SpotifyAPI": {}}


class MakePlaybackTests(unittest.TestCase):
    def test_shape_and_coercion(self):
        pb = make_playback(
            is_playing=True, progress_ms="12345.6", track_id="abc",
            name="Song", duration_ms="200000", track_number="7",
            artists=["A", "B"], album_name="Alb", art_url="http://x/y.jpg",
        )
        self.assertEqual(pb["is_playing"], True)
        self.assertEqual(pb["progress_ms"], 12345)
        item = pb["item"]
        self.assertEqual(item["id"], "abc")
        self.assertEqual(item["duration_ms"], 200000)
        self.assertEqual(item["track_number"], 7)
        self.assertEqual([a["name"] for a in item["artists"]], ["A", "B"])
        self.assertEqual(item["album"]["name"], "Alb")
        self.assertEqual(item["album"]["release_date"], "0000")
        self.assertEqual(item["album"]["images"], [{"url": "http://x/y.jpg"}])

    def test_defaults_when_empty(self):
        pb = make_playback(
            is_playing=False, progress_ms=None, track_id="", name="",
            duration_ms=None, track_number=None, artists=None, album_name="",
        )
        item = pb["item"]
        self.assertEqual(pb["progress_ms"], 0)
        self.assertEqual(item["artists"], [{"name": "Unknown Artist"}])
        self.assertEqual(item["album"]["images"], [])
        self.assertEqual(item["name"], "Unknown")


class AppleScriptParseTests(unittest.TestCase):
    def _source_returning(self, stdout, returncode=0, stderr=""):
        src = AppleScriptSource(_fake_cfg())
        cp = mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)
        src._invoke = mock.Mock(return_value=cp)
        return src

    def test_parses_playing_track(self):
        payload = _SEP.join([
            "spotify:track:6rqhFgbbKwnb9MLmUQDhG6",
            "Bohemian Rhapsody", "Queen", "A Night at the Opera",
            "11", "354320", "42.5", "playing",
            "https://i.scdn.co/image/abc123", "END",
        ])
        pb = self._source_returning(payload).get_playback()
        self.assertIsNotNone(pb)
        self.assertTrue(pb["is_playing"])
        self.assertEqual(pb["progress_ms"], 42500)
        item = pb["item"]
        self.assertEqual(item["id"], "6rqhFgbbKwnb9MLmUQDhG6")
        self.assertEqual(item["name"], "Bohemian Rhapsody")
        self.assertEqual(item["artists"][0]["name"], "Queen")
        self.assertEqual(item["album"]["name"], "A Night at the Opera")
        self.assertEqual(item["track_number"], 11)
        self.assertEqual(item["duration_ms"], 354320)
        self.assertEqual(item["album"]["images"][0]["url"], "https://i.scdn.co/image/abc123")

    def test_paused_track_with_empty_art_still_returns_dict(self):
        # Trailing empty art field + "END" sentinel: parser must not lose fields.
        payload = _SEP.join([
            "spotify:track:x", "N", "A", "Al", "1", "1000", "0.0", "paused", "", "END",
        ])
        pb = self._source_returning(payload + "\n").get_playback()
        self.assertIsNotNone(pb)
        self.assertFalse(pb["is_playing"])
        self.assertEqual(pb["item"]["album"]["images"], [])
        self.assertEqual(pb["item"]["name"], "N")

    def test_sentinels_and_garbage_return_none(self):
        for out in ("notrunning", "stopped", "", "just one field", "a\x1eb"):
            self.assertIsNone(self._source_returning(out).get_playback(), out)

    def test_nonzero_exit_returns_none(self):
        src = self._source_returning("ignored", returncode=1,
                                     stderr="Not authorized to send Apple events")
        self.assertIsNone(src.get_playback())

    def test_invoke_unavailable_returns_none(self):
        src = AppleScriptSource(_fake_cfg())
        src._invoke = mock.Mock(return_value=None)
        self.assertIsNone(src.get_playback())

    def test_connect_raises_on_permission_error(self):
        src = AppleScriptSource(_fake_cfg())
        src._invoke = mock.Mock(return_value=mock.Mock(
            returncode=1, stdout="", stderr="Not authorized to send Apple events to Spotify"))
        with self.assertRaises(RuntimeError):
            src.connect()


class GetSourceTests(unittest.TestCase):
    def test_linux_falls_back_to_web_api_when_os_source_fails(self):
        from spytorec import sources

        boom = mock.Mock()
        boom.connect.side_effect = RuntimeError("no MPRIS")
        with mock.patch("platform.system", return_value="Linux"), \
             mock.patch.object(sources, "_make_os_media_source", return_value=boom):
            # falls through to Web API, which then fails on missing creds
            with self.assertRaises(RuntimeError):
                sources.get_source(_fake_cfg())
            boom.connect.assert_called_once()

    def test_non_linux_has_no_fallback(self):
        from spytorec import sources

        boom = mock.Mock()
        boom.connect.side_effect = RuntimeError("no Spotify app")
        with mock.patch("platform.system", return_value="Darwin"), \
             mock.patch.object(sources, "_make_os_media_source", return_value=boom):
            with self.assertRaisesRegex(RuntimeError, "no Spotify app"):
                sources.get_source(_fake_cfg())

    def test_base_class_contract(self):
        self.assertTrue(issubclass(AppleScriptSource, TrackSource))
        self.assertFalse(AppleScriptSource.needs_auth)


class SmtcSourceTests(unittest.TestCase):
    """Covers the pure logic (session pick, timeline math, art short-circuit).

    winsdk itself is Windows-only and isn't installed in this environment, so
    the parts that call into it directly (connect()'s trial call, get_playback,
    and the WinRT stream-reading half of _save_thumbnail) aren't exercised
    here - see the module docstring for that caveat.
    """

    def test_connect_raises_without_winsdk(self):
        if _WINSDK_AVAILABLE:
            self.skipTest("winsdk is installed in this environment")
        src = SmtcSource(_fake_cfg())
        with self.assertRaises(RuntimeError):
            src.connect()

    def test_find_spotify_session_matches_by_app_id(self):
        spotify_session = mock.Mock(source_app_user_model_id="SpotifyAB.SpotifyMusic_abc!Spotify")
        other_session = mock.Mock(source_app_user_model_id="Microsoft.ZuneMusic_abc!App")
        manager = mock.Mock()
        manager.get_sessions.return_value = [other_session, spotify_session]
        self.assertIs(SmtcSource._find_spotify_session(manager), spotify_session)

    def test_find_spotify_session_none_when_absent(self):
        manager = mock.Mock()
        manager.get_sessions.return_value = [mock.Mock(source_app_user_model_id="Other.App")]
        self.assertIsNone(SmtcSource._find_spotify_session(manager))

    def test_find_spotify_session_handles_get_sessions_error(self):
        manager = mock.Mock()
        manager.get_sessions.side_effect = RuntimeError("boom")
        self.assertIsNone(SmtcSource._find_spotify_session(manager))

    def test_read_timeline(self):
        session = mock.Mock()
        session.get_timeline_properties.return_value = mock.Mock(
            position=timedelta(seconds=42.5),
            start_time=timedelta(0),
            end_time=timedelta(seconds=212.0),
        )
        self.assertEqual(SmtcSource._read_timeline(session), (42500, 212000))

    def test_read_timeline_missing_returns_zero(self):
        session = mock.Mock()
        session.get_timeline_properties.return_value = None
        self.assertEqual(SmtcSource._read_timeline(session), (0, 0))

    def test_read_timeline_error_returns_zero(self):
        session = mock.Mock()
        session.get_timeline_properties.side_effect = RuntimeError("boom")
        self.assertEqual(SmtcSource._read_timeline(session), (0, 0))

    def test_save_thumbnail_none_short_circuits(self):
        src = SmtcSource(_fake_cfg())
        src._tmp_dir = None
        self.assertIsNone(asyncio.run(src._save_thumbnail(None)))

    def test_is_incomplete(self):
        self.assertTrue(SmtcSource._is_incomplete(None))
        self.assertTrue(SmtcSource._is_incomplete(mock.Mock(title="", artist="A")))
        self.assertTrue(SmtcSource._is_incomplete(mock.Mock(title="T", artist="")))
        self.assertFalse(SmtcSource._is_incomplete(mock.Mock(title="T", artist="A")))


if __name__ == "__main__":
    unittest.main()
