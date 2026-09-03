"""Discord/HTTP webhook notifications."""

import logging
import requests


def send_webhook(message: str, cfg) -> None:
    """Sends a notification to a Discord webhook if configured."""
    webhook_url = cfg['Webhooks'].get('discord_url', '').strip()
    if not webhook_url:
        return

    try:
        payload = {"content": message}
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"Failed to send webhook: {e}")
