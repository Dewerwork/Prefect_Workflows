"""Normalize raw adapter output into the common ``Listing`` schema (FR-2)."""

from __future__ import annotations

from .models import Listing, RawListing

# Common keys a scraper might use for a human-readable label inside a nested object.
_TEXT_KEYS = ("text", "name", "displayName", "city", "label", "title", "url", "uri")


def _as_text(value) -> str | None:
    """Coerce a possibly-nested scraper value into a plain string, or None.

    Scrapers sometimes return structured objects where we expect a string (e.g.
    a Facebook listing's ``location`` is a dict). Reduce it to a readable string
    so downstream rendering never sees a dict/list."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _as_text(item)
            if text:
                return text
        return None
    return str(value)


def to_listing(raw: RawListing) -> Listing:
    price = raw.price
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None

    return Listing(
        id=Listing.make_id(raw.source, raw.source_id, raw.url),
        source=raw.source,
        title=_as_text(raw.title) or "",
        price=price,
        currency=raw.currency or "USD",
        url=raw.url if isinstance(raw.url, str) else "",
        location=_as_text(raw.location),
        distance_mi=raw.distance_mi,
        posted_at=raw.posted_at,
        description=_as_text(raw.description),
        image_url=_as_text(raw.image_url),
        category=_as_text(raw.category),
        raw=raw.raw or {},
    )


def normalize_all(raws: list[RawListing]) -> list[Listing]:
    """Normalize and drop within-run duplicate ids (same item returned by two
    searches against the same source)."""
    seen: set[str] = set()
    listings: list[Listing] = []
    for raw in raws:
        listing = to_listing(raw)
        if not listing.url or not listing.title:
            continue
        if listing.id in seen:
            continue
        seen.add(listing.id)
        listings.append(listing)
    return listings
