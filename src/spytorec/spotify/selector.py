"""Track source selection.

SpytoRec reads "what's playing" from the OS media source on macOS and Windows.
Linux still uses the Spotify Web API, which needs credentials and Premium.
"""

import logging
import platform

from spytorec.spotify.base import TrackSource


def get_source(cfg) -> TrackSource:
    """Build and connect this platform's track source."""
    system = platform.system()

    if system == "Darwin":
        from spytorec.spotify.macos_applescript import AppleScriptSource
        src = AppleScriptSource(cfg)
    elif system == "Windows":
        from spytorec.spotify.windows_smtc import SmtcSource
        src = SmtcSource(cfg)
    else:
        from spytorec.spotify.web_api import WebApiSource
        src = WebApiSource(cfg)

    src.connect()
    logging.info(f"Track source: {src.name}")
    return src
