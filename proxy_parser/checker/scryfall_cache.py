"""Caching backends for Scryfall card metadata.

Split out of ScryfallBatchValidator for two reasons:

1. The previous implementation read *every* ``*_meta.json`` in the cache
   directory into memory on construction (~2000 files). Fine once per CLI run,
   wrong for a server that constructs a validator per submission. Lookups here
   are lazy: a key maps to a deterministic filename, so a miss costs one
   ``open()`` rather than a directory scan.

2. Printings and names are kept in separate namespaces. They used to share one
   dict, so a lookup by bare card name could return whichever printing happened
   to be cached first -- meaning a line without a set code might be validated
   against a digital-only or foil-only printing that the submitter never asked
   for.

The portal swaps in a database-backed implementation with the same three
methods, so the cache survives the VPS being destroyed between order rounds.
"""

import json
import os
import re
from typing import Dict, Optional, Protocol

# Cache namespaces. Kept apart so a lookup by bare card name can never return a
# specific printing that the submitter did not ask for.
PRINTING = "set"      # keyed "{set}_{collector_number}"
NAME = "name"         # keyed by exact card name
NONFOIL = "nonfoil"   # best non-foil paper printing found for a name
PAPER = "paper"       # any paper printing found for a name


def _slug(value: str) -> str:
    """Filesystem-safe form of a cache key component."""
    return re.sub(r"[^a-z0-9_\-.]", "_", value.strip().lower())


class ScryfallCache(Protocol):
    """Minimal interface a cache backend must provide."""

    def get(self, namespace: str, key: str) -> Optional[dict]: ...

    def put(self, namespace: str, key: str, payload: dict) -> None: ...


class FileScryfallCache:
    """Lazy, file-backed cache. One JSON document per cached card."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self._memo: Dict[str, Optional[dict]] = {}

    def _path(self, namespace: str, key: str) -> str:
        # PRINTING and NAME keep the historic unprefixed layout so an existing
        # warm cache stays usable; the fallback namespaces carry their prefix.
        if namespace in (PRINTING, NAME):
            filename = f"{_slug(key)}_meta.json"
        else:
            filename = f"{namespace}_{_slug(key)}_meta.json"
        return os.path.join(self.cache_dir, filename)

    def get(self, namespace: str, key: str) -> Optional[dict]:
        memo_key = f"{namespace}:{key.lower()}"
        if memo_key in self._memo:
            return self._memo[memo_key]

        payload = None
        try:
            with open(self._path(namespace, key), "r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, ValueError):
            payload = None

        # Guard against the old flat layout, where a printing file and a name
        # file could collide: only accept a document that really is this card.
        if payload is not None and namespace == NAME:
            if payload.get("name", "").lower() != key.strip().lower():
                payload = None

        self._memo[memo_key] = payload
        return payload

    def put(self, namespace: str, key: str, payload: dict) -> None:
        memo_key = f"{namespace}:{key.lower()}"
        self._memo[memo_key] = payload
        try:
            with open(self._path(namespace, key), "w", encoding="utf-8") as fp:
                json.dump(payload, fp, indent=2)
        except OSError:
            # A cache that cannot be written is a slow cache, not a broken run.
            pass


class MemoryScryfallCache:
    """In-memory only. Used by tests and by one-off validations."""

    def __init__(self):
        self._data: Dict[str, dict] = {}

    def get(self, namespace: str, key: str) -> Optional[dict]:
        return self._data.get(f"{namespace}:{key.strip().lower()}")

    def put(self, namespace: str, key: str, payload: dict) -> None:
        self._data[f"{namespace}:{key.strip().lower()}"] = payload
