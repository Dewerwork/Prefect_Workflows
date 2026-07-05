"""Deterministic pre-filter (section 6.2, FR-4).

This is the first and biggest cost/quality lever: kill 70-90% of noise for free
before any LLM call. Only survivors get scored. Everything here is cheap and
deterministic — price ceiling, distance, hard-exclude keywords/categories.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import PrefilterConfig
from .models import Listing

logger = logging.getLogger(__name__)


@dataclass
class PrefilterStats:
    total: int = 0
    kept: int = 0
    dropped_price: int = 0
    dropped_distance: int = 0
    dropped_keyword: int = 0
    dropped_category: int = 0

    @property
    def dropped(self) -> int:
        return self.total - self.kept


def _matches_keyword(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


def apply(listings: list[Listing], cfg: PrefilterConfig) -> tuple[list[Listing], PrefilterStats]:
    stats = PrefilterStats(total=len(listings))
    kept: list[Listing] = []

    for listing in listings:
        # Price ceiling. Listings with no price survive (bundles priced in the
        # body); the LLM can still judge them.
        if cfg.max_price is not None and listing.price is not None and listing.price > cfg.max_price:
            stats.dropped_price += 1
            continue

        # Distance. Only enforced when the adapter supplied a distance.
        if (
            cfg.max_distance_mi is not None
            and listing.distance_mi is not None
            and listing.distance_mi > cfg.max_distance_mi
        ):
            stats.dropped_distance += 1
            continue

        haystack = f"{listing.title} {listing.description or ''}".lower()
        if cfg.exclude_keywords and _matches_keyword(haystack, cfg.exclude_keywords):
            stats.dropped_keyword += 1
            continue

        if cfg.exclude_categories and listing.category and listing.category.lower() in cfg.exclude_categories:
            stats.dropped_category += 1
            continue

        kept.append(listing)

    stats.kept = len(kept)
    logger.info(
        "prefilter: %d in -> %d kept (price:-%d distance:-%d keyword:-%d category:-%d)",
        stats.total, stats.kept, stats.dropped_price, stats.dropped_distance,
        stats.dropped_keyword, stats.dropped_category,
    )
    return kept, stats
