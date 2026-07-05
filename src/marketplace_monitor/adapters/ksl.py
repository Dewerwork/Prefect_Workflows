"""KSL Classifieds adapter — HTTP + internal JSON (section 5.3, P1).

High regional value in Idaho/Utah. No official API, but the site's own frontend
calls an internal JSON endpoint that is cleaner than parsing HTML. This adapter
tries that endpoint first and parses the ``window.renderSearchSection`` /
embedded JSON payload out of the results page as a fallback.

Effort is medium: a realistic User-Agent and gentle rate limiting go a long way
(section 14). If KSL blocks plain HTTP, this adapter returns [] and the rest of
the run continues (FR-10) — escalate to a headless browser only if needed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

import requests

from ..models import RawListing, SearchSpec
from .base import BaseAdapter

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_SEARCH_HTML = "https://classifieds.ksl.com/search/"
# Embedded state the KSL frontend hydrates from. Different builds of the site
# expose this under different markers, so we try several.
_STATE_RE = re.compile(r"window\.renderSearchSectionInitialData\s*=\s*(\{.*?\});", re.DOTALL)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', re.DOTALL
)
_INITIAL_STATE_RE = re.compile(
    r"window\.__(?:INITIAL_STATE|PRELOADED_STATE|APP_STATE)__\s*=\s*(\{.*?\})\s*;?\s*</script>",
    re.DOTALL,
)
_LISTINGS_RE = re.compile(r'"(?:items|listings|results)"\s*:\s*(\[\{.*?\}\])', re.DOTALL)


class KslAdapter(BaseAdapter):
    name = "ksl"

    def __init__(self, *, location=None, options=None):
        super().__init__(location=location, options=options)
        self._delay = float(self.options.get("rate_limit_seconds", 2.0))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _UA, "Accept": "text/html"})

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:
        params = {"keyword": spec.query, "sort": "0"}
        if spec.max_price is not None:
            params["priceTo"] = int(spec.max_price)
        if spec.min_price is not None:
            params["priceFrom"] = int(spec.min_price)
        zip_code = getattr(self.location, "zip_code", None)
        radius = getattr(self.location, "radius_mi", None)
        if zip_code and radius:
            params["zip"] = zip_code
            params["miles"] = radius
        if spec.category:
            params["category"] = spec.category

        time.sleep(self._delay)  # gentle rate limiting
        resp = self._session.get(_SEARCH_HTML, params=params, timeout=30)
        resp.raise_for_status()
        html = resp.text
        logger.debug(
            "[ksl] GET %s -> status=%s len=%d markers: next_data=%s initial_state=%s "
            "renderSearch=%s listings=%s",
            resp.url, resp.status_code, len(html),
            "__NEXT_DATA__" in html,
            bool(_INITIAL_STATE_RE.search(html)),
            "renderSearchSection" in html,
            '"listings"' in html or '"items"' in html,
        )
        items = _extract_items(html)
        return [self._to_raw(item, spec) for item in items if item]

    def _to_raw(self, item: dict, spec: SearchSpec) -> RawListing | None:
        listing_id = str(item.get("id") or item.get("listingId") or "")
        title = item.get("title") or item.get("name") or ""
        if not title:
            return None
        url = item.get("url") or (
            f"https://classifieds.ksl.com/listing/{listing_id}" if listing_id else ""
        )
        if url and not url.startswith("http"):
            url = "https://classifieds.ksl.com" + url
        price = item.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        city = item.get("city") or item.get("displayLocation")
        state = item.get("state")
        location = ", ".join(p for p in (city, state) if p) or None
        image = item.get("photo") or item.get("image")
        if isinstance(image, list) and image:
            image = image[0]
        return RawListing(
            source=self.name,
            source_id=listing_id,
            title=title,
            url=url or _SEARCH_HTML,
            price=price,
            location=location,
            posted_at=_parse_epoch(item.get("createTime") or item.get("modifyTime")),
            description=item.get("description"),
            image_url=image if isinstance(image, str) else None,
            category=spec.category or item.get("category"),
            raw=item,
        )


def _find_listings(obj) -> list[dict] | None:
    """Recursively search a decoded JSON blob for the first list of listing-like
    dicts (objects that have a title/price/id). Handles Next.js/preloaded-state
    payloads where the listings are nested arbitrarily deep."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(
            k in obj[0] for k in ("title", "price", "listingId", "id")
        ) and any(k in obj[0] for k in ("title", "name")):
            return obj
        for item in obj:
            found = _find_listings(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for key in ("items", "listings", "results", "data"):
            val = obj.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
        for val in obj.values():
            found = _find_listings(val)
            if found:
                return found
    return None


def _extract_items(html_text: str) -> list[dict]:
    for regex in (_STATE_RE, _NEXT_DATA_RE, _INITIAL_STATE_RE):
        m = regex.search(html_text)
        if not m:
            continue
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        found = _find_listings(payload)
        if found:
            return found
    # Last resort: a bare "listings":[...] / "items":[...] array in the HTML.
    m = _LISTINGS_RE.search(html_text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    logger.info("[ksl] no embedded listing JSON found (site markup may have changed)")
    return []


def _parse_epoch(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts)
    except (TypeError, ValueError, OSError):
        return None
