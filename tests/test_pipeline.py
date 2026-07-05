"""Unit tests for the pure pipeline stages (no network, no API key)."""

from __future__ import annotations

from datetime import datetime

import pytest

from marketplace_monitor.adapters.base import BaseAdapter
from marketplace_monitor.config import PrefilterConfig, ScoringConfig
from marketplace_monitor.models import Listing, RawListing, ScoredListing, SearchSpec
from marketplace_monitor.normalize import normalize_all, to_listing
from marketplace_monitor.prefilter import apply as prefilter_apply
from marketplace_monitor.report import RunSummary, render_html, render_text
from marketplace_monitor.score import Scorer, rank_and_cap
from marketplace_monitor.store import SeenStore


def make_listing(id="ebay:1", price=30.0, title="Lodge cast iron skillet",
                 distance=10.0, category="Home & Garden", description="great pan"):
    return Listing(
        id=id, source="ebay", title=title, price=price, currency="USD",
        url=f"https://example.com/{id}", location="Nampa, ID", distance_mi=distance,
        posted_at=datetime(2026, 7, 5), description=description,
        image_url=None, category=category, raw={},
    )


# --- models / normalize -----------------------------------------------------

def test_make_id_prefers_source_id():
    assert Listing.make_id("ebay", "123", "http://x") == "ebay:123"


def test_make_id_hashes_url_when_no_source_id():
    a = Listing.make_id("cl", None, "http://x/1.html")
    b = Listing.make_id("cl", None, "http://x/1.html")
    assert a == b and a.startswith("cl:")


def test_normalize_dedupes_within_run():
    raws = [
        RawListing(source="ebay", source_id="1", title="A", url="http://x/1"),
        RawListing(source="ebay", source_id="1", title="A", url="http://x/1"),
        RawListing(source="ebay", source_id="2", title="B", url="http://x/2"),
    ]
    listings = normalize_all(raws)
    assert len(listings) == 2


def test_normalize_drops_untitled_or_urlless():
    raws = [
        RawListing(source="ebay", source_id="1", title="", url="http://x/1"),
        RawListing(source="ebay", source_id="2", title="B", url=""),
    ]
    assert normalize_all(raws) == []


def test_to_listing_coerces_bad_price():
    raw = RawListing(source="ebay", source_id="1", title="A", url="http://x", price="not-a-number")
    assert to_listing(raw).price is None


# --- prefilter --------------------------------------------------------------

def test_prefilter_price_ceiling():
    cfg = PrefilterConfig(max_price=40)
    kept, stats = prefilter_apply([make_listing(price=30), make_listing(id="ebay:2", price=99)], cfg)
    assert len(kept) == 1 and stats.dropped_price == 1


def test_prefilter_keeps_priceless_listings():
    cfg = PrefilterConfig(max_price=40)
    kept, _ = prefilter_apply([make_listing(price=None)], cfg)
    assert len(kept) == 1


def test_prefilter_distance_and_keywords():
    cfg = PrefilterConfig(max_distance_mi=40, exclude_keywords=["broken"])
    listings = [
        make_listing(id="a", distance=100),
        make_listing(id="b", title="broken skillet"),
        make_listing(id="c"),
    ]
    kept, stats = prefilter_apply(listings, cfg)
    assert {l.id for l in kept} == {"c"}
    assert stats.dropped_distance == 1 and stats.dropped_keyword == 1


# --- seen-store -------------------------------------------------------------

def test_seen_store_dedupe_and_idempotency(tmp_path):
    store = SeenStore(str(tmp_path / "seen.db"))
    listings = [make_listing(id="ebay:1"), make_listing(id="ebay:2")]
    assert len(store.filter_new(listings)) == 2

    scored = [ScoredListing(listing=listings[0], score=90, reason="x")]
    store.record_all(scored, reported_ids={"ebay:1"})

    # Second run: the recorded one is no longer new.
    new = store.filter_new(listings)
    assert {l.id for l in new} == {"ebay:2"}
    store.close()


# --- scorer (fake client) ---------------------------------------------------

class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text):
        self.content = [FakeBlock(text)]


class FakeClient:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls += 1
            # Echo back a score for every listing id present in the prompt.
            import json
            user = kwargs["messages"][0]["content"]
            results = []
            for lid, score in self.outer.mapping.items():
                if lid in user:
                    results.append({"id": lid, "score": score, "reason": "match", "matched_interest": "cast iron"})
            return FakeResponse(json.dumps({"results": results}))

    @property
    def messages(self):
        return self._Messages(self)


