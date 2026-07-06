"""Adapter parsing tests — network monkeypatched, real parse paths exercised."""

from __future__ import annotations

import types

from marketplace_monitor.config import LocationConfig


class FakeResponse:
    def __init__(self, json_data=None, text="", url="https://example.test/",
                 status_code=200, content=b""):
        self._json = json_data
        self.text = text
        self.url = url
        self.status_code = status_code
        self.content = content

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
    fake_module = types.SimpleNamespace(parse=lambda src, **kw: fake_feed)
    monkeypatch.setitem(__import__("sys").modules, "feedparser", fake_module)

    class FakeSession:
        def get(self, *a, **k):
            return FakeResponse(status_code=200, content=b"<rss/>")

    adapter = craigslist.CraigslistAdapter(location=LOC, options={"site": "boise"})
    adapter._session = FakeSession()  # skip real session creation + warm-up
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


def test_craigslist_playwright_path(monkeypatch):
    import sys

    from marketplace_monitor.adapters import craigslist
    from marketplace_monitor.models import SearchSpec

    # Fake feedparser: return one entry.
    fake_feed = types.SimpleNamespace(entries=[{
        "link": "https://boise.craigslist.org/tls/98765.html",
        "title": "Cast iron dutch oven - $35 (Nampa)",
        "summary": "Lodge 5qt",
        "published": "2026-07-05T09:00:00+00:00",
        "enclosures": [],
    }])
    monkeypatch.setitem(sys.modules, "feedparser",
                        types.SimpleNamespace(parse=lambda src, **kw: fake_feed))

    # Fake Playwright chain: page.goto -> response with .status / .body().
    class FakeResp:
        status = 200

        def body(self):
            return b"<rss/>"

    class FakePage:
        def goto(self, url, **kw):
            return FakeResp()

        def wait_for_timeout(self, ms):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return types.SimpleNamespace(new_page=lambda: FakePage())

        def close(self):
            pass

    class FakePW:
        chromium = types.SimpleNamespace(launch=lambda **kw: FakeBrowser())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_sync_api = types.SimpleNamespace(sync_playwright=lambda: FakePW())
    monkeypatch.setitem(sys.modules, "playwright", types.SimpleNamespace(sync_api=fake_sync_api))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    adapter = craigslist.CraigslistAdapter(
        location=LOC, options={"site": "boise", "use_playwright": True}
    )
    out = adapter.fetch([SearchSpec(query="cast iron", max_price=40)])
    assert len(out) == 1
    assert out[0].source_id == "98765"
    assert out[0].price == 35.0


def test_craigslist_playwright_missing_dep_returns_empty(monkeypatch):
    import builtins

    from marketplace_monitor.adapters import craigslist
    from marketplace_monitor.models import SearchSpec

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = craigslist.CraigslistAdapter(location=LOC, options={"use_playwright": True})
    # Missing Playwright -> graceful [] (never aborts the run).
    assert adapter.fetch([SearchSpec(query="x")]) == []


# --- KSL --------------------------------------------------------------------

def _ksl_flight_html(listings):
    """Build a KSL v2 page the way Next.js renders it: the results embedded in a
    self.__next_f.push([1, "<json-escaped flight string>"]) script."""
    import json

    inner = {"initialState": {"results": [listings], "pageInfo": [{"total": len(listings)}]}}
    flight_str = '2a:["$","$L2b",null,' + json.dumps(inner) + "]"
    push = "self.__next_f.push([1, " + json.dumps(flight_str) + "])"
    return f"<html><body><script>{push}</script></body></html>"


def test_ksl_extracts_flight_results(monkeypatch):
    from marketplace_monitor.adapters import ksl
    from marketplace_monitor.models import SearchSpec

    listing = {
        "id": 81585559, "title": "Small folding pedestal table - Butcher block top",
        "price": 45, "location": {"city": "Boise", "state": "ID", "zip": "83713"},
        "primaryImage": {"url": "https://image.ksldigital.com/x.jpg"},
        "createdAt": 1783187422, "category": "Furniture",
    }
    html = _ksl_flight_html([listing])
    monkeypatch.setattr(ksl.time, "sleep", lambda *a, **k: None)

    adapter = ksl.KslAdapter(location=LOC)
    monkeypatch.setattr(adapter._session, "get",
                        lambda *a, **k: FakeResponse(text=html, url="https://classifieds.ksl.com/v2/search"))
    out = adapter.fetch([SearchSpec(query="table")])
    assert len(out) == 1
    item = out[0]
    assert item.source_id == "81585559"
    assert item.price == 45.0
    assert item.url == "https://classifieds.ksl.com/listing/81585559"
    assert item.location == "Boise, ID"
    assert item.image_url == "https://image.ksldigital.com/x.jpg"
    assert item.category == "Furniture"
    assert item.posted_at is not None


def test_ksl_builds_v2_url(monkeypatch):
    from marketplace_monitor.adapters import ksl
    from marketplace_monitor.models import SearchSpec

    adapter = ksl.KslAdapter(location=LOC)
    url = adapter._build_url(SearchSpec(query="cast iron"))
    assert url == "https://classifieds.ksl.com/v2/search/keyword/cast+iron/zip/83605/miles/40"


def test_ksl_no_results_returns_empty(monkeypatch):
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

    captured = {}
    items = [{"id": "77", "title": "Cast iron griddle", "price": "25",
              "url": "https://offerup.com/item/detail/77", "locationName": "Boise"}]

    def fake_actor(actor, run_input):
        captured["input"] = run_input
        return items

    monkeypatch.setattr(offerup, "run_apify_actor", fake_actor)

    adapter = offerup.OfferUpAdapter(
        location=LOC, options={"mode": "apify", "apify_actor": "u/actor"}
    )
    out = adapter.fetch([SearchSpec(query="cast iron")])
    assert len(out) == 1 and out[0].price == 25.0 and out[0].location == "Boise"
    # Input must match the actor's real schema (location / radiusMiles keys).
    assert captured["input"]["location"] == "83605"
    assert captured["input"]["radiusMiles"] == 40
    assert captured["input"]["query"] == "cast iron"


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

    inputs = []

    def fake_actor(actor, run_input):
        calls["n"] += 1
        inputs.append(run_input)
        return [{"id": "1", "title": "cast iron", "price": {"amount": 30},
                 "url": "https://fb.com/marketplace/item/1"}]

    monkeypatch.setattr(facebook, "run_apify_actor", fake_actor)
    adapter = facebook.FacebookAdapter(
        location=LOC,
        options={"apify_actor": "u/a", "max_searches": 2, "city_slug": "boise"},
    )
    specs = [SearchSpec(query="cast iron", max_price=40)] + [SearchSpec(query=f"q{i}") for i in range(4)]
    out = adapter.fetch(specs)
    # Hard cap: only 2 of the 5 searches actually hit the paid actor.
    assert calls["n"] == 2
    assert len(out) == 2
    # Input must match the actor's real schema (startUrls / resultsLimit).
    url = inputs[0]["startUrls"][0]["url"]
    assert "marketplace/boise/search" in url and "query=cast+iron" in url and "maxPrice=40" in url
    assert inputs[0]["resultsLimit"] == 30
