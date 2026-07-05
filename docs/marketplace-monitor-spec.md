---
title: Local Marketplace Monitor — Requirements & Technical Design
status: Draft v1.0
owner: David
last_updated: 2026-07-05
tags: [scraping, llm-filtering, automation, marketplaces]
---

# Local Marketplace Monitor

A personal automation that scans local for-sale listings across multiple marketplaces once per day, scores each listing against a natural-language description of what you're looking for, and emails you a ranked digest of the best matches.

---

## 1. Overview

### 1.1 Problem

Finding worthwhile local items (homesteading gear, wargaming/miniatures lots, tools, furniture, kids' stuff, deals to flip) means manually re-running the same searches across Facebook Marketplace, KSL, Craigslist, OfferUp, and eBay every day. It's repetitive, easy to forget, and keyword search misses good listings that are titled badly or priced as a bundle. The judgment ("is this actually a good deal / actually what I want?") is the part a human does well and a keyword filter does badly.

### 1.2 Solution

A scheduled pipeline that does the boring part (fetch every day, everywhere) and delegates the judgment part to an LLM that reads each listing the way you would, against a written description of your interests. Output is a single daily email: a short ranked list with a one-line reason per item and a direct link.

### 1.3 Design philosophy

- **Cheap and boring beats clever and fragile.** Prefer official feeds/APIs over scraping wherever they exist; only reach for headless browsers and paid unblockers where there's no alternative.
- **The LLM is a filter, not a crawler.** It never touches a website. It only reads already-fetched, normalized text and scores it. This keeps cost and failure modes contained.
- **Personal-scale.** One user, a few hundred listings/day, one email. No dashboards, no infra to babysit. If a marketplace adapter breaks, the rest keep working.

---

## 2. Goals & Non-Goals

### 2.1 Goals

- Monitor **multiple marketplaces** in one run: eBay, Craigslist, KSL Classifieds, Facebook Marketplace, OfferUp (Nextdoor optional/stretch).
- Accept **natural-language preferences** ("I want cast-iron cookware under $40, any 28mm sci-fi miniatures especially bundles, garden/canning equipment, power tools if clearly a deal") rather than rigid keyword lists.
- Produce a **daily ranked digest** delivered by email, deduped so you never see the same listing twice.
- Run **unattended** on a schedule with no manual step.
- Stay **cheap** (target: under ~$10/month all-in) and **low-maintenance**.

### 2.2 Non-Goals (v1)

- Not a reseller/arbitrage analytics suite (no sold-comps valuation, no cross-posting).
- No automated messaging or buying — it surfaces items, you contact sellers.
- No web UI. Config is a file; output is an email.
- Not real-time. Daily batch is the contract. (Hot categories could move to hourly later.)
- No mobile app.

---

## 3. Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | Fetch new listings from each enabled marketplace for a configured location + radius. |
| FR-2 | Normalize every listing to a common schema regardless of source. |
| FR-3 | Deduplicate against previously-seen listings so each item is reported at most once. |
| FR-4 | Apply a cheap deterministic pre-filter (price ceiling, distance, hard excludes) before any LLM call. |
| FR-5 | Score each surviving listing 0–100 against the user's natural-language preferences using an LLM, with a one-sentence rationale. |
| FR-6 | Rank results, drop anything below a score threshold, and cap the digest length. |
| FR-7 | Render a clean daily report (email; ranked, grouped by category or marketplace) with title, price, distance, source, thumbnail, direct link, and the LLM's reason. |
| FR-8 | Deliver the report on a daily schedule; send a "nothing new today" note or skip, per config. |
| FR-9 | Preferences, marketplaces, location, thresholds, and schedule are all editable in one config file without code changes. |
| FR-10 | Failure of one marketplace adapter must not abort the run; log it and continue. |

---

## 4. Non-Functional Requirements

- **Reliability:** A broken scraper degrades gracefully (partial report + error note), never crashes the whole job.
- **Cost:** LLM + infra target under ~$10/mo at a few hundred listings/day.
- **Maintainability:** Each marketplace is an isolated adapter behind a common interface. Adding/removing one touches nothing else.
- **Idempotency:** Re-running the same day produces no duplicate emails and re-reports nothing already seen.
- **Observability:** Per-run log of listings fetched / filtered / scored / reported, per marketplace, plus adapter errors.
- **Portability:** Runs locally (cron) or in CI (GitHub Actions) with only env-var/secret changes.

---

## 5. Marketplace Access Reality

This is the crux of the whole project. Each marketplace has a very different access story, and that dictates effort, cost, and legal exposure. Ordered from easiest to hardest.

### 5.1 eBay — Official API ✅ (easiest)

- **Access:** Official **Browse API** (the Finding API is legacy/deprecated — use Browse). Free developer tier is generous for personal use.
- **Local filter:** Supports `deliveryCountry`, item location, and pickup filters; you can bias toward local pickup and a distance from a ZIP.
- **Effort:** Low. Register an app, get OAuth client credentials, call a documented REST endpoint. No scraping, no proxies, no ToS gray area.
- **Verdict:** Do this first. It's the reference implementation for the adapter interface.

### 5.2 Craigslist — Public RSS feeds ✅ (easy, underrated)

- **Access:** No API (they retired it), but **every search results page exposes an RSS feed** — add `format=rss` / use the RSS link on the results page. This is a publicly-offered feed, so consuming it is clean.
- **Local filter:** Native — you're already querying a specific Craigslist region (e.g. `boise.craigslist.org`) with search terms, price, and distance params in the URL.
- **Effort:** Low. Build the search URL, fetch RSS, parse. No headless browser.
- **Caveat:** RSS gives title, price, URL, timestamp, sometimes a thumbnail — but not full body text. For most filtering the title+price is enough; if you need the description, a light follow-up fetch of the listing page works but raises volume/blocking risk. Start with RSS-only.
- **Legal note:** *Craigslist v. 3Taps* established that scraping **after** a cease-and-desist + IP block can trigger CFAA liability. Consuming the RSS feed they publicly publish is a different posture. Stay on the feed.

### 5.3 KSL Classifieds — Scrape, moderate friction ⚠️

- **Access:** No official public API. Big and very relevant in Idaho/Utah, so worth the effort. Search pages render results that can be fetched; there's an internal JSON endpoint the site's own frontend calls that's often cleaner than parsing HTML — worth inspecting via browser dev tools (Network tab) before writing an HTML parser.
- **Local filter:** Native (KSL is regional; filter by miles from ZIP, category, price).
- **Effort:** Medium. Plain HTTP + parsing usually works; moderate anti-bot. A realistic User-Agent and gentle rate limiting go a long way. Escalate to a headless browser only if blocked.
- **Verdict:** High value for your region. Prioritize right after eBay + Craigslist.

### 5.4 OfferUp — Reverse-engineered API ⚠️

- **Access:** No public API, but a **private GraphQL/JSON API** backs the app and site; it can be called directly once you capture the request shape. Mobile-first.
- **Local filter:** Native (it's a local-first marketplace).
- **Effort:** Medium–high. The internal API shifts occasionally and has anti-abuse measures. An Apify actor or an existing OSS wrapper may save you the reverse-engineering.
- **Verdict:** Second wave. Nice coverage but not worth blocking v1 on.

### 5.5 Facebook Marketplace — Hostile 🛑 (hardest, highest cost/risk)

- **Access:** **No official API for Marketplace listings.** Actively anti-scraping. Realistic options:
  1. **Third-party service (recommended):** an **Apify actor** built for FB Marketplace, or a scraping/unblocker API (Bright Data, Oxylabs, ScraperAPI). You pay per result/request; they handle proxies, headless browsers, and blocking. This is by far the least painful path.
  2. **DIY headless browser** (Playwright) + residential proxies + a logged-in session. Cheapest in dollars, most expensive in maintenance — it breaks often and login/session handling is fiddly and risky.
- **Local filter:** Native and excellent (FB Marketplace is inherently local) — *if* you can get in.
- **Legal note:** *Meta v. Bright Data* (2024) went largely against Meta on scraping of **public** data, and courts have been skeptical that scraping public pages breaches the CFAA (cf. *hiQ v. LinkedIn*). This is not legal advice, but the public-data posture is more defensible than logged-in scraping. Volume discipline and no login reduce both risk and breakage.
- **Verdict:** Highest value (biggest local inventory) but highest effort. **Use a paid actor, don't hand-roll it.** Treat it as its own phase.

### 5.6 Nextdoor — Very locked down 🛑 (stretch / skip)

- **Access:** Login-walled, hostile, no meaningful API. For-sale section exists but is hard to reach programmatically.
- **Verdict:** Skip for v1. Revisit only if you find a maintained actor and decide the marginal inventory is worth it.

### 5.7 Access summary

| Marketplace | Best access path | Effort | ~Cost | Priority |
|-------------|------------------|--------|-------|----------|
| eBay | Official Browse API | Low | Free | P0 |
| Craigslist | Public RSS feed | Low | Free | P0 |
| KSL | HTTP + parse internal JSON | Medium | Free | P1 |
| OfferUp | Internal API / Apify actor | Med–High | Free–$ | P2 |
| Facebook Marketplace | Apify actor / unblocker API | High | $$ | P2 (own phase) |
| Nextdoor | — | Very high | $$ | Skip |

---

## 6. System Architecture

### 6.1 Pipeline (single daily run)

```
                 ┌─────────────────────────────────────────────┐
   schedule ───► │                 orchestrator                │
   (cron/CI)     └─────────────────────────────────────────────┘
                                     │
        ┌────────────┬───────────────┼───────────────┬────────────┐
        ▼            ▼               ▼               ▼            ▼
   ┌────────┐  ┌──────────┐   ┌──────────┐    ┌──────────┐  ┌──────────┐
   │  eBay  │  │Craigslist│   │   KSL    │    │ OfferUp  │  │ FB Mkt   │   adapters
   │ (API)  │  │  (RSS)   │   │ (HTTP)   │    │ (API)    │  │ (Apify)  │   (isolated)
   └────┬───┘  └────┬─────┘   └────┬─────┘    └────┬─────┘  └────┬─────┘
        └───────────┴──────────────┴───────────────┴────────────┘
                                     │  raw listings
                                     ▼
                            ┌─────────────────┐
                            │   normalize     │  → common Listing schema
                            └────────┬────────┘
                                     ▼
                            ┌─────────────────┐
                            │   dedupe        │  ← seen-store (SQLite/Turso)
                            └────────┬────────┘  (new listings only)
                                     ▼
                            ┌─────────────────┐
                            │ pre-filter      │  price ceiling, distance,
                            │ (deterministic) │  hard excludes  → cut volume
                            └────────┬────────┘
                                     ▼
                            ┌─────────────────┐
                            │ LLM scorer      │  ← preferences.md (cached prompt)
                            │ (Claude Haiku)  │  score 0–100 + reason, batched
                            └────────┬────────┘
                                     ▼
                            ┌─────────────────┐
                            │ rank + threshold│  drop < threshold, cap N
                            └────────┬────────┘
                                     ▼
                            ┌─────────────────┐
                            │ render + send   │  HTML email digest
                            └─────────────────┘
                                     │
                                     ▼
                            update seen-store
```

### 6.2 The two-stage filter (why it matters)

Do **not** send every fetched listing to the LLM. Stage it:

1. **Deterministic pre-filter (free):** drop anything over the price ceiling, outside the radius, in an excluded category, or matching hard-exclude keywords. This is where you kill the 70–90% of noise cheaply.
2. **LLM scorer (cheap but not free):** only survivors get scored. This keeps token spend low and lets the model spend its "attention budget" on genuinely plausible items.

This staging is the single biggest cost and quality lever in the design.

### 6.3 Adapter interface

Every marketplace implements the same contract so the orchestrator doesn't know or care how a listing was obtained:

```python
class MarketplaceAdapter(Protocol):
    name: str
    def fetch(self, queries: list[SearchSpec]) -> list[RawListing]:
        """Return raw listings for the given searches. Must not raise past
        its own boundary — on failure, log and return []."""
```

Adding a marketplace = writing one class. Removing one = deleting it from the registry. Nothing else changes.

---

## 7. Data Model

### 7.1 Normalized `Listing`

```python
@dataclass
class Listing:
    id: str            # stable dedupe key: f"{source}:{source_id}" or hash(url)
    source: str        # "ebay" | "craigslist" | "ksl" | "offerup" | "facebook"
    title: str
    price: float | None
    currency: str      # "USD"
    url: str
    location: str      # human-readable, e.g. "Nampa, ID"
    distance_mi: float | None
    posted_at: datetime | None
    description: str | None   # may be empty (e.g. Craigslist RSS)
    image_url: str | None
    category: str | None
    raw: dict           # original payload, for debugging
```

### 7.2 Scored result (adds LLM output)

```python
@dataclass
class ScoredListing:
    listing: Listing
    score: int          # 0–100
    reason: str         # one sentence: why it matches (or the caveat)
    matched_interest: str | None   # which preference bucket it hit
```

### 7.3 Seen-store

A single table keyed by `Listing.id` recording `first_seen_at` and the score it got. Used for dedupe (FR-3) and idempotency (re-runs skip known IDs). SQLite locally; **Turso (libSQL)** or a committed SQLite file for CI persistence (see §10.3).

```sql
CREATE TABLE seen (
  id           TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  url          TEXT NOT NULL,
  first_seen   TIMESTAMP NOT NULL,
  score        INTEGER,
  reported     BOOLEAN NOT NULL DEFAULT 0
);
```

---

## 8. Semantic Filtering Layer

### 8.1 Model choice

**Claude Haiku** (current small model) for scoring. Rationale: this is high-volume, latency-tolerant, and each judgment is simple ("does this listing match these interests, and how well?"). Haiku is cheap, fast, and more than smart enough for this. Reserve a larger model only if you later find Haiku missing nuance.

### 8.2 Preferences as a document, not keywords

Store interests in a human-written `preferences.md` — prose, not a keyword list. Example shape:

```markdown
# What I'm looking for

## Always interested (score high)
- Cast-iron cookware (Lodge, Griswold, unbranded) under $40
- 28mm sci-fi miniatures, especially bundles/lots/army boxes; sprues OK
- Canning & food-preservation gear: pressure canners, jars by the case, dehydrators
- Quality hand tools and power tools *if clearly underpriced*

## Interested if it's a deal
- Solid-wood furniture for a workshop/basement
- Garden equipment, raised-bed materials, greenhouse parts

## Not interested (score 0)
- Cars, car parts, electronics unless explicitly listed above
- Anything "for parts / not working" unless a miniatures lot
```

This prose becomes the LLM's rubric. Editing your interests = editing this file. No code, no redeploy.

### 8.3 Scoring prompt design

- **System prompt** = the full `preferences.md` + scoring instructions + required output format. This is stable across every call in a run, so it's a perfect **prompt-caching** target (cache it once, reuse for every listing → big token savings).
- **User turn** = one listing (or a small batch) as compact text.
- **Output** = strict JSON: `{"score": int, "reason": str, "matched_interest": str|null}`. Instruct "respond with only the JSON object, no preamble."

Score each listing to 0–100:
- 80–100: strong match, act fast
- 50–79: plausible, worth a look
- 1–49: weak/tangential
- 0: explicitly excluded

Threshold for the digest defaults to **≥ 60**, configurable.

### 8.4 Cost control

- **Prompt caching** on the system prompt (the big, stable part) — cached input tokens are heavily discounted.
- **Batch API** — daily runs aren't latency-sensitive, so submit scoring as a batch for ~50% off standard rates.
- **Micro-batch listings** — put several listings in one request (each scored independently in the JSON array) to amortize per-call overhead, while keeping each judgment isolated in the output.
- **Pre-filter first** (§6.2) so you're only paying to score plausible items.

### 8.5 Rough cost math

Assume ~400 listings/day survive dedupe, ~120 survive the pre-filter and get scored, each ~500 input + ~120 output tokens. That's ~60K input + ~14K output tokens/day. At Haiku pricing that's **cents per day** before caching/batch — comfortably in the **$1–5/month** range even with headroom. LLM cost is not the constraint here; the FB Marketplace fetch is (see §11).

---

## 9. Report / Digest

### 9.1 Format

HTML email, ranked highest-score first, optionally grouped by category ("Miniatures", "Homestead", "Tools"). Each item shows:

- **Score badge** + **title** (linked directly to the listing)
- **Price**, **distance**, **source**, **posted time**
- **Thumbnail** (if available)
- **One-line reason** from the LLM ("Full Lodge Dutch oven + skillet set, $35, well under your ceiling")

Header line: counts per marketplace and total scanned → reported, so you can see coverage at a glance. If nothing clears threshold, send a one-line "nothing today" (configurable to skip entirely).

### 9.2 Delivery

- **Email:** transactional sender — **Resend**, **AWS SES**, or plain SMTP (even a Gmail app password) for a personal tool. Resend's free tier is the least-friction start.
- **Nice-to-have later:** a second channel (Telegram bot / Discord webhook) for high-score (≥90) instant pings between daily digests.

---

## 10. Orchestration & Scheduling

### 10.1 Recommended: GitHub Actions (scheduled workflow)

- **Why:** free for this scale (2,000 free minutes/month on private repos; unlimited on public), no server to run, secrets management built in, logs retained, trivial to trigger manually for testing.
- **How:** a `schedule:` cron trigger (e.g. daily 7:00 AM local → set in UTC) runs the pipeline; API keys and the Apify token live in repo **Secrets**.
- **Caveat:** GitHub's scheduled triggers can lag by minutes under load and may pause on repos with no recent activity — fine for a daily digest.

### 10.2 Alternatives

| Option | Fit |
|--------|-----|
| **Local cron** (your Windows box / a Pi) | Simplest if you have an always-on machine; you own uptime. |
| **n8n (self-hosted)** | Good if you want a visual pipeline and easy multi-channel delivery; more infra. |
| **Cloud Function + Scheduler** (AWS Lambda + EventBridge, etc.) | Clean and cheap, slightly more setup than Actions. |
| **Make.com / Zapier** | Only for the no-code path; awkward for custom scraping + LLM. |

### 10.3 State persistence in CI

GitHub Actions runners are ephemeral, so the seen-store needs to live somewhere between runs. Options, cleanest first:

1. **Turso (libSQL):** hosted SQLite-compatible DB, generous free tier — the seen-store just connects over the network. Recommended.
2. **Commit SQLite back to a `data` branch** at end of run — works, zero extra services, but adds commit noise.
3. **Actions cache / artifact** — possible but eviction makes it unreliable for a source of truth.

---

## 11. Cost Summary

| Component | Approach | Est. monthly |
|-----------|----------|--------------|
| Orchestration | GitHub Actions | $0 |
| eBay | Official API | $0 |
| Craigslist | RSS | $0 |
| KSL | HTTP scrape | $0 |
| OfferUp | Internal API / actor | $0–$X |
| **Facebook Marketplace** | **Apify actor / unblocker** | **~$5–45** ← main cost |
| LLM scoring | Claude Haiku + batch + cache | ~$1–5 |
| Email delivery | Resend/SES free tier | $0 |
| State store | Turso free tier | $0 |
| **Total** | | **~$5–50** |

The FB Marketplace fetch is the entire cost story. **Everything else is effectively free.** Ship without FB first and you're at ~$1–5/month; add FB when you decide the inventory is worth the per-result cost, and cap how many searches/results you pull to control it.

---

## 12. Legal & ToS Considerations

Not legal advice — but the relevant shape of the landscape:

- **Public data scraping** has been treated relatively favorably: *hiQ v. LinkedIn* (scraping public pages isn't obviously CFAA "unauthorized access") and *Meta v. Bright Data* (2024, largely against Meta re: public data). Staying on **public, non-logged-in** data is the more defensible posture.
- **Craigslist v. 3Taps:** scraping **after** an explicit block/C&D can create CFAA exposure — which is exactly why the design uses Craigslist's **published RSS feed** rather than hammering their pages.
- **ToS ≠ criminal law, but it's still ToS.** Facebook's terms prohibit scraping; the realistic risk for a low-volume personal tool is account/IP blocking, not a lawsuit — which is another reason to prefer a third-party actor (their problem to manage) and avoid logged-in scraping.
- **Practical hygiene:** identify honestly, rate-limit gently, cache aggressively so you re-fetch as little as possible, personal use only, don't redistribute the data. Low volume + public data + published feeds = low risk.

---

## 13. Rollout Plan

### Phase 0 — Skeleton (½ day)
Adapter interface, normalize, SQLite seen-store, dedupe, and a plaintext email. Wire up **eBay (API)** and **Craigslist (RSS)** only — both free and easy. Prove the end-to-end loop with a dumb pre-filter and *no* LLM yet.

**Exit criteria:** a daily email arrives with real deduped listings from two sources.

### Phase 1 — Intelligence (½–1 day)
Add the **Claude Haiku scorer**, `preferences.md`, prompt caching, batch API, ranking + threshold, and the HTML digest. Move scheduling to **GitHub Actions** with **Turso** for state.

**Exit criteria:** the email is ranked and reasoned, config-driven, running unattended daily in CI.

### Phase 2 — Coverage (1–2 days, incremental)
Add **KSL** (highest regional value), then **OfferUp**. Each is an isolated adapter — ship them one at a time.

**Exit criteria:** KSL + OfferUp listings appear in the digest; a broken adapter degrades gracefully.

### Phase 3 — Facebook Marketplace (own phase, when justified)
Integrate an **Apify FB Marketplace actor** behind the same adapter interface. Cap searches/results to bound cost. This is the only phase that adds real spend.

**Exit criteria:** FB listings in the digest at a known, capped monthly cost.

### Later / optional
- Instant Telegram/Discord ping for score ≥ 90.
- Per-category schedules (hot categories hourly).
- Sold-comps enrichment for flip candidates.
- Nextdoor (only if a maintained actor appears).

---

## 14. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| FB Marketplace scraper breaks | High | Use a maintained Apify actor, not hand-rolled; isolate behind adapter so the rest survives. |
| KSL/OfferUp anti-bot blocks | Medium | Realistic UA, gentle rate limits, prefer internal JSON endpoints; escalate to actor only if needed. |
| Craigslist RSS lacks descriptions | Medium | Filter on title+price first; optional light body fetch only for borderline items. |
| LLM false-negatives (misses a good item) | Medium | Tune threshold conservatively (lower it); keep the reason field to audit misses; iterate on `preferences.md`. |
| Cost creep from FB per-result pricing | Medium | Hard caps on searches/results; keep FB in its own phase and toggleable. |
| CI state loss (seen-store) | Low | Turso as source of truth, not Actions cache. |
| Duplicate/cross-posted items across marketplaces | Medium | Optional near-dup detection (title+price+image similarity) as a later enhancement. |

---

## 15. Open Questions

1. **Search seeds vs. broad pull?** Do you want a fixed set of saved searches per marketplace, or a broad "recent local listings" pull that the LLM sifts entirely? (Broad = better recall, higher volume/cost.)
2. **Radius?** Confirm the mile radius from Caldwell and whether Boise/Nampa/Meridian are all in-scope.
3. **Digest timing?** One morning email, or morning + evening?
4. **FB from day one, or defer?** Given it's the only cost, worth deciding whether Phase 3 is soon or "maybe later."
5. **Instant alerts?** Is a same-day ping for standout items (≥90) valuable, or is one daily digest the whole point?

---

## Appendix A — Suggested Repo Layout

```
marketplace-monitor/
├─ preferences.md              # your interests (the LLM rubric)
├─ config.yaml                 # marketplaces, location, radius, thresholds, schedule
├─ src/
│  ├─ adapters/
│  │  ├─ base.py               # MarketplaceAdapter protocol
│  │  ├─ ebay.py               # official Browse API
│  │  ├─ craigslist.py         # RSS
│  │  ├─ ksl.py                # HTTP + parse
│  │  ├─ offerup.py            # internal API / actor
│  │  └─ facebook.py           # Apify actor
│  ├─ normalize.py             # → Listing
│  ├─ store.py                 # seen-store (SQLite/Turso), dedupe
│  ├─ prefilter.py             # deterministic cuts
│  ├─ score.py                 # Claude Haiku scorer (cache + batch)
│  ├─ report.py                # HTML digest
│  ├─ deliver.py               # email (Resend/SES/SMTP)
│  └─ run.py                   # orchestrator
├─ .github/workflows/daily.yml # cron + secrets
└─ tests/
```

## Appendix B — OSS Starting Points

- **`BoPeng/ai-marketplace-monitor`** — an existing AI-assisted marketplace monitor worth reading/forking for patterns (adapter structure, LLM filtering, notification wiring) before building from scratch.
- **Apify Store** — search for maintained **Facebook Marketplace** and **OfferUp** actors; check recent update dates and run success rates before committing.
- **`python-craigslist`-style libraries** exist but the RSS approach is simpler and more robust — prefer RSS unless you need richer fields.

> Vet any OSS scraper's **maintenance status** (last commit, open issues) before depending on it — marketplace scrapers rot fast when sites change.