def test_scorer_parses_and_matches(tmp_path):
    prefs = tmp_path / "prefs.md"
    prefs.write_text("# prefs\n- cast iron")
    cfg = ScoringConfig(preferences_path=str(prefs), batch_size=2)
    listings = [make_listing(id="ebay:1"), make_listing(id="ebay:2"), make_listing(id="ebay:3")]
    client = FakeClient({"ebay:1": 90, "ebay:2": 40, "ebay:3": 70})
    scorer = Scorer(cfg, client=client)
    scored = scorer.score(listings)
    assert {s.listing.id: s.score for s in scored} == {"ebay:1": 90, "ebay:2": 40, "ebay:3": 70}
    assert client.calls == 2  # 3 listings, batch_size 2 -> 2 requests


def test_scorer_system_prompt_is_cached(tmp_path):
    from marketplace_monitor.score import _system_blocks
    blocks = _system_blocks("prefs")
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_rank_and_cap():
    scored = [
        ScoredListing(listing=make_listing(id="a"), score=90, reason=""),
        ScoredListing(listing=make_listing(id="b"), score=50, reason=""),
        ScoredListing(listing=make_listing(id="c"), score=70, reason=""),
    ]
    out = rank_and_cap(scored, threshold=60, max_results=10)
    assert [s.listing.id for s in out] == ["a", "c"]  # b dropped, sorted desc


# --- report -----------------------------------------------------------------

def test_render_html_and_text():
    summary = RunSummary(total_fetched=100, new_after_dedupe=20, scored=10, reported=1)
    summary.fetched_by_source = {"ebay": 60, "craigslist": 40}
    items = [ScoredListing(listing=make_listing(), score=85, reason="great deal")]
    html = render_html(items, summary)
    assert "Lodge cast iron skillet" in html and "85" in html and "great deal" in html
    text = render_text(items, summary)
    assert "[85]" in text and "https://example.com/ebay:1" in text


def test_render_html_empty():
    summary = RunSummary()
    assert "Nothing cleared the threshold" in render_html([], summary)


# --- adapter isolation ------------------------------------------------------

class ExplodingAdapter(BaseAdapter):
    name = "boom"

    def _fetch(self, spec):
        raise RuntimeError("kaboom")


def test_adapter_never_raises_past_boundary():
    adapter = ExplodingAdapter()
    # One bad search does not raise; returns [].
    assert adapter.fetch([SearchSpec(query="x")]) == []


class PartialAdapter(BaseAdapter):
    name = "partial"

    def _fetch(self, spec):
        if spec.query == "bad":
            raise RuntimeError("nope")
        return [RawListing(source="partial", source_id="1", title="ok", url="http://x/1")]


def test_adapter_one_bad_search_keeps_the_others():
    adapter = PartialAdapter()
    out = adapter.fetch([SearchSpec(query="bad"), SearchSpec(query="good")])
    assert len(out) == 1


# --- full orchestrator integration -----------------------------------------

def _write_integration_config(tmp_path):
    prefs = tmp_path / "prefs.md"
    prefs.write_text("# prefs\n- cast iron")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "location: {label: T, zip_code: '83605', radius_mi: 40}\n"
        "prefilter: {max_price: 300}\n"
        "dedupe: {enabled: true}\n"
        "alerts: {enabled: false}\n"
        f"run_log_path: '{tmp_path / 'run.json'}'\n"
        f"scoring: {{model: m, threshold: 60, preferences_path: '{prefs}'}}\n"
        "delivery: {method: console, send_when_empty: true}\n"
        "marketplaces:\n  - {name: fake, enabled: true, searches: ['cast iron']}\n"
    )
    return str(cfg)


class _FakeAdapter(BaseAdapter):
    name = "fake"

    def _fetch(self, spec):
        # Same Dutch oven cross-posted on ebay + craigslist, plus a low-value item.
        return [
            RawListing(source="ebay", source_id="1", title="Lodge cast iron dutch oven",
                       url="http://x/e1", price=35),
            RawListing(source="craigslist", source_id="2", title="Lodge cast iron dutch oven",
                       url="http://x/c2", price=35, description="nice"),
            RawListing(source="ebay", source_id="3", title="rusty bolt",
                       url="http://x/e3", price=1),
        ]


class _FakeScorer:
    def __init__(self, cfg, client=None):
        self.cfg = cfg

    def score(self, listings):
        # Dutch oven scores high; anything else low.
        out = []
        for l in listings:
            score = 90 if "dutch oven" in l.title.lower() else 20
            out.append(ScoredListing(listing=l, score=score, reason="r", matched_interest="cast iron"))
        return out


