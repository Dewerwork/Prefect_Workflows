"""Adapter registry.

Adding a marketplace = registering one class here. Removing one = deleting the
entry. The orchestrator only ever asks the registry to build the adapters named
in the config; it never imports a concrete adapter directly.
"""

from __future__ import annotations

from .base import MarketplaceAdapter
from .craigslist import CraigslistAdapter
from .ebay import EbayAdapter
from .facebook import FacebookAdapter
from .ksl import KslAdapter
from .offerup import OfferUpAdapter

_REGISTRY: dict[str, type] = {
    EbayAdapter.name: EbayAdapter,
    CraigslistAdapter.name: CraigslistAdapter,
    KslAdapter.name: KslAdapter,
    OfferUpAdapter.name: OfferUpAdapter,
    FacebookAdapter.name: FacebookAdapter,
}


def available() -> list[str]:
    return sorted(_REGISTRY)


def build_adapter(name: str, *, location=None, options: dict | None = None) -> MarketplaceAdapter:
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown marketplace '{name}'; available: {', '.join(available())}"
        ) from exc
    return cls(location=location, options=options or {})
