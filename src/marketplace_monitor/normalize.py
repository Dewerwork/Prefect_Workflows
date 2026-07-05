"""Normalize raw adapter output into the common ``Listing`` schema (FR-2)."""

from __future__ import annotations

from .models import Listing, RawListing


def to_listing(raw: RawListing) -> Listing:
    price = raw.price
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None

    title = (raw.title or "").strip()
    return Listing(
        id=Listing.make_id(raw.source, raw.source_id, raw.url),
        source=raw.source,
        title=title,
        price=price,
        currency=raw.currency or "USD",
        url=raw.url,
        location=(raw.location or None),
        distance_mi=raw.distance_mi,
        posted_at=raw.posted_at,
        description=(raw.description or None),
        image_url=(raw.image_url or None),
        category=(raw.category or None),
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
