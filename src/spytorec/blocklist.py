"""Track blocklist loading and matching."""

import logging
from typing import Dict, List, Tuple

from spytorec.utils import resolve_path


def load_blocklist() -> List[str]:
    """Loads the blocklist.txt file into a list of rules."""
    blocklist = []
    path = resolve_path('blocklist.txt')
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                blocklist = [line.strip().lower() for line in f if line.strip() and not line.startswith('#')]
        except Exception as e:
            logging.error(f"Failed to load blocklist: {e}")
    return blocklist


def is_track_blocked(track: Dict, blocklist: List[str]) -> Tuple[bool, str]:
    """Checks if a track matches any blocklist rule."""
    if not blocklist:
        return False, ""

    track_id = track.get('id', '').lower()
    title = track.get('name', '').lower()
    artist = track.get('artists', [{}])[0].get('name', '').lower()

    for rule in blocklist:
        if rule.startswith('id:') and rule[3:].strip() == track_id:
            return True, f"Matched ID rule: {rule}"
        elif rule.startswith('artist:') and rule[7:].strip() in artist:
            return True, f"Matched Artist rule: {rule}"
        elif rule.startswith('title:') and rule[6:].strip() in title:
            return True, f"Matched Title rule: {rule}"

    return False, ""
