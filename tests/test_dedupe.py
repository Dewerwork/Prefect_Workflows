"""Tests for cross-marketplace near-dup collapse and instant alerts."""

from __future__ import annotations

from marketplace_monitor.dedupe import DedupeConfig, collapse
from marketplace_monitor.models import Listing, ScoredListing
from marketplace_monitor.notify import AlertsConfig, send_alerts


def L(id, source, title, price=30.0, description=None, distance=None, image=None):
    return Listing(
        id=id, source=source, title=title, price=price, currency="USD",
        url=f"https://ex.com/{id}", location="Nampa, ID", distance_mi=distance,
        posted_at=None, description=description, image_url=image, category=None, raw={},
    )


def test_collapses_cross_posted_item():
    listings = [
        L("ebay:1", "ebay", "Lodge cast iron dutch oven 5qt", price=35),
        L("cl:9", "craigslist", "Lodge Cast Iron Dutch Oven 5 qt", price=35,
          description="great shape"),
    ]
    reps, dropped = collapse(listings, DedupeConfig())
    assert len(reps) == 1 and len(dropped) == 1
    # The richer entry (has a description) is the representative.
    assert reps[0].source == "craigslist"
    assert reps[0].raw["cross_posts"] == ["https://ex.com/ebay:1"]


def test_distinct_items_not_collapsed():
    listings = [
        L("ebay:1", "ebay", "Lodge cast iron skillet 10 inch"),
        L("cl:9", "craigslist", "Milwaukee cordless drill set", price=120),
    ]
    reps, dropped = collapse(listings, DedupeConfig())
    assert len(reps) == 2 and dropped == []


def test_same_source_never_merged():
    # Two similar items on the same source are distinct listings, not dupes.
    listings = [
        L("ebay:1", "ebay", "Lodge cast iron skillet"),
        L("ebay:2", "ebay", "Lodge cast iron skillet"),
    ]
    reps, dropped = collapse(listings, DedupeConfig())
    assert len(reps) == 2


def test_price_mismatch_blocks_collapse():
    listings = [
        L("ebay:1", "ebay", "Lodge cast iron dutch oven", price=35),
        L("cl:9", "craigslist", "Lodge cast iron dutch oven", price=150),
    ]
    reps, _ = collapse(listings, DedupeConfig())
    assert len(reps) == 2


def test_disabled_is_passthrough():
    listings = [
        L("ebay:1", "ebay", "Lodge cast iron dutch oven"),
        L("cl:9", "craigslist", "Lodge cast iron dutch oven"),
    ]
    reps, dropped = collapse(listings, DedupeConfig(enabled=False))
    assert len(reps) == 2 and dropped == []


# --- alerts -----------------------------------------------------------------

def test_alerts_disabled_sends_nothing():
    items = [ScoredListing(listing=L("a", "ebay", "x"), score=95, reason="")]
    assert send_alerts(AlertsConfig(enabled=False), items) == 0


def test_alerts_only_above_min_score(monkeypatch):
    sent = []
    import marketplace_monitor.notify as notify

    monkeypatch.setattr(notify, "_send_telegram", lambda text: sent.append(text))
    items = [
        ScoredListing(listing=L("a", "ebay", "great find"), score=95, reason="deal"),
        ScoredListing(listing=L("b", "ebay", "meh"), score=70, reason="ok"),
    ]
    n = send_alerts(AlertsConfig(enabled=True, channel="telegram", min_score=90), items)
    assert n == 1 and len(sent) == 1 and "great find" in sent[0]


def test_alerts_failure_is_swallowed(monkeypatch):
    import marketplace_monitor.notify as notify

    def boom(text):
        raise RuntimeError("network down")

    monkeypatch.setattr(notify, "_send_discord", boom)
    items = [ScoredListing(listing=L("a", "ebay", "x"), score=99, reason="")]
    # Best-effort: a failing channel does not raise.
    assert send_alerts(AlertsConfig(enabled=True, channel="discord", min_score=90), items) == 0
