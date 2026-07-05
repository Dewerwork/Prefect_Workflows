"""The marketplace adapter contract (section 6.3).

Every marketplace implements the same tiny interface so the orchestrator neither
knows nor cares how a listing was obtained. Adding a marketplace = writing one
class; removing one = deleting it from the registry. Nothing else changes.

The cardinal rule (FR-10 / NFR reliability): ``fetch`` must never raise past its
own boundary. On any failure it logs and returns ``[]`` so one broken source can
never abort the whole run. ``BaseAdapter.fetch`` enforces this by wrapping the
subclass hook ``_fetch`` in a try/except.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from ..models import RawListing, SearchSpec

logger = logging.getLogger(__name__)


@runtime_checkable
class MarketplaceAdapter(Protocol):
    name: str

    def fetch(self, queries: list[SearchSpec]) -> list[RawListing]:
        """Return raw listings for the given searches. Must not raise past its
        own boundary — on failure, log and return []."""
        ...


class BaseAdapter:
    """Convenience base that provides the never-raise guarantee.

    Subclasses implement ``_fetch`` for a *single* SearchSpec and get isolation
    per-search for free: one bad query does not lose the others, and one bad
    adapter does not lose the run.
    """

    name: str = "base"

    def __init__(self, *, location=None, options: dict | None = None):
        self.location = location
        self.options = options or {}

    def _fetch(self, spec: SearchSpec) -> list[RawListing]:  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch(self, queries: list[SearchSpec]) -> list[RawListing]:
        results: list[RawListing] = []
        for spec in queries:
            try:
                found = self._fetch(spec) or []
                logger.info("[%s] '%s' -> %d listings", self.name, spec.query, len(found))
                results.extend(found)
            except Exception as exc:  # noqa: BLE001 - deliberate boundary
                logger.warning("[%s] search '%s' failed: %s", self.name, spec.query, exc)
        return results
