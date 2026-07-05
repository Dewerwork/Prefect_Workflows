"""Cross-marketplace near-duplicate detection (section 14).

Exact within-source dupes are handled by ``Listing.id`` in ``normalize``. But
the *same physical item* is often cross-posted to several marketplaces (a seller
lists a Dutch oven on both Facebook and Craigslist), each with a different id and
URL. Without this step you'd score and report it two or three times.

This collapses listings that look like the same item — similar title tokens plus
a compatible price — into one representative, preferring the entry with the most
information (a real description, a known distance). It's deliberately simple and
deterministic; the design lists richer title+price+image similarity as a later
enhancement, but token-Jaccard + price tolerance catches the common case cheaply
and with no external dependency.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .models import Listing

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "new", "used", "obo", "sale",
    "price", "great", "condition", "excellent", "like", "set", "of", "in", "to",
}


@dataclass
class DedupeConfig:
    enabled: bool = True
    title_similarity: float = 0.6   # Jaccard threshold on significant tokens
    price_tolerance: float = 0.15   # fraction; absolute floor of $5 applied too


def _tokens(title: str) -> frozenset[str]:
    return frozenset(
        t for t in _TOKEN_RE.findall(title.lower()) if t not in _STOPWORDS and len(t) > 1
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _prices_compatible(p1: float | None, p2: float | None, tol: float) -> bool:
    # Both unpriced -> can't distinguish on price; allow the title to decide.
    if p1 is None or p2 is None:
        return True
    hi = max(p1, p2)
    allowed = max(5.0, hi * tol)
    return abs(p1 - p2) <= allowed


def _rank(listing: Listing) -> tuple:
    """Sort key for choosing a cluster representative — richer entries win."""
    return (
        1 if listing.description else 0,
        1 if listing.distance_mi is not None else 0,
        1 if listing.image_url is not None else 0,
        -(listing.distance_mi if listing.distance_mi is not None else 1e9),
    )


def collapse(listings: list[Listing], cfg: DedupeConfig) -> tuple[list[Listing], list[Listing]]:
    """Return ``(representatives, dropped)``.

    ``dropped`` are the listings merged away — the caller records them in the
    seen-store too, so a collapsed cross-post doesn't resurface tomorrow.
    """
    if not cfg.enabled or len(listings) < 2:
        return listings, []

    token_cache = {l.id: _tokens(l.title) for l in listings}
    clusters: list[list[Listing]] = []

    for listing in listings:
        placed = False
        toks = token_cache[listing.id]
        for cluster in clusters:
            rep = cluster[0]
            if listing.source == rep.source:
                # Same source already deduped by id; don't merge distinct items.
                continue
            if any(m.source == listing.source for m in cluster):
                continue
            if _jaccard(toks, token_cache[rep.id]) >= cfg.title_similarity and _prices_compatible(
                listing.price, rep.price, cfg.price_tolerance
            ):
                cluster.append(listing)
                placed = True
                break
        if not placed:
            clusters.append([listing])

    representatives: list[Listing] = []
    dropped: list[Listing] = []
    for cluster in clusters:
        if len(cluster) == 1:
            representatives.append(cluster[0])
            continue
        ordered = sorted(cluster, key=_rank, reverse=True)
        rep = ordered[0]
        others = ordered[1:]
        # Note the cross-posts on the representative so the report can show them.
        rep.raw = {**rep.raw, "cross_posts": [o.url for o in others]}
        representatives.append(rep)
        dropped.extend(others)

    if dropped:
        logger.info("near-dup: %d listings -> %d after collapsing %d cross-posts",
                    len(listings), len(representatives), len(dropped))
    return representatives, dropped
