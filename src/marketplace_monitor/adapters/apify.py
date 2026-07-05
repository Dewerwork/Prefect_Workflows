"""Thin Apify actor runner, shared by the Facebook and OfferUp adapters.

Section 5.5 / 5.4: for the hostile marketplaces the recommended path is a
maintained Apify actor — you pay per result and they manage proxies, headless
browsers, and blocking. This helper runs an actor synchronously and returns its
dataset items. Requires ``APIFY_TOKEN``.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.apify.com/v2"


def run_apify_actor(actor_id: str, run_input: dict, timeout_secs: int = 300) -> list[dict]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        logger.info("APIFY_TOKEN not set; skipping Apify actor %s", actor_id)
        return []
    # Actor ids use '~' in the API path (user~actor-name).
    actor_path = actor_id.replace("/", "~")
    url = f"{_BASE}/acts/{actor_path}/run-sync-get-dataset-items"
    resp = requests.post(
        url,
        params={"token": token, "timeout": timeout_secs},
        json=run_input,
        timeout=timeout_secs + 30,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("items", []) if isinstance(data, dict) else []
