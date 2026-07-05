"""The seen-store (section 7.3).

A single SQLite table keyed by ``Listing.id`` gives us FR-3 (dedupe: each item
reported at most once) and idempotency (re-running a day re-reports nothing).

SQLite works locally and in CI. For CI persistence you point ``STORE_URL`` /
the config at a committed DB file or a Turso/libSQL URL (section 10.3); the
libsql driver is used automatically when the URL is not a plain path.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import REPO_ROOT
from .models import Listing

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
  id          TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  url         TEXT NOT NULL,
  first_seen  TIMESTAMP NOT NULL,
  score       INTEGER,
  reported    INTEGER NOT NULL DEFAULT 0
);
"""


class SeenStore:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("STORE_URL") or str(REPO_ROOT / "data" / "seen.db")
        self._conn = self._connect(self.url)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    @staticmethod
    def _connect(url: str):
        # Turso / libSQL: hosted SQLite-compatible DB (recommended for CI).
        if url.startswith("libsql://") or url.startswith("http://") or url.startswith("https://"):
            try:
                import libsql_experimental as libsql  # type: ignore

                auth = os.environ.get("STORE_AUTH_TOKEN")
                logger.info("Connecting to libSQL store")
                return libsql.connect(url, auth_token=auth)
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "libsql_experimental is required for libsql:// stores; "
                    "pip install libsql-experimental"
                ) from exc
        # Plain SQLite file.
        Path(url).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(url)

    def filter_new(self, listings: list[Listing]) -> list[Listing]:
        """Return only listings we have never seen before (FR-3)."""
        if not listings:
            return []
        known = self._known_ids([l.id for l in listings])
        return [l for l in listings if l.id not in known]

    def _known_ids(self, ids: list[str]) -> set[str]:
        known: set[str] = set()
        # Chunk to stay well under SQLite's variable limit.
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            placeholders = ",".join("?" for _ in chunk)
            cur = self._conn.execute(
                f"SELECT id FROM seen WHERE id IN ({placeholders})", chunk
            )
            known.update(row[0] for row in cur.fetchall())
        return known

    def record(self, listing: Listing, score: int | None = None, reported: bool = False) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO seen (id, source, url, first_seen, score, reported) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (listing.id, listing.source, listing.url, now, score, 1 if reported else 0),
        )

    def record_all(self, scored, reported_ids: set[str]) -> None:
        """Persist every listing we scored so we never re-report it.

        ``scored`` is an iterable of ScoredListing; ``reported_ids`` are the ids
        that actually made the digest.
        """
        for item in scored:
            self.record(item.listing, score=item.score, reported=item.listing.id in reported_ids)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()
