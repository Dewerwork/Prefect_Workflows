"""Craigslist adapter — public RSS feeds (section 5.2, P0).

Craigslist retired its API, but every search results page publishes an RSS feed
(append ``format=rss``). Consuming a publicly-offered feed is a clean posture
(section 12: the design deliberately stays on the feed rather than scraping
pages, per *Craigslist v. 3Taps*).

RSS gives title, price, URL, timestamp, and sometimes a thumbnail — but not full
body text. Title + price is enough for most filtering; the LLM handles the rest.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from ..models import RawListing, SearchSpec
from .apify import run_apify_actor
from .base import BaseAdapter

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"\$([0-9][0-9,]*)")


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
_BODY_RE = re.compile(r'id="postingbody"[^>]*>(.*?)</section>', re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class CraigslistAdapter(BaseAdapter):
    name = "craigslist"

    def __init__(self, *, location=None, options=None):
        super().__init__(location=location, options=options)
        # e.g. "boise" -> boise.craigslist.org
        self.site = self.options.get("site", "sfbay")
        # Default search section: 'sss' = all for sale.
        self.section = self.options.get("section", "sss")
        # RSS gives no body text. Opt in to a light follow-up fetch of the
        # listing page for borderline items that survived the pre-filter
        # (section 5.2). Off by default; capped and rate-limited when on.
        self.fetch_body = bool(self.options.get("fetch_body", False))
        self.max_body_fetches = int(self.options.get("max_body_fetches", 20))
        self.body_delay = float(self.options.get("body_delay_seconds", 1.0))
        # Fetch mode. Craigslist hard-blocks plain HTTP (even browser-TLS) with a
        # JS challenge, so the practical options are:
        #   "apify"      — an Apify Craigslist actor (reliable; needs apify_actor)
        #   "playwright" — a headless Chromium that clears the challenge
        #   "rss"        — plain HTTP RSS (works only if CL ever relaxes)
        self.mode = self.options.get("mode")
        if not self.mode:
            self.mode = "playwright" if self.options.get("use_playwright") else "rss"
        self.apify_actor = self.options.get("apify_actor", "")
        self.scrape_details = bool(self.options.get("scrape_details", False))
        self.max_items = int(self.options.get("max_items", 50))
        self._session = None
        self._browser_tls = False
        self._warmed = False

    @classmethod
    def required_env(cls, options=None):
        options = options or {}
        return ["APIFY_TOKEN"] if options.get("mode") == "apify" else []

    def fetch(self, queries: list[SearchSpec]) -> list[RawListing]:
        if self.mode == "playwright":
            return self._fetch_all_playwright(queries)
        return super().fetch(queries)  # rss / apify handled per-search in _fetch

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:
        if self.mode == "apify":
            return self._fetch_apify(spec)
        return self._fetch_rss(spec)

    def _fetch_apify(self, spec: SearchSpec) -> list[RawListing]:
        if not self.apify_actor:
            logger.info("[craigslist] mode=apify but no apify_actor configured; skipping")
            return []
        # Input shape for lulzasaur/craigslist-scraper (verified schema):
        #   searchQueries (list), city, category (CL section), maxListings,
        #   scrapeDetails, minPrice, maxPrice.
        run_input = {
            "searchQueries": [spec.query],
            "city": self.site,
            "category": spec.extra.get("section", self.section),
            "maxListings": self.max_items,
            "scrapeDetails": self.scrape_details,
        }
        if spec.max_price is not None:
            run_input["maxPrice"] = spec.max_price
        if spec.min_price is not None:
            run_input["minPrice"] = spec.min_price
        run_input.update(self.options.get("extra_input", {}))
        items = run_apify_actor(self.apify_actor, run_input)
        return [self._to_raw_apify(item, spec) for item in items if item]

    def _to_raw_apify(self, item: dict, spec: SearchSpec) -> RawListing | None:
        title = item.get("title") or item.get("name") or ""
        url = item.get("url") or item.get("link") or ""
        if not title or not url:
            return None
        price = _coerce_price(item.get("price"))
        loc = item.get("location") or item.get("locality") or item.get("hood") or self.site
        if isinstance(loc, dict):
            loc = loc.get("city") or loc.get("name")
        image = item.get("image") or item.get("imageUrl") or item.get("thumbnail")
        if isinstance(image, list) and image:
            image = image[0]
        return RawListing(
            source=self.name,
            source_id=item.get("id") or item.get("pid") or _extract_id(url),
            title=title,
            url=url,
            price=price,
            location=loc if isinstance(loc, str) else None,
            posted_at=_parse_date(item.get("datetime") or item.get("postedAt") or item.get("date")),
            description=item.get("description") or item.get("body"),
            image_url=image if isinstance(image, str) else None,
            category=spec.category,
            raw=item,
        )

    def _fetch_all_playwright(self, queries: list[SearchSpec]) -> list[RawListing]:
        """Drive one headless Chromium across all searches: navigate to the
        region homepage first so the JS bot-challenge resolves and sets its
        clearance cookie, then fetch each search's RSS feed within that same
        browser context (which now carries the cookie)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning(
                "[craigslist] use_playwright is set but Playwright isn't installed. "
                "Run: pip install playwright && python -m playwright install chromium"
            )
            return []

        results: list[RawListing] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=_UA, locale="en-US")
                page = context.new_page()
                # Warm up: clear the challenge on the homepage.
                try:
                    page.goto(f"https://{self.site}.craigslist.org/",
                              wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[craigslist] warm-up navigation note: %s", exc)

                for spec in queries:
                    try:
                        found = self._fetch_one_playwright(page, spec)
                        logger.info("[craigslist] '%s' -> %d listings", spec.query, len(found))
                        results.extend(found)
                    except Exception as exc:  # noqa: BLE001 - per-search isolation
                        logger.warning("[craigslist] search '%s' failed: %s", spec.query, exc)
                browser.close()
        except Exception as exc:  # noqa: BLE001 - never abort the run
            logger.warning("[craigslist] playwright fetch failed: %s", exc)
        return results

    def _fetch_one_playwright(self, page, spec: SearchSpec) -> list[RawListing]:
        url = self._build_url(spec)
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = resp.status if resp else None
        body = resp.body() if resp else b""
        logger.debug("[craigslist] (pw) GET %s -> status=%s len=%d", url, status, len(body or b""))
        if status != 200:
            logger.info("[craigslist] (pw) status %s for %s", status, url)
            return []
        return self._parse_feed(body, spec)

    def _build_url(self, spec: SearchSpec) -> str:
        section = spec.extra.get("section", self.section)
        base = f"https://{self.site}.craigslist.org/search/{section}"
        params = [f"query={requests_quote(spec.query)}", "format=rss"]
        if spec.max_price is not None:
            params.append(f"max_price={int(spec.max_price)}")
        if spec.min_price is not None:
            params.append(f"min_price={int(spec.min_price)}")
        radius = getattr(self.location, "radius_mi", None)
        zip_code = getattr(self.location, "zip_code", None)
        if radius and zip_code:
            params.append(f"search_distance={radius}")
            params.append(f"postal={zip_code}")
        return base + "?" + "&".join(params)

    def _get_session(self):
        """Lazily create a browser-TLS session (curl_cffi) and warm it up on the
        region homepage so Craigslist sets its cookies before we fetch the feed.
        Falls back to requests when curl_cffi isn't installed."""
        if self._session is not None:
            return self._session
        try:
            from curl_cffi import requests as cffi

            self._session = cffi.Session(impersonate="chrome")
            self._browser_tls = True
        except ImportError:
            import requests

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": _UA})
            self._browser_tls = False
        # Warm up cookies from the homepage (best-effort).
        try:
            self._session.get(f"https://{self.site}.craigslist.org/", timeout=20)
        except Exception:  # noqa: BLE001
            pass
        self._warmed = True
        return self._session

    def _fetch_rss(self, spec: SearchSpec) -> list[RawListing]:
        url = self._build_url(spec)
        headers = {
            "Accept": "application/rss+xml,application/xml,text/xml,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://{self.site}.craigslist.org/search/{spec.extra.get('section', self.section)}",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        session = self._get_session()
        resp = session.get(url, headers=headers, timeout=30)
        status = resp.status_code
        content = resp.content
        logger.debug("[craigslist] GET %s -> status=%s len=%d (browser-tls=%s)",
                     url, status, len(content or b""), self._browser_tls)
        if status != 200:
            if status == 403 and not self._browser_tls:
                logger.info(
                    "[craigslist] 403 (TLS fingerprint block). Install curl_cffi "
                    "(pip install curl_cffi) to fetch with a real browser TLS handshake."
                )
            else:
                logger.info(
                    "[craigslist] status %s for %s (browser-tls=%s)",
                    status, url, self._browser_tls,
                )
            return []
        out = self._parse_feed(content, spec)
        if not out:
            logger.info("[craigslist] 0 entries (status 200) for %s", url)
        return out

    def _parse_feed(self, content, spec: SearchSpec) -> list[RawListing]:
        import feedparser

        feed = feedparser.parse(content)
        out: list[RawListing] = []
        for entry in feed.entries:
            link = entry.get("link", "")
            if not link:
                continue
            title = entry.get("title", "")
            price = _extract_price(title) or _extract_price(entry.get("summary", ""))
            image = None
            for enc in entry.get("enclosures", []) or []:
                if enc.get("type", "").startswith("image"):
                    image = enc.get("href")
                    break
            out.append(
                RawListing(
                    source=self.name,
                    source_id=_extract_id(link),
                    title=title,
                    url=link,
                    price=price,
                    location=self.site,
                    posted_at=_parse_date(entry.get("published") or entry.get("updated")),
                    description=entry.get("summary"),
                    image_url=image,
                    category=spec.category,
                    raw=dict(entry),
                )
            )
        return out

    def enrich(self, listings: list) -> None:
        """Fetch listing-page bodies for items still missing a description.

        Opt-in (``fetch_body: true``) and capped (``max_body_fetches``) to keep
        volume/blocking risk low — the design's advice is to start RSS-only and
        only reach for bodies on borderline items (section 5.2 / 14).
        """
        if not self.fetch_body:
            return
        import time

        import requests

        session = requests.Session()
        session.headers.update({"User-Agent": _UA})
        fetched = 0
        for listing in listings:
            if fetched >= self.max_body_fetches:
                break
            if listing.source != self.name or listing.description:
                continue
            try:
                time.sleep(self.body_delay)
                resp = session.get(listing.url, timeout=20)
                resp.raise_for_status()
                body = _extract_body(resp.text)
                if body:
                    listing.description = body
                    fetched += 1
            except Exception as exc:  # noqa: BLE001 - best-effort enrichment
                logger.info("[craigslist] body fetch failed for %s: %s", listing.url, exc)
        if fetched:
            logger.info("[craigslist] enriched %d listing bodies", fetched)


def _extract_body(html_text: str) -> str | None:
    m = _BODY_RE.search(html_text)
    if not m:
        return None
    text = _TAG_RE.sub(" ", m.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def requests_quote(text: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(text)


def _extract_price(text: str | None) -> float | None:
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _coerce_price(value) -> float | None:
    """Parse a price that may be a number, a "$40" string, or a bare "40"."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[0-9][0-9,]*(?:\.[0-9]+)?", str(value))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _extract_id(url: str) -> str:
    m = re.search(r"/(\d+)\.html", url)
    return m.group(1) if m else url


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None
