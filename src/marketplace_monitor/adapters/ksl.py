"""KSL Classifieds adapter — v2 server-rendered flight data (section 5.3, P1).

High regional value in Idaho/Utah. KSL Classifieds is now a Next.js app at
``classifieds.ksl.com/v2/search/...``. There's no separate public JSON API, but
the search page **server-renders the listings into its Next.js flight data** —
the ``self.__next_f.push([1, "..."])`` script chunks embed a
``"results":[[ {...}, ... ]]`` array. So we fetch the page and pull the results
straight out of the embedded payload: no auth token, no headless browser.

Each embedded listing carries: ``id``, ``title``, ``price``, ``location``
(``{city, state, zip}``), ``primaryImage.url``, ``createdAt`` (epoch seconds),
``category``/``subCategory``. The public listing URL is ``/listing/{id}``.

If KSL changes the markup or blocks the request, this returns [] and the rest of
the run continues (FR-10).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote_plus

import requests

from ..models import RawListing, SearchSpec
from .base import BaseAdapter

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_BASE = "https://classifieds.ksl.com"
# Each Next.js flight chunk: self.__next_f.push([<n>, "<json-escaped string>"]).
_FLIGHT_RE = re.compile(
    r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)', re.DOTALL
)


class KslAdapter(BaseAdapter):
    name = "ksl"

    def __init__(self, *, location=None, options=None):
        super().__init__(location=location, options=options)
        self._delay = float(self.options.get("rate_limit_seconds", 2.0))
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _build_url(self, spec: SearchSpec) -> str:
        # Path-segment based URL: /v2/search/keyword/<q>/zip/<zip>/miles/<miles>
        parts = ["v2", "search", "keyword", quote_plus(spec.query)]
        zip_code = getattr(self.location, "zip_code", None)
        radius = getattr(self.location, "radius_mi", None)
        if zip_code:
            parts += ["zip", str(zip_code)]
            if radius:
                parts += ["miles", str(radius)]
        return f"{_BASE}/" + "/".join(parts)

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:
        url = self._build_url(spec)
        time.sleep(self._delay)  # gentle rate limiting
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        html = resp.text
        items, saw_flight = _extract_results(html)
        logger.debug("[ksl] GET %s -> status=%s len=%d results=%d flight=%s",
                     resp.url, resp.status_code, len(html), len(items), saw_flight)
        if not items:
            if saw_flight:
                logger.info("[ksl] 0 results for '%s' (no local matches)", spec.query)
            else:
                logger.info("[ksl] no flight data in %s (markup may have changed)", url)
        return [self._to_raw(item, spec) for item in items if item]

    def _to_raw(self, item: dict, spec: SearchSpec) -> RawListing | None:
        listing_id = str(item.get("id") or item.get("listingId") or "")
        title = item.get("title") or item.get("name") or ""
        if not title:
            return None
        price = item.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None

        loc = item.get("location") or {}
        if isinstance(loc, dict):
            location = ", ".join(p for p in (loc.get("city"), loc.get("state")) if p) or None
        else:
            location = str(loc) or None

        image = None
        primary = item.get("primaryImage")
        if isinstance(primary, dict):
            image = primary.get("url")

        return RawListing(
            source=self.name,
            source_id=listing_id,
            title=title,
            url=f"{_BASE}/listing/{listing_id}" if listing_id else _BASE,
            price=price,
            location=location,
            posted_at=_parse_epoch(item.get("createdAt")),
            description=item.get("description"),
            image_url=image,
            category=item.get("category") or spec.category,
            raw=item,
        )


def _extract_results(html_text: str) -> tuple[list[dict], bool]:
    """Pull the listings out of KSL's Next.js flight data.

    Returns ``(listings, saw_results_key)`` — the second flag distinguishes "the
    search genuinely had 0 matches" (flight data present, empty results) from
    "the page markup changed / was blocked" (no results key at all).

    The flight payload is split across many ``self.__next_f.push([1, "..."])``
    calls. We JSON-decode each string chunk (which un-escapes it), concatenate
    them into the full flight text, then find the ``"results"`` array and
    bracket-match its (possibly nested) JSON value.
    """
    parts = []
    for raw in _FLIGHT_RE.findall(html_text):
        try:
            parts.append(json.loads(raw))  # raw is a JSON string literal
        except json.JSONDecodeError:
            continue
    flight = "".join(parts)
    if not flight:
        return [], False

    key = '"results":'
    saw_key = key in flight
    idx = flight.find(key)
    while idx != -1:
        bracket = flight.find("[", idx + len(key))
        if bracket == -1:
            break
        block = _match_brackets(flight, bracket)
        if block:
            try:
                results = json.loads(block)
            except json.JSONDecodeError:
                results = None
            listings = _flatten(results)
            if listings:
                return listings, True
        idx = flight.find(key, idx + len(key))
    return [], saw_key


def _match_brackets(s: str, start: int) -> str | None:
    """Return the balanced ``[...]`` substring beginning at ``s[start] == '['``,
    ignoring brackets that appear inside JSON strings."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _flatten(results) -> list[dict]:
    """KSL's ``results`` is a list of lists of listing dicts (paged). Flatten to
    a single list of dicts."""
    out: list[dict] = []
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict):
                out.append(entry)
            elif isinstance(entry, list):
                out.extend(x for x in entry if isinstance(x, dict))
    return out


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
