"""OfferUp adapter — internal API / Apify actor (section 5.4, P2).

OfferUp has no public API but a private GraphQL/JSON API backs the app and site.
Reverse-engineering it is medium-high effort and the shape shifts, so this
adapter supports two backends, selected by config:

  * ``mode: apify`` (default when a token is present) — run a maintained Apify
    OfferUp actor; they handle the anti-abuse surface.
  * ``mode: internal`` — call OfferUp's internal search endpoint directly.

Either way, on failure the adapter returns [] and the run continues (FR-10).
This is "second wave" coverage — nice to have, not worth blocking v1 on.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

from ..models import RawListing, SearchSpec
from .apify import run_apify_actor
from .base import BaseAdapter

logger = logging.getLogger(__name__)

_INTERNAL_URL = "https://offerup.com/api/graphql"


class OfferUpAdapter(BaseAdapter):
    name = "offerup"

    def __init__(self, *, location=None, options=None):
        super().__init__(location=location, options=options)
        self.mode = self.options.get("mode")
        if not self.mode:
            self.mode = "apify" if os.environ.get("APIFY_TOKEN") else "internal"
        self.actor = self.options.get("apify_actor", "")
        self.max_items = int(self.options.get("max_items", 40))

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:
        if self.mode == "apify":
            return self._fetch_apify(spec)
        return self._fetch_internal(spec)

    def _fetch_apify(self, spec: SearchSpec) -> list[RawListing]:
        if not self.actor:
            logger.info("[offerup] no apify_actor configured; skipping")
            return []
        run_input = {
            "query": spec.query,
            "zipCode": getattr(self.location, "zip_code", None),
            "radius": getattr(self.location, "radius_mi", None),
            "maxItems": self.max_items,
        }
        if spec.max_price is not None:
            run_input["priceMax"] = spec.max_price
        items = run_apify_actor(self.actor, run_input)
        return [self._to_raw(item, spec) for item in items if item]

    def _fetch_internal(self, spec: SearchSpec) -> list[RawListing]:
        zip_code = getattr(self.location, "zip_code", None)
        params = {"q": spec.query}
        if zip_code:
            params["zip"] = zip_code
        headers = {"User-Agent": "OfferUp/1.0", "Accept": "application/json"}
        resp = requests.get(_INTERNAL_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = _dig(data, ("data", "searchResults", "items")) or []
        return [self._to_raw(item, spec) for item in items if item]

    def _to_raw(self, item: dict, spec: SearchSpec) -> RawListing | None:
        listing_id = str(item.get("id") or item.get("listingId") or "")
        title = item.get("title") or item.get("name") or ""
        if not title:
            return None
        url = item.get("url") or item.get("listingUrl") or (
            f"https://offerup.com/item/detail/{listing_id}" if listing_id else ""
        )
        price = _parse_price(item.get("price"))
        image = item.get("image") or item.get("photoUrl")
        if isinstance(image, dict):
            image = image.get("url")
        return RawListing(
            source=self.name,
            source_id=listing_id,
            title=title,
            url=url,
            price=price,
            location=item.get("locationName") or item.get("location"),
            distance_mi=_parse_float(item.get("distance")),
            posted_at=_parse_iso(item.get("postedAt") or item.get("createdAt")),
            description=item.get("description"),
            image_url=image if isinstance(image, str) else None,
            category=spec.category,
            raw=item,
        )


def _dig(data: dict, path: tuple[str, ...]):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _parse_price(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount") or value.get("value")
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "")
    return _parse_float(value)


def _parse_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
