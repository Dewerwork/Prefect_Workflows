"""Facebook Marketplace adapter — Apify actor (section 5.5, P2, own phase).

The hardest, highest-cost source. No official API and actively anti-scraping, so
the design's verdict is explicit: **use a paid actor, don't hand-roll it.** This
adapter runs a maintained Apify FB Marketplace actor behind the same interface.

Cost discipline (section 11 / 14): FB is the entire cost story, so this adapter
caps the number of searches and results it will pull. It stays fully toggleable
(``enabled: false`` in config) and, like every adapter, returns [] on failure so
a broken/rate-limited actor never aborts the run.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..models import RawListing, SearchSpec
from .apify import run_apify_actor
from .base import BaseAdapter

logger = logging.getLogger(__name__)


class FacebookAdapter(BaseAdapter):
    name = "facebook"

    @classmethod
    def required_env(cls, options=None):
        # FB always goes through a paid Apify actor.
        return ["APIFY_TOKEN"]

    def __init__(self, *, location=None, options=None):
        super().__init__(location=location, options=options)
        self.actor = self.options.get("apify_actor", "")
        self.max_items = int(self.options.get("max_items", 30))
        # Hard cap on how many of the configured searches FB will run, to bound
        # per-result spend regardless of how many searches are configured.
        self.max_searches = int(self.options.get("max_searches", 3))
        self._searches_run = 0

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:
        if self._searches_run >= self.max_searches:
            logger.info("[facebook] max_searches (%d) reached; skipping '%s'",
                        self.max_searches, spec.query)
            return []
        if not self.actor:
            logger.info("[facebook] no apify_actor configured; skipping")
            return []
        self._searches_run += 1

        run_input = {
            "query": spec.query,
            "city": getattr(self.location, "zip_code", None),
            "radius": getattr(self.location, "radius_mi", None),
            "maxItems": self.max_items,
        }
        if spec.max_price is not None:
            run_input["maxPrice"] = spec.max_price
        if spec.min_price is not None:
            run_input["minPrice"] = spec.min_price

        items = run_apify_actor(self.actor, run_input)
        return [self._to_raw(item, spec) for item in items if item]

    def _to_raw(self, item: dict, spec: SearchSpec) -> RawListing | None:
        listing_id = str(item.get("id") or item.get("listingId") or "")
        title = item.get("title") or item.get("marketplace_listing_title") or ""
        if not title:
            return None
        url = item.get("url") or item.get("listingUrl") or (
            f"https://www.facebook.com/marketplace/item/{listing_id}" if listing_id else ""
        )
        price = _parse_price(item.get("price"))
        image = item.get("image") or item.get("primaryPhoto") or item.get("photo")
        if isinstance(image, dict):
            image = image.get("uri") or image.get("url")
        return RawListing(
            source=self.name,
            source_id=listing_id,
            title=title,
            url=url,
            price=price,
            location=item.get("location") or item.get("city"),
            posted_at=_parse_iso(item.get("createdAt") or item.get("postedAt")),
            description=item.get("description"),
            image_url=image if isinstance(image, str) else None,
            category=spec.category,
            raw=item,
        )


def _parse_price(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value")
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, OSError):
        return None
