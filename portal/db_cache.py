"""ScryfallCache backend on top of the `scryfall_cache` DB table.

Implements the same Protocol as proxy_parser.checker.scryfall_cache.ScryfallCache
(get/put), so ScryfallBatchValidator works unmodified against it. This is what
makes the metadata cache survive a VPS teardown: it ships inside the regular
`portal.db` backup, so the round after a snapshot restore starts warm instead
of re-fetching ~1800 unique cards from Scryfall.

TTLs: successful lookups 30 days (portal/settings.py scryfall_cache_ttl_days);
misses (a not-found result the caller chose to cache) get a much shorter TTL
by the caller passing one in -- this backend just stores whatever expires_at
it's given.
"""
import json
from datetime import timedelta

from sqlalchemy.orm import Session

from .models import ScryfallCacheRow, utcnow


class SqliteScryfallCache:
    """One instance per check job; cheap to construct -- it does no I/O until
    get()/put() are called, unlike the old file-cache's eager directory scan.
    """

    def __init__(self, session: Session, ttl_days: int = 30):
        self.session = session
        self.ttl_days = ttl_days

    def get(self, namespace: str, key: str) -> dict | None:
        row = self.session.get(ScryfallCacheRow, (namespace, key.strip().lower()))
        if row is None:
            return None
        if row.expires_at < utcnow():
            return None
        try:
            return json.loads(row.payload)
        except ValueError:
            return None

    def put(self, namespace: str, key: str, payload: dict, ttl_days: int | None = None) -> None:
        normalized_key = key.strip().lower()
        expires = utcnow() + timedelta(days=ttl_days if ttl_days is not None else self.ttl_days)
        row = self.session.get(ScryfallCacheRow, (namespace, normalized_key))
        if row is None:
            row = ScryfallCacheRow(namespace=namespace, key=normalized_key)
            self.session.add(row)
        row.payload = json.dumps(payload)
        row.fetched_at = utcnow()
        row.expires_at = expires
        # Flush (not commit) so a later get() in the same request/job sees it
        # immediately, without forcing the caller's transaction boundary.
        self.session.flush()