def test_full_run_collapses_dupes_and_is_idempotent(tmp_path, monkeypatch, capsys):
    import marketplace_monitor.run as run_mod

    config_path = _write_integration_config(tmp_path)
    monkeypatch.setenv("STORE_URL", str(tmp_path / "seen.db"))
    monkeypatch.setattr(run_mod, "build_adapter",
                        lambda name, location=None, options=None: _FakeAdapter())
    monkeypatch.setattr(run_mod, "Scorer", _FakeScorer)

    summary = run_mod.run(config_path)
    # 3 fetched -> 2 after cross-post collapse -> dutch oven reported once.
    assert summary.total_fetched == 3
    assert summary.new_after_dedupe == 3
    assert summary.near_dups_collapsed == 1
    assert summary.reported == 1
    out = capsys.readouterr().out
    assert "Lodge cast iron dutch oven" in out

    # Idempotency: a second run sees everything as already-seen and reports nothing.
    summary2 = run_mod.run(config_path)
    assert summary2.new_after_dedupe == 0
    assert summary2.reported == 0


# --- profiles ---------------------------------------------------------------

def _cfg_with_profiles(tmp_path):
    from marketplace_monitor.config import load_config

    prefs = tmp_path / "prefs.md"
    prefs.write_text("# prefs")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "location: {label: T, zip_code: '83605', radius_mi: 40}\n"
        "prefilter: {max_price: 300}\n"
        f"scoring: {{model: m, threshold: 60, preferences_path: '{prefs}'}}\n"
        "delivery: {method: console}\n"
        "profiles:\n"
        "  hot: {categories: ['Miniatures'], threshold: 55, subject_prefix: 'Hot deals'}\n"
        "  ebay_only: {marketplaces: ['ebay']}\n"
        "marketplaces:\n"
        "  - name: ebay\n    enabled: true\n    searches:\n"
        "      - {query: minis, category: Miniatures}\n"
        "      - {query: skillet, category: Home & Garden}\n"
        "  - name: craigslist\n    enabled: true\n    searches:\n"
        "      - {query: minis, category: Miniatures}\n"
    )
    return load_config(cfg_path)


def test_profile_filters_by_category(tmp_path):
    from marketplace_monitor.run import select_marketplaces

    cfg = _cfg_with_profiles(tmp_path)
    selected = select_marketplaces(cfg, cfg.get_profile("hot"), None)
    # Both marketplaces have a Miniatures search; the eBay Home & Garden one is dropped.
    assert {m.name for m in selected} == {"ebay", "craigslist"}
    ebay = next(m for m in selected if m.name == "ebay")
    assert [s.query for s in ebay.searches] == ["minis"]


def test_profile_filters_by_marketplace(tmp_path):
    from marketplace_monitor.run import select_marketplaces

    cfg = _cfg_with_profiles(tmp_path)
    selected = select_marketplaces(cfg, cfg.get_profile("ebay_only"), None)
    assert {m.name for m in selected} == {"ebay"}
    # No category filter -> both eBay searches kept.
    assert len(selected[0].searches) == 2


def test_unknown_profile_raises(tmp_path):
    cfg = _cfg_with_profiles(tmp_path)
    with pytest.raises(ValueError):
        cfg.get_profile("nope")


def test_profile_threshold_override_applied(tmp_path, monkeypatch, capsys):
    import marketplace_monitor.run as run_mod

    cfg = _cfg_with_profiles(tmp_path)
    # Point run() at the same config file the fixture wrote.
    config_path = str(tmp_path / "config.yaml")
    monkeypatch.setenv("STORE_URL", str(tmp_path / "seen.db"))

    class _Adapter(BaseAdapter):
        name = "ebay"

        def _fetch(self, spec):
            return [RawListing(source="ebay", source_id="1", title="mini lot",
                               url="http://x/1", price=20)]

    class _Scorer57:
        def __init__(self, cfg, client=None):
            pass

        def score(self, listings):
            return [ScoredListing(listing=l, score=57, reason="r") for l in listings]

    monkeypatch.setattr(run_mod, "build_adapter",
                        lambda name, location=None, options=None: _Adapter())
    monkeypatch.setattr(run_mod, "Scorer", _Scorer57)

    # Default threshold 60 -> a score of 57 is dropped.
    s_default = run_mod.run(config_path, dry_run=True)
    assert s_default.reported == 0
    # Hot profile lowers threshold to 55 -> now it clears, and the console
    # delivery (non-dry) shows the profile's subject prefix override.
    s_hot = run_mod.run(config_path, profile="hot")
    assert s_hot.reported == 1
    assert "SUBJECT: Hot deals" in capsys.readouterr().out
