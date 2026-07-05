# Local Marketplace Monitor

A personal automation that scans local for-sale listings across multiple
marketplaces once per day, scores each listing against a natural-language
description of what you're looking for, and emails you a ranked digest of the
best matches.

It does the boring part (fetch every day, everywhere) and delegates the
judgment part ("is this actually a good deal / actually what I want?") to an LLM
that reads each listing the way you would, against a written description of your
interests. Output is a single daily email: a short ranked list with a one-line
reason per item and a direct link.

> Full requirements & technical design: [`docs/marketplace-monitor-spec.md`](docs/marketplace-monitor-spec.md).

## How it works

```
schedule ─▶ orchestrator ─▶ adapters ─▶ normalize ─▶ dedupe ─▶ near-dup ─▶ pre-filter ─▶ LLM scorer ─▶ rank+cap ─▶ email
 (cron/CI)                  (isolated)   (Listing)   (SQLite)  (collapse)  (free cuts)   (Claude Haiku)            + alerts
```

1. **Fetch** new listings from each enabled marketplace (each an isolated adapter).
2. **Normalize** every listing to one common schema.
3. **Dedupe** against a seen-store so you never see the same item twice.
4. **Collapse near-dups** — the same item cross-posted to several marketplaces
   is reported once (title + price similarity).
5. **Pre-filter** deterministically (price ceiling, distance, hard excludes) —
   this kills 70–90% of noise for free, *before* any LLM call.
6. **Enrich** (optional) survivors that lack a description — e.g. a light
   follow-up fetch of a Craigslist listing body, opt-in and capped.
7. **Score** each survivor 0–100 against your `preferences.md` with Claude Haiku,
   with a one-sentence rationale.
8. **Rank**, drop anything below threshold, cap the digest length.
9. **Render + send** an HTML email, optionally **ping** for standout items, then
   update the seen-store and write a structured run log.

The design philosophy: *cheap and boring beats clever and fragile.* Official
feeds/APIs are preferred over scraping; the LLM is a filter that only reads
already-fetched text, never a crawler; a broken adapter degrades gracefully and
never aborts the run.

## Marketplaces

| Marketplace | Access path | Effort | Cost | Status |
|-------------|-------------|--------|------|--------|
| eBay | Official Browse API | Low | Free | ✅ enabled |
| Craigslist | Public RSS feed | Low | Free | ✅ enabled |
| KSL Classifieds | HTTP + internal JSON | Medium | Free | ✅ enabled |
| OfferUp | Internal API / Apify actor | Med | Free–$ | ⚪ opt-in |
| Facebook Marketplace | Apify actor | High | $$ | ⚪ opt-in |

Adding a marketplace is one class behind a common interface
(`adapters/base.py`); removing one is deleting it from `adapters/registry.py`.
Nothing else changes.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in ANTHROPIC_API_KEY and any source keys

# Edit what you're looking for (prose, not keywords):
$EDITOR preferences.md
# Edit location, marketplaces, thresholds, delivery:
$EDITOR config.yaml

# Dry run — prints the digest, sends nothing, writes no state:
python -m marketplace_monitor.run --dry-run

# Real run (delivers per config.yaml, updates the seen-store):
python -m marketplace_monitor.run

# Validate config + credential readiness before a real run (exits non-zero on
# a fatal problem — handy as a CI/cron pre-flight):
python -m marketplace_monitor.run --check

# Run a single marketplace (handy for testing one adapter):
python -m marketplace_monitor.run --source ebay --dry-run

