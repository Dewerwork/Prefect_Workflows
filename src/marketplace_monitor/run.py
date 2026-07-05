"""Orchestrator — one daily run (section 6.1).

Pipeline: fetch (per adapter, isolated) -> normalize -> dedupe (seen-store)
-> collapse cross-marketplace near-dups -> deterministic pre-filter -> LLM scorer
-> rank + threshold + cap -> render + deliver -> instant alerts -> update store.

A broken adapter degrades gracefully (partial digest + error note); it never
aborts the run (FR-10 / NFR reliability). The whole thing is idempotent: a
second run on the same day re-reports nothing (FR-3), because every listing we
evaluate is written to the seen-store.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from pathlib import Path

from .adapters import available, build_adapter
from .config import Config, ProfileConfig, load_config
from .dedupe import collapse
from .deliver import deliver
from .normalize import normalize_all
from .notify import send_alerts
from .prefilter import apply as prefilter_apply
from .report import RunSummary, render_html, render_text
from .score import Scorer, rank_and_cap
from .store import SeenStore

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Best-effort: load a local .env so keys work without exporting them.
    No-op if python-dotenv isn't installed or there's no .env file."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def select_marketplaces(cfg: Config, profile: ProfileConfig | None, only_source: str | None):
    """Narrow the run to a profile's marketplaces + categories and/or a single
    source. Returns filtered ``MarketplaceConfig`` copies (searches trimmed to
    the selected categories); marketplaces left with no searches are dropped.
    """
    marketplaces = cfg.enabled_marketplaces()

    if profile and profile.marketplaces:
        allow = set(profile.marketplaces)
        marketplaces = [m for m in marketplaces if m.name in allow]
    if only_source:
        marketplaces = [m for m in marketplaces if m.name == only_source]

    if profile and profile.categories:
        cats = {c.lower() for c in profile.categories}
        trimmed = []
        for m in marketplaces:
            searches = [s for s in m.searches if s.category and s.category.lower() in cats]
            if searches:
                trimmed.append(dataclasses.replace(m, searches=searches))
        marketplaces = trimmed

    return marketplaces


def run(
    config_path: str | None = None,
    *,
    dry_run: bool = False,
    only_source: str | None = None,
    profile: str | None = None,
) -> RunSummary:
    _load_dotenv()
    cfg = load_config(config_path)
    logging.getLogger("marketplace_monitor").setLevel(logging.INFO)
    summary = RunSummary()

    profile_cfg = cfg.get_profile(profile) if profile else None
    threshold = (profile_cfg.threshold if profile_cfg and profile_cfg.threshold is not None
                 else cfg.scoring.threshold)
    max_results = (profile_cfg.max_results if profile_cfg and profile_cfg.max_results is not None
                   else cfg.scoring.max_results)
    subject_prefix = (profile_cfg.subject_prefix if profile_cfg and profile_cfg.subject_prefix
                      else cfg.delivery.subject_prefix)

    marketplaces = select_marketplaces(cfg, profile_cfg, only_source)
    if not marketplaces:
        logger.warning("no marketplaces selected (profile=%s, source=%s)", profile, only_source)
    if profile_cfg:
        logger.info("profile '%s': %d marketplaces, threshold %d",
                    profile, len(marketplaces), threshold)

    # 1. Fetch from every enabled marketplace (isolated per adapter).
    raw_listings = []
    adapters_by_name = {}
    for mc in marketplaces:
        try:
            adapter = build_adapter(mc.name, location=cfg.location, options=mc.options)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not build adapter '%s': %s", mc.name, exc)
            summary.adapter_errors.append(f"{mc.name}: {exc}")
            continue
        adapters_by_name[mc.name] = adapter
        found = adapter.fetch(mc.searches)
        summary.fetched_by_source[mc.name] = len(found)
        raw_listings.extend(found)

    summary.total_fetched = len(raw_listings)

    # 2. Normalize to the common schema.
    listings = normalize_all(raw_listings)

    # 3. Dedupe against the seen-store (report each item at most once).
    store = SeenStore()
    try:
        new_listings = store.filter_new(listings)
        summary.new_after_dedupe = len(new_listings)

        # 4. Collapse the same item cross-posted to several marketplaces.
        deduped, dropped = collapse(new_listings, cfg.dedupe)
        summary.near_dups_collapsed = len(dropped)
        summary.after_near_dup = len(deduped)

        # 5. Deterministic pre-filter (kill cheap noise before any LLM call).
        survivors, _pf_stats = prefilter_apply(deduped, cfg.prefilter)
        summary.after_prefilter = len(survivors)

        # 5b. Optional enrichment: let adapters fill in missing detail (e.g. a
        #     Craigslist listing body) for survivors before scoring (section 5.2).
        _enrich(survivors, adapters_by_name)

        # 6. LLM scoring (only survivors get scored).
        scorer = Scorer(cfg.scoring)
        scored = scorer.score(survivors) if survivors else []
        summary.scored = len(scored)

        # 7. Rank, threshold, cap (profile overrides applied).
        digest = rank_and_cap(scored, threshold, max_results)
        summary.reported = len(digest)

        # 8. Render + deliver.
        html_body = render_html(digest, summary, cfg.delivery.group_by)
        text_body = render_text(digest, summary)
        if digest or cfg.delivery.send_when_empty:
            subject = f"{subject_prefix}: {summary.reported} matches ({summary.date})"
            if dry_run:
                print(text_body)
            else:
                deliver(cfg.delivery, subject, html_body, text_body)
        else:
            logger.info("nothing to report and send_when_empty is false; skipping email")

        # 9. Instant alerts for standout items (score >= min_score).
        if not dry_run:
            summary.alerts_sent = send_alerts(cfg.alerts, digest)

        # 10. Update the seen-store (idempotency) — every listing we evaluated,
        #     including pre-filtered and collapsed-away cross-posts.
        if not dry_run:
            scores_by_id = {s.listing.id: s.score for s in scored}
            reported_ids = {d.listing.id for d in digest}
            store.record_run(new_listings, scores_by_id, reported_ids)
    finally:
        store.close()

    _write_run_log(cfg.run_log_path, summary, dry_run)

    logger.info(
        "run complete: fetched=%d new=%d near_dup=-%d scored=%d reported=%d alerts=%d",
        summary.total_fetched, summary.new_after_dedupe, summary.near_dups_collapsed,
        summary.scored, summary.reported, summary.alerts_sent,
    )
    return summary


def _enrich(survivors: list, adapters_by_name: dict) -> None:
    """Ask each source's adapter to enrich its own survivors (best-effort)."""
    by_source: dict[str, list] = {}
    for listing in survivors:
        by_source.setdefault(listing.source, []).append(listing)
    for source, items in by_source.items():
        adapter = adapters_by_name.get(source)
        if adapter is None:
            continue
        try:
            adapter.enrich(items)
        except Exception as exc:  # noqa: BLE001 - enrichment is optional
            logger.info("enrichment for '%s' failed: %s", source, exc)


