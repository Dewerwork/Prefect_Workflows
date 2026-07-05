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
from .base import BaseAdapter

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"\$([0-9][0-9,]*)")


class CraigslistAdapter(BaseAdapter):
    name = "craigslist"

    def __init__(self, *, location=None, options=None):
        super().__init__(location=location, options=options)
        # e.g. "boise" -> boise.craigslist.org
        self.site = self.options.get("site", "sfbay")
        # Default search section: 'sss' = all for sale.
        self.section = self.options.get("section", "sss")

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

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:
        import feedparser  # lazy: keeps the package importable without the dep

        url = self._build_url(spec)
        feed = feedparser.parse(url)
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
