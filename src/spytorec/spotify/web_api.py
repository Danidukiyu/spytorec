"""Spotify Web API track source — the original behaviour.

Requires Spotify API credentials in ``config.ini`` and (as of 2025) a Spotify
Premium subscription; non-Premium accounts are blocked from the Web API.
"""

import time
import logging
from typing import Dict, Optional

from spotipy import Spotify
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from spytorec import state
from spytorec.config import console
from spytorec.spotify.base import TrackSource


def _with_retry(sp, max_retries=3):
    """Get Spotify playback with retry logic."""
    for attempt in range(max_retries):
        try:
            return sp.current_playback()
        except SpotifyException as e:
            if e.http_status == 429:
                retry_after = int(e.headers.get('Retry-After', 5))
                logging.warning(f"Rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
            elif attempt < max_retries - 1:
                logging.warning(f"Spotify API error: {e}, retrying...")
                time.sleep(1 * (attempt + 1))
            else:
                raise
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                raise
    return None


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
        return _with_retry(self.sp)
