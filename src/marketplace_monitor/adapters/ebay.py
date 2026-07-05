"""eBay adapter — official Browse API (section 5.1, P0).

The easiest and cleanest source: register an app, get OAuth client credentials,
call a documented REST endpoint. No scraping, no proxies. This is the reference
implementation for the adapter interface.

Auth:  client-credentials OAuth (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET env vars).
Local: biased toward local pickup near the configured ZIP via the
       ``X-EBAY-C-ENDUSERCTX`` contextual-location header + a pickup filter.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from datetime import datetime

import requests

from ..models import RawListing, SearchSpec
from .base import BaseAdapter

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayAdapter(BaseAdapter):
    name = "ebay"

    @classmethod
    def required_env(cls, options=None):
        return ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"]

    def __init__(self, *, location=None, options=None):
        super().__init__(location=location, options=options)
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._limit = int(self.options.get("limit", 50))

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        client_id = os.environ.get("EBAY_CLIENT_ID")
        client_secret = os.environ.get("EBAY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            _OAUTH_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": _SCOPE},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        }
        zip_code = getattr(self.location, "zip_code", None)
        if zip_code:
            headers["X-EBAY-C-ENDUSERCTX"] = (
                f"contextualLocation=country%3DUS%2Czip%3D{zip_code}"
            )
        return headers

    def _build_filter(self, spec: SearchSpec) -> str | None:
        clauses = []
        if spec.max_price is not None or spec.min_price is not None:
            lo = spec.min_price if spec.min_price is not None else 0
            hi = spec.max_price if spec.max_price is not None else ""
            clauses.append(f"price:[{lo}..{hi}]")
            clauses.append("priceCurrency:USD")
        # Bias toward items available for local pickup.
        if self.options.get("local_pickup_only", True):
            clauses.append("deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}")
        return ",".join(clauses) if clauses else None

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:
        params = {"q": spec.query, "limit": self._limit}
        flt = self._build_filter(spec)
        if flt:
            params["filter"] = flt
        if spec.category:
            params["category_ids"] = spec.category
        resp = requests.get(_SEARCH_URL, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        out: list[RawListing] = []
        for item in data.get("itemSummaries", []) or []:
            price = None
            if isinstance(item.get("price"), dict):
                try:
                    price = float(item["price"]["value"])
                except (KeyError, TypeError, ValueError):
                    price = None
            loc = item.get("itemLocation", {}) or {}
            location = ", ".join(
                p for p in (loc.get("city"), loc.get("stateOrProvince")) if p
            ) or loc.get("postalCode")
            image = (item.get("image") or {}).get("imageUrl")
            distance = None
            if isinstance(item.get("distanceFromPickupLocation"), dict):
                try:
                    distance = float(item["distanceFromPickupLocation"]["value"])
                except (KeyError, TypeError, ValueError):
                    distance = None
            out.append(
                RawListing(
                    source=self.name,
                    source_id=item.get("itemId", ""),
                    title=item.get("title", ""),
                    url=item.get("itemWebUrl") or item.get("itemHref", ""),
                    price=price,
                    currency=(item.get("price") or {}).get("currency", "USD"),
                    location=location,
                    distance_mi=distance,
                    posted_at=_parse_date(item.get("itemCreationDate")),
                    description=item.get("shortDescription"),
                    image_url=image,
                    category=spec.category,
                    raw=item,
                )
            )
        return out


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