def _write_run_log(path: str | None, summary: RunSummary, dry_run: bool) -> None:
    if not path or dry_run:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    logger.info("wrote run log to %s", p)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Marketplace Monitor — daily run")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the pipeline but do not send email, alerts, or write state",
    )
    parser.add_argument("--source", help="run only this one marketplace (e.g. ebay)")
    parser.add_argument(
        "--profile",
        help="run a named profile from config (e.g. 'hot') — narrows categories "
        "and applies its threshold/cap/subject overrides",
    )
    parser.add_argument(
        "--list-sources", action="store_true", help="list registered marketplaces and exit"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config + credential readiness and exit (no fetching)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _load_dotenv()

    if args.list_sources:
        print("registered marketplaces:", ", ".join(available()))
        return 0

    if args.check:
        from .doctor import format_checks, run_checks, worst_status

        cfg = load_config(args.config)
        checks = run_checks(cfg)
        print(f"Config: {args.config or 'config.yaml'}")
        print(format_checks(checks))
        status = worst_status(checks)
        print(f"\nResult: {status.upper()}")
        return 1 if status == "fail" else 0

    try:
        run(args.config, dry_run=args.dry_run, only_source=args.source, profile=args.profile)
    except ValueError as exc:  # config-level problems (e.g. unknown profile)
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
