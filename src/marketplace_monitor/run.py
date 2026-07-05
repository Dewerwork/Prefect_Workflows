"""Orchestrator — one daily run (section 6.1).

Pipeline: fetch (per adapter, isolated) -> normalize -> dedupe (seen-store)
-> deterministic pre-filter -> LLM scorer -> rank + threshold + cap
-> render + deliver -> update seen-store.

A broken adapter degrades gracefully (partial digest + error note); it never
aborts the run (FR-10 / NFR reliability). The whole thing is idempotent: a
second run on the same day re-reports nothing (FR-3), because everything scored
is written to the seen-store.
"""

from __future__ import annotations

import argparse
import logging

from .adapters import build_adapter
from .config import Config, load_config
from .deliver import deliver
from .normalize import normalize_all
from .prefilter import apply as prefilter_apply
from .report import RunSummary, render_html, render_text
from .score import Scorer, rank_and_cap
from .store import SeenStore

logger = logging.getLogger(__name__)


def run(config_path: str | None = None, *, dry_run: bool = False) -> RunSummary:
    cfg = load_config(config_path)
    logging.getLogger("marketplace_monitor").setLevel(logging.INFO)
    summary = RunSummary()

    # 1. Fetch from every enabled marketplace (isolated per adapter).
    raw_listings = []
    for mc in cfg.enabled_marketplaces():
        try:
            adapter = build_adapter(mc.name, location=cfg.location, options=mc.options)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not build adapter '%s': %s", mc.name, exc)
            summary.adapter_errors.append(f"{mc.name}: {exc}")
            continue
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

        # 4. Deterministic pre-filter (kill cheap noise before any LLM call).
        survivors, _pf_stats = prefilter_apply(new_listings, cfg.prefilter)
        summary.after_prefilter = len(survivors)

        # 5. LLM scoring (only survivors get scored).
        scorer = Scorer(cfg.scoring)
        scored = scorer.score(survivors) if survivors else []
        summary.scored = len(scored)

        # 6. Rank, threshold, cap.
        digest = rank_and_cap(scored, cfg.scoring.threshold, cfg.scoring.max_results)
        summary.reported = len(digest)

        # 7. Render + deliver.
        html_body = render_html(digest, summary, cfg.delivery.group_by)
        text_body = render_text(digest, summary)
        if digest or cfg.delivery.send_when_empty:
            subject = f"{cfg.delivery.subject_prefix}: {summary.reported} matches ({summary.date})"
            if dry_run:
                print(text_body)
            else:
                deliver(cfg.delivery, subject, html_body, text_body)
        else:
            logger.info("nothing to report and send_when_empty is false; skipping email")

        # 8. Update the seen-store (idempotency) — unless this is a dry run.
        if not dry_run:
            reported_ids = {d.listing.id for d in digest}
            store.record_all(scored, reported_ids)
    finally:
        store.close()

    logger.info(
        "run complete: fetched=%d new=%d scored=%d reported=%d",
        summary.total_fetched, summary.new_after_dedupe, summary.scored, summary.reported,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Marketplace Monitor — daily run")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the pipeline but do not send email or write the seen-store",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(args.config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
