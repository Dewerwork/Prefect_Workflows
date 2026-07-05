"""Tests for the --check doctor and Craigslist body enrichment."""

from __future__ import annotations

from marketplace_monitor.config import (
    AlertsConfig,
    Config,
    DedupeConfig,
    DeliveryConfig,
    LocationConfig,
    MarketplaceConfig,
    PrefilterConfig,
    ScoringConfig,
)
from marketplace_monitor.doctor import FAIL, OK, run_checks, worst_status
from marketplace_monitor.models import SearchSpec


def make_config(**overrides):
    base = dict(
        location=LocationConfig(zip_code="83605"),
        marketplaces=[
            MarketplaceConfig(name="ebay", enabled=True, searches=[SearchSpec(query="x")]),
            MarketplaceConfig(name="craigslist", enabled=True, searches=[SearchSpec(query="x")]),
        ],
        prefilter=PrefilterConfig(),
        scoring=ScoringConfig(),
        delivery=DeliveryConfig(method="console"),
        dedupe=DedupeConfig(),
        alerts=AlertsConfig(),
    )
    base.update(overrides)
    return Config(**base)


def test_doctor_flags_missing_ebay_and_anthropic_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    checks = run_checks(make_config())
    by_label = {c.label: c for c in checks}
    assert by_label["scoring"].status == FAIL
    assert by_label["marketplace:ebay"].status == FAIL
    # Craigslist needs no credentials.
    assert by_label["marketplace:craigslist"].status == OK
    assert worst_status(checks) == FAIL


def test_doctor_all_ok_when_keys_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    checks = run_checks(make_config())
    assert worst_status(checks) in (OK, "warn")
    assert {c.label: c.status for c in checks}["marketplace:ebay"] == OK


def test_doctor_flags_resend_missing_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    cfg = make_config(delivery=DeliveryConfig(method="resend", to=["a@b.com"]))
    checks = run_checks(cfg)
    assert {c.label: c.status for c in checks}["delivery"] == FAIL


def test_doctor_alerts_missing_channel_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    cfg = make_config(alerts=AlertsConfig(enabled=True, channel="discord", min_score=90))
    checks = run_checks(cfg)
    assert {c.label: c.status for c in checks}["alerts"] == FAIL


# --- Craigslist enrichment --------------------------------------------------

def test_craigslist_enrich_fills_missing_body(monkeypatch):
    from marketplace_monitor.adapters import craigslist
    from marketplace_monitor.models import Listing

    page = (
        '<html><section id="postingbody" class="show-contact">'
        "Lodge 5qt dutch oven, seasoned, comes with lid. Firm on price."
        "</section></html>"
    )

    class FakeResp:
        text = page

        def raise_for_status(self):
            pass

    class FakeSession:
        headers = {}

        def get(self, url, timeout=20):
            return FakeResp()

    monkeypatch.setattr(craigslist, "_UA", "test-agent")
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    adapter = craigslist.CraigslistAdapter(options={"site": "boise", "fetch_body": True})
    monkeypatch.setattr("requests.Session", lambda: FakeSession())

    listing = Listing(
        id="craigslist:1", source="craigslist", title="dutch oven", price=35.0,
        currency="USD", url="https://boise.craigslist.org/for/1.html", location="boise",
        distance_mi=None, posted_at=None, description=None, image_url=None, category=None, raw={},
    )
    adapter.enrich([listing])
    assert listing.description and "dutch oven" in listing.description.lower()


def test_craigslist_enrich_noop_when_disabled():
    from marketplace_monitor.adapters import craigslist
    from marketplace_monitor.models import Listing

    adapter = craigslist.CraigslistAdapter(options={"fetch_body": False})
    listing = Listing(
        id="craigslist:1", source="craigslist", title="x", price=None, currency="USD",
        url="http://x/1.html", location="boise", distance_mi=None, posted_at=None,
        description=None, image_url=None, category=None, raw={},
    )
    adapter.enrich([listing])  # must not touch the network
    assert listing.description is None


def test_extract_body_strips_tags():
    from marketplace_monitor.adapters.craigslist import _extract_body

    html = '<section id="postingbody">Great <b>cast iron</b> skillet<br>call me</section>'
    assert _extract_body(html) == "Great cast iron skillet call me"
