"""Spotify Web API track source — the original behaviour.

Requires Spotify API credentials in ``config.ini`` and (as of 2025) a Spotify
Premium subscription; non-Premium accounts are blocked from the Web API.
"""

import logging
from typing import Dict, Optional

from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

from spytorec import state
from spytorec.config import console
from spytorec.sources.base import TrackSource
from spytorec.spotify import spotify_with_retry


class WebApiSource(TrackSource):
    """Polls ``sp.current_playback()``; the response already matches our shape."""

    name = "Spotify Web API"
    needs_auth = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.sp = None

    def connect(self) -> None:
        client_id = self.cfg['SpotifyAPI'].get('spotipy_client_id')
        client_secret = self.cfg['SpotifyAPI'].get('spotipy_client_secret')

        if not client_id or not client_secret:
            raise RuntimeError(
                "Spotify credentials missing from config.ini under [SpotifyAPI]"
            )

        self.sp = Spotify(auth_manager=SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=state.SPOTIPY_REDIRECT_URI,
            scope=state.SPOTIPY_SCOPE,
            open_browser=True,
        ))
        # Force a token exchange / verify the account can reach the Web API.
        self.sp.current_user()
        logging.info("Spotify Web API authentication successful")
        console.print("[green]Spotify authentication successful![/green]")

    def get_playback(self) -> Optional[Dict]:
        return spotify_with_retry(self.sp)
