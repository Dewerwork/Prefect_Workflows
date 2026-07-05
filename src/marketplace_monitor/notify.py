"""Instant alerts for standout items (section 9.2 nice-to-have, section 13).

Between daily digests, a same-day ping for a truly standout listing (score >= 90
by default) is worth having. This sends a short message to a Telegram bot or a
Discord webhook. It's best-effort: a failed ping is logged and never affects the
digest or the run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .models import ScoredListing

logger = logging.getLogger(__name__)


@dataclass
class AlertsConfig:
    enabled: bool = False
    channel: str = "telegram"   # telegram | discord
    min_score: int = 90


def _fmt(item: ScoredListing) -> str:
    l = item.listing
    price = f"${l.price:,.0f}" if l.price is not None else "price n/a"
    reason = f" — {item.reason}" if item.reason else ""
    return f"[{item.score}] {l.title} ({price}, {l.source}){reason}\n{l.url}"


def send_alerts(cfg: AlertsConfig, items: list[ScoredListing]) -> int:
    """Ping for every item at or above ``min_score``. Returns the count sent."""
    if not cfg.enabled:
        return 0
    standouts = [i for i in items if i.score >= cfg.min_score]
    if not standouts:
        return 0

    sender = {"telegram": _send_telegram, "discord": _send_discord}.get(cfg.channel)
    if sender is None:
        logger.warning("unknown alert channel: %s", cfg.channel)
        return 0

    sent = 0
    for item in standouts:
        try:
            sender(_fmt(item))
            sent += 1
        except Exception as exc:  # noqa: BLE001 - best-effort boundary
            logger.warning("alert failed for %s: %s", item.listing.id, exc)
    logger.info("sent %d instant alert(s) via %s", sent, cfg.channel)
    return sent


def _send_telegram(text: str) -> None:
    import requests

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=15,
    )
    resp.raise_for_status()


def _send_discord(text: str) -> None:
    import requests

    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL not set")
    resp = requests.post(url, json={"content": text}, timeout=15)
    resp.raise_for_status()
