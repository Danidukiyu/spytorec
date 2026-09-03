"""Spotify API authentication and retry logic."""

import time
import logging

from spotipy.exceptions import SpotifyException


def spotify_with_retry(sp, max_retries=3):
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
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                raise
    return None
