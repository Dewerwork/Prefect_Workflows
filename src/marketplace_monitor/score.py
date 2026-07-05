"""Semantic filtering layer (section 8).

The LLM is a *filter, not a crawler*: it never touches a website, it only reads
already-fetched, normalized text and scores it 0-100 against the user's
natural-language ``preferences.md``.

Cost control levers from section 8.4, all implemented here:
  * Prompt caching on the big, stable system prompt (preferences + rubric).
  * Micro-batching several listings per request.
  * Optional Batch API submission for ~50% off on the daily (latency-tolerant) run.
  * Only survivors of the deterministic pre-filter reach this stage.

Uses Claude Haiku (section 8.1): cheap, fast, and more than smart enough for a
simple "does this match, and how well?" judgment.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import REPO_ROOT, ScoringConfig
from .models import Listing, ScoredListing

logger = logging.getLogger(__name__)

_SCORING_INSTRUCTIONS = """
You are a filter for a personal local-marketplace monitor. Above is the user's
written description of what they are looking for. For each listing you are given,
decide how well it matches those interests and assign an integer score 0-100:

- 80-100: strong match, act fast
- 50-79: plausible, worth a look
- 1-49:  weak or tangential
- 0:      explicitly excluded by the "not interested" rules

Judge each listing independently. Use the title, price, location, and
description. Missing descriptions are common (some feeds only give a title and
price) — score on what you have; do not penalize a listing merely for a short
description. Reward clear underpricing and bundles/lots where the preferences
call for them.

For each listing return:
  - "id": the exact id you were given
  - "score": integer 0-100
  - "reason": ONE short sentence saying why it matches (or the caveat)
  - "matched_interest": the short name of the preference bucket it hit, or null

Respond with ONLY a JSON object of the form {"results": [ ... ]}, one entry per
listing, no preamble.
""".strip()

_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                    "matched_interest": {"type": ["string", "null"]},
                },
                "required": ["id", "score", "reason", "matched_interest"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def load_preferences(path: str | None = None) -> str:
    p = Path(path) if path else REPO_ROOT / "preferences.md"
    if not Path(p).is_absolute():
        p = REPO_ROOT / p
    return Path(p).read_text(encoding="utf-8")


def _system_blocks(preferences: str) -> list[dict]:
    """System prompt = preferences + rubric, cached as one stable prefix.

    This is identical across every call in a run, so a single ``cache_control``
    breakpoint on the last block lets every subsequent request read it back at
    ~0.1x input cost (section 8.3 / 8.4).
    """
    return [
        {"type": "text", "text": preferences},
        {
            "type": "text",
            "text": _SCORING_INSTRUCTIONS,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _render_listing(listing: Listing) -> str:
    parts = [f"id: {listing.id}", f"title: {listing.title}"]
    if listing.price is not None:
        parts.append(f"price: ${listing.price:.0f}")
    else:
        parts.append("price: (not listed)")
    if listing.location:
        parts.append(f"location: {listing.location}")
    if listing.distance_mi is not None:
        parts.append(f"distance: {listing.distance_mi:.0f} mi")
    parts.append(f"source: {listing.source}")
    if listing.description:
        desc = listing.description.strip().replace("\n", " ")
        parts.append(f"description: {desc[:600]}")
    return "\n".join(parts)


def _user_prompt(batch: list[Listing]) -> str:
    blocks = [f"Listing {i + 1}:\n{_render_listing(l)}" for i, l in enumerate(batch)]
    return "Score these listings:\n\n" + "\n\n---\n\n".join(blocks)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _parse_results(text: str) -> dict[str, dict]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("scorer returned non-JSON output; skipping batch")
        return {}
    out: dict[str, dict] = {}
    for entry in payload.get("results", []):
        if "id" in entry and "score" in entry:
            out[str(entry["id"])] = entry
    return out


def _to_scored(batch: list[Listing], parsed: dict[str, dict]) -> list[ScoredListing]:
    scored: list[ScoredListing] = []
    for listing in batch:
        entry = parsed.get(listing.id)
        if not entry:
            # Model dropped this one — treat as un-scored (score 0) rather than
            # inventing a score.
            continue
        try:
            score = max(0, min(100, int(entry["score"])))
        except (TypeError, ValueError):
            continue
        scored.append(
            ScoredListing(
                listing=listing,
                score=score,
                reason=str(entry.get("reason", "")).strip(),
                matched_interest=entry.get("matched_interest") or None,
            )
        )
    return scored


class Scorer:
    def __init__(self, cfg: ScoringConfig, client=None):
        self.cfg = cfg
        self.preferences = load_preferences(cfg.preferences_path)
        self._client = client  # injectable for tests

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def score(self, listings: list[Listing]) -> list[ScoredListing]:
        if not listings:
            return []
        batches = list(_chunks(listings, self.cfg.batch_size))
        if self.cfg.use_batch_api:
            return self._score_batch_api(batches)
        return self._score_sync(batches)

    # -- synchronous micro-batch scoring -------------------------------------
    def _score_sync(self, batches: list[list[Listing]]) -> list[ScoredListing]:
        system = _system_blocks(self.preferences)
        results: list[ScoredListing] = []
        for batch in batches:
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": _user_prompt(batch)}],
                output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
            )
            text = next((b.text for b in resp.content if b.type == "text"), "")
            results.extend(_to_scored(batch, _parse_results(text)))
        return results

    # -- Batch API scoring (section 8.4: ~50% off) ---------------------------
    def _score_batch_api(self, batches: list[list[Listing]]) -> list[ScoredListing]:
        import time

        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        system = _system_blocks(self.preferences)
        by_custom_id: dict[str, list[Listing]] = {}
        requests = []
        for i, batch in enumerate(batches):
            custom_id = f"batch-{i}"
            by_custom_id[custom_id] = batch
            requests.append(
                Request(
                    custom_id=custom_id,
                    params=MessageCreateParamsNonStreaming(
                        model=self.cfg.model,
                        max_tokens=1024,
                        system=system,
                        messages=[{"role": "user", "content": _user_prompt(batch)}],
                        output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
                    ),
                )
            )

        job = self.client.messages.batches.create(requests=requests)
        logger.info("submitted scoring batch %s (%d requests)", job.id, len(requests))
        while True:
            job = self.client.messages.batches.retrieve(job.id)
            if job.processing_status == "ended":
                break
            time.sleep(30)

        results: list[ScoredListing] = []
        for result in self.client.messages.batches.results(job.id):
            batch = by_custom_id.get(result.custom_id, [])
            if result.result.type != "succeeded":
                logger.warning("batch item %s: %s", result.custom_id, result.result.type)
                continue
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            results.extend(_to_scored(batch, _parse_results(text)))
        return results


def rank_and_cap(scored: list[ScoredListing], threshold: int, max_results: int) -> list[ScoredListing]:
    """FR-6: drop below threshold, rank highest first, cap the digest length."""
    keep = [s for s in scored if s.score >= threshold]
    keep.sort(key=lambda s: s.score, reverse=True)
    return keep[:max_results]
