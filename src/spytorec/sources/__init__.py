"""Track source selection.

SpytoRec reads "what's playing" from the platform's OS media source (the local
Spotify desktop app). On Linux the MPRIS source isn't built yet, so it falls
back to the Spotify Web API when credentials are present. macOS and Windows
have no fallback - their OS media source is the only path.
"""

import logging
import platform

from spytorec.config import console
from spytorec.sources.base import TrackSource


def _make_os_media_source(cfg) -> TrackSource:
    system = platform.system()
    if system == "Darwin":
        from spytorec.sources.macos_applescript import AppleScriptSource
        return AppleScriptSource(cfg)
    if system == "Linux":
        from spytorec.sources.linux_mpris import MprisSource  # noqa: F401  (phase 3)
        return MprisSource(cfg)
    if system == "Windows":
        from spytorec.sources.windows_smtc import SmtcSource
        return SmtcSource(cfg)
    raise RuntimeError(f"no OS media source implemented for platform {system!r}")


def _make_web_api_source(cfg) -> TrackSource:
    from spytorec.sources.web_api import WebApiSource
    src = WebApiSource(cfg)
    src.connect()
    return src


def get_source(cfg) -> TrackSource:
    """Build and connect the track source."""
    try:
        src = _make_os_media_source(cfg)
        src.connect()
    except Exception as exc:
        # Linux's MPRIS source is a stub, so fall back to the Web API there.
        if platform.system() == 'Linux':
            logging.warning(f"OS media source unavailable ({exc}); falling back to Web API")
            console.print(f"[yellow]OS media source unavailable: {exc}[/yellow]")
            console.print("[yellow]Falling back to Spotify Web API...[/yellow]")
            return _make_web_api_source(cfg)
        raise

    logging.info(f"Track source: {src.name}")
    console.print(f"[green]Track source: {src.name}[/green]")
    return src
