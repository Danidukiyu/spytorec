"""Linux track source: MPRIS over D-Bus (org.mpris.MediaPlayer2.spotify).

Phase 3 — not implemented yet. Planned backend: ``jeepney`` (pure-Python D-Bus),
event-driven via ``PropertiesChanged``, exposes real Spotify track ids via
``mpris:trackid`` and art URLs via ``mpris:artUrl``.
"""

from typing import Dict, Optional

from spytorec.sources.base import TrackSource


class MprisSource(TrackSource):
    name = "Linux / MPRIS (D-Bus)"
    needs_auth = False

    def __init__(self, cfg):
        self.cfg = cfg

    def connect(self) -> None:
        raise RuntimeError(
            "Linux MPRIS source not implemented yet (roadmap phase 3); "
            "using the Spotify Web API instead"
        )

    def get_playback(self) -> Optional[Dict]:
        return None
