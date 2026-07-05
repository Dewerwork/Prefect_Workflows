"""Core data types shared across the pipeline.

These mirror section 7 ("Data Model") of the design doc. Everything that flows
between adapters, the store, the pre-filter, the scorer, and the report speaks
in these types so no stage needs to know how a listing was originally obtained.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SearchSpec:
    """One search to run against a marketplace.

    A marketplace adapter is handed a list of these and returns raw listings for
    all of them. ``query`` is a natural search string; the numeric fields are
    optional deterministic hints an adapter may push down to the source's own
    filters (cheaper than fetching everything and filtering locally).
    """

    query: str
    category: str | None = None
    max_price: float | None = None
    min_price: float | None = None
    # Extra per-adapter knobs (e.g. a Craigslist subcategory code). Adapters
    # ignore keys they do not understand.
    extra: dict = field(default_factory=dict)


@dataclass
class RawListing:
    """A listing as an adapter first sees it, before normalization.

    Adapters do a best-effort mapping into these fields and stash the original
    payload in ``raw``. ``normalize.to_listing`` turns this into a ``Listing``.
    """

    source: str
    source_id: str
    title: str
    url: str
    price: float | None = None
    currency: str = "USD"
    location: str | None = None
    distance_mi: float | None = None
    posted_at: datetime | None = None
    description: str | None = None
    image_url: str | None = None
    category: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Listing:
    """Normalized listing — the common schema every downstream stage uses."""

    id: str
    source: str
    title: str
    price: float | None
    currency: str
    url: str
    location: str | None
    distance_mi: float | None
    posted_at: datetime | None
    description: str | None
    image_url: str | None
    category: str | None
    raw: dict = field(default_factory=dict)

    @staticmethod
    def make_id(source: str, source_id: str | None, url: str) -> str:
        """Stable dedupe key: ``source:source_id`` when we have an id, else a
        hash of the URL so re-runs collapse to the same key."""
        if source_id:
            return f"{source}:{source_id}"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return f"{source}:{digest}"


@dataclass
class ScoredListing:
    """A listing plus the LLM's judgment of it (section 7.2)."""

    listing: Listing
    score: int
    reason: str
    matched_interest: str | None = None
