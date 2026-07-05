"""Adapter parsing tests — network monkeypatched, real parse paths exercised."""

from __future__ import annotations

import types

from marketplace_monitor.config import LocationConfig


class FakeResponse:
    def __init__(self, json_data=None, text=""):
        self._json = json_data
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


LOC = LocationConfig(zip_code="83605", radius_mi=40, label="Test")


# --- eBay -------------------------------------------------------------------

def test_ebay_parses_item_summary(monkeypatch):
    from marketplace_monitor.adapters import ebay
    from marketplace_monitor.models import SearchSpec

    payload = {
        "itemSummaries": [
            {
                "itemId": "v1|123|0",
                "title": "Lodge Cast Iron Skillet",
                "itemWebUrl": "https://ebay.com/itm/123",
                "price": {"value": "29.99", "currency": "USD"},
                "itemLocation": {"city": "Nampa", "stateOrProvince": "ID"},
                "image": {"imageUrl": "https://img/1.jpg"},
                "itemCreationDate": "2026-07-01T12:00:00Z",
                "shortDescription": "seasoned",
            }
        ]
    }
    monkeypatch.setattr(ebay.EbayAdapter, "_get_token", lambda self: "tok")
    monkeypatch.setattr(ebay.requests, "get", lambda *a, **k: FakeResponse(json_data=payload))

    adapter = ebay.EbayAdapter(location=LOC)
    out = adapter.fetch([SearchSpec(query="cast iron", max_price=40)])
    assert len(out) == 1
    item = out[0]
    assert item.source == "ebay"
    assert item.price == 29.99
    assert item.location == "Nampa, ID"
    assert item.image_url == "https://img/1.jpg"
    assert item.posted_at is not None


def test_ebay_missing_token_returns_empty(monkeypatch):
    from marketplace_monitor.adapters import ebay
    from marketplace_monitor.models import SearchSpec

    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    adapter = ebay.EbayAdapter(location=LOC)
    # Never-raise-past-boundary: no creds -> [] rather than an exception.
    assert adapter.fetch([SearchSpec(query="x")]) == []


# --- Craigslist -------------------------------------------------------------

def test_craigslist_parses_rss(monkeypatch):
    from marketplace_monitor.adapters import craigslist
    from marketplace_monitor.models import SearchSpec

    fake_feed = types.SimpleNamespace(
        entries=[
            {
                "link": "https://boise.craigslist.org/for/12345.html",
                "title": "Cast iron dutch oven - $35 (Nampa)",
                "summary": "Lodge 5qt",
                "published": "2026-07-01T09:00:00+00:00",
                "enclosures": [{"type": "image/jpeg", "href": "https://img/x.jpg"}],
            }
        ]
    )
    fake_module = types.SimpleNamespace(parse=lambda url: fake_feed)
    monkeypatch.setitem(__import__("sys").modules, "feedparser", fake_module)

    adapter = craigslist.CraigslistAdapter(location=LOC, options={"site": "boise"})
    out = adapter.fetch([SearchSpec(query="cast iron", max_price=40)])
    assert len(out) == 1
    item = out[0]
    assert item.source_id == "12345"
    assert item.price == 35.0
    assert item.image_url == "https://img/x.jpg"


def test_craigslist_price_extraction():
    from marketplace_monitor.adapters.craigslist import _extract_price

    assert _extract_price("Skillet - $1,250 (Boise)") == 1250.0
    assert _extract_price("no price here") is None


# --- KSL --------------------------------------------------------------------

def test_ksl_extracts_embedded_json(monkeypatch):
    from marketplace_monitor.adapters import ksl
    from marketplace_monitor.models import SearchSpec

    html = (
        "<html><script>window.renderSearchSectionInitialData = "
        '{"items":[{"id":"9988","title":"Presto pressure canner",'
        '"price":45,"city":"Meridian","state":"ID",'
        '"url":"/listing/9988"}]};</script></html>'
    )
    monkeypatch.setattr(ksl.time, "sleep", lambda *a, **k: None)

    adapter = ksl.KslAdapter(location=LOC)
    monkeypatch.setattr(adapter._session, "get", lambda *a, **k: FakeResponse(text=html))
    out = adapter.fetch([SearchSpec(query="pressure canner")])
    assert len(out) == 1
    item = out[0]
    assert item.source_id == "9988"
    assert item.price == 45.0
    assert item.url == "https://classifieds.ksl.com/listing/9988"
    assert item.location == "Meridian, ID"


def test_ksl_no_json_returns_empty(monkeypatch):
    from marketplace_monitor.adapters import ksl
    from marketplace_monitor.models import SearchSpec

    monkeypatch.setattr(ksl.time, "sleep", lambda *a, **k: None)
    adapter = ksl.KslAdapter(location=LOC)
    monkeypatch.setattr(adapter._session, "get", lambda *a, **k: FakeResponse(text="<html></html>"))
    assert adapter.fetch([SearchSpec(query="x")]) == []


# --- OfferUp ----------------------------------------------------------------

def test_offerup_apify_mode(monkeypatch):
    from marketplace_monitor.adapters import offerup
    from marketplace_monitor.models import SearchSpec

    items = [{"id": "77", "title": "Cast iron griddle", "price": "25",
              "url": "https://offerup.com/item/detail/77", "locationName": "Boise"}]
    monkeypatch.setattr(offerup, "run_apify_actor", lambda actor, run_input: items)

    adapter = offerup.OfferUpAdapter(
        location=LOC, options={"mode": "apify", "apify_actor": "u/actor"}
    )
    out = adapter.fetch([SearchSpec(query="cast iron")])
    assert len(out) == 1 and out[0].price == 25.0 and out[0].location == "Boise"


def test_offerup_no_actor_returns_empty(monkeypatch):
    from marketplace_monitor.adapters import offerup
    from marketplace_monitor.models import SearchSpec

    adapter = offerup.OfferUpAdapter(location=LOC, options={"mode": "apify", "apify_actor": ""})
    assert adapter.fetch([SearchSpec(query="x")]) == []


# --- Facebook ---------------------------------------------------------------

def test_facebook_caps_searches(monkeypatch):
    from marketplace_monitor.adapters import facebook
    from marketplace_monitor.models import SearchSpec

    calls = {"n": 0}

    def fake_actor(actor, run_input):
        calls["n"] += 1
        return [{"id": "1", "title": "cast iron", "price": {"amount": 30},
                 "url": "https://fb.com/marketplace/item/1"}]

    monkeypatch.setattr(facebook, "run_apify_actor", fake_actor)
    adapter = facebook.FacebookAdapter(
        location=LOC, options={"apify_actor": "u/a", "max_searches": 2}
    )
    specs = [SearchSpec(query=f"q{i}") for i in range(5)]
    out = adapter.fetch(specs)
    # Hard cap: only 2 of the 5 searches actually hit the paid actor.
    assert calls["n"] == 2
    assert len(out) == 2
