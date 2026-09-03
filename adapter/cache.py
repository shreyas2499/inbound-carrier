"""A tiny TTL cache for load records.

Within one carrier call, the negotiation rounds all need the same load's ceiling.
Caching the LOAD_GET for a short window means we read it once instead of hammering
the flaky TMS on every round. Booking always bypasses this and reads live.
"""
from __future__ import annotations

import time


class TTLCache:
    def __init__(self, ttl_seconds: float = 90.0) -> None:
        self.ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key):
        hit = self._store.get(key)
        if hit is None:
            return None
        value, ts = hit
        if time.time() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key, value) -> None:
        self._store[key] = (value, time.time())

    def invalidate(self, key) -> None:
        self._store.pop(key, None)