# List registered marketplaces:
python -m marketplace_monitor.run --list-sources
```

With no email configured, `delivery.method: console` just prints the digest —
the least-friction way to see it working end-to-end.

## Configuration

Everything is editable in two files, no code changes:

- **`preferences.md`** — a prose description of what you want. This *is* the
  LLM's rubric. Editing your interests = editing this file.
- **`config.yaml`** — location + radius, which marketplaces and searches to run,
  the pre-filter (price ceiling, distance, hard-exclude keywords), scoring
  (model, threshold, digest cap, batching), and delivery.

Secrets are referenced as `${ENV_VAR}` in `config.yaml` and supplied via `.env`
locally or GitHub Actions Secrets in CI — they never live in the repo.

### Required and optional credentials

| Purpose | Env var | Needed for |
|---|---|---|
| LLM scoring | `ANTHROPIC_API_KEY` | always |
| eBay | `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | the eBay adapter |
| Apify actors | `APIFY_TOKEN` | OfferUp / Facebook adapters |
| Email (Resend) | `RESEND_API_KEY` | `delivery.method: resend` |
| Email (SMTP) | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` | `delivery.method: smtp` |
| CI state store | `STORE_URL`, `STORE_AUTH_TOKEN` | Turso seen-store in CI |

Craigslist and KSL need no credentials.

## Instant alerts (optional)

Between daily digests, the monitor can ping you the moment a standout item shows
up. Set `alerts.enabled: true` in `config.yaml`, pick a `channel` (`telegram` or
`discord`) and a `min_score` (default 90), and provide the channel's credentials
(`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, or `DISCORD_WEBHOOK_URL`). Alerts are
best-effort — a failed ping is logged and never affects the digest.

## Observability

Each run writes a structured JSON log to `run_log_path` (default
`data/last_run.json`) with the funnel counts per stage — fetched per source,
new after dedupe, near-dups collapsed, survivors after pre-filter, scored,
reported, alerts sent, and any adapter errors. The GitHub Actions workflow
uploads it as a build artifact so you can see coverage at a glance.

## Scheduling

- **GitHub Actions** (recommended): `.github/workflows/daily.yml` runs the
  pipeline on a daily cron and can be triggered manually. Put your keys in repo
  Secrets. Free at this scale.
- **Local cron**: `python -m marketplace_monitor.run` on any always-on machine.
- **Prefect**: `flow.py` wraps the same pipeline in a Prefect flow for per-stage
  observability and Prefect deployments (`pip install "marketplace-monitor[prefect]"`).

### State persistence in CI

GitHub runners are ephemeral, so the seen-store must live somewhere between
runs. The store speaks SQLite locally (`data/seen.db`) and connects over the
network when `STORE_URL` is a **Turso / libSQL** URL — the recommended CI option
(`pip install "marketplace-monitor[turso]"`).

## Cost

Everything except Facebook is effectively free. LLM scoring with Claude Haiku,
prompt caching, and the pre-filter runs in the **$1–5/month** range at a few
hundred listings/day. Facebook Marketplace (via a paid Apify actor) is the only
real spend — it stays disabled by default and, when enabled, caps how many
searches/results it pulls to bound cost.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest          # pure-pipeline tests: no network, no API key
```

The tests cover normalization, dedupe/idempotency, cross-marketplace near-dup
collapse, the deterministic pre-filter, scorer parsing (with a fake LLM client),
per-adapter parsing (network monkeypatched), report rendering, instant alerts,
the adapter never-raise-past-its-boundary guarantee, and a full orchestrator run.

## Repo layout

```
preferences.md               # your interests (the LLM rubric)
config.yaml                  # marketplaces, location, radius, thresholds, delivery
src/marketplace_monitor/
  adapters/                  # one isolated adapter per marketplace + registry
  models.py                  # Listing / RawListing / ScoredListing / SearchSpec
  normalize.py               # raw -> Listing
  store.py                   # seen-store (SQLite / Turso), dedupe, idempotency
  dedupe.py                  # cross-marketplace near-dup collapse
  prefilter.py               # deterministic cuts
  score.py                   # Claude Haiku scorer (prompt cache + batch)
  report.py                  # HTML + text digest
  deliver.py                 # console / SMTP / Resend
  notify.py                  # instant alerts (Telegram / Discord)
  doctor.py                  # --check config + credential readiness
  run.py                     # orchestrator
flow.py                      # optional Prefect flow wrapper
.github/workflows/daily.yml  # scheduled run + secrets
tests/
```
