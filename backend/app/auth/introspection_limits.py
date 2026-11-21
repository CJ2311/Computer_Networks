"""Process-local admission control; never retains credentials or token digests."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address
import math
import os
from threading import BoundedSemaphore, Lock
import time

from fastapi import HTTPException


@dataclass(frozen=True)
class IntrospectionSettings:
    timeout_seconds: float = 5
    max_concurrent: int = 8
    global_per_minute: int = 600
    peer_per_minute: int = 60
    max_peers: int = 1024

    @classmethod
    def from_environment(cls):
        try:
            timeout = float(os.getenv("COLLEGE_INTROSPECTION_TIMEOUT_SECONDS", "5"))
            integers = [int(os.getenv(name, str(default))) for name, default in (
                ("COLLEGE_INTROSPECTION_MAX_CONCURRENT", 8),
                ("COLLEGE_INTROSPECTION_GLOBAL_PER_MINUTE", 600),
                ("COLLEGE_INTROSPECTION_PEER_PER_MINUTE", 60),
                ("COLLEGE_INTROSPECTION_MAX_PEERS", 1024),
            )]
            if (not math.isfinite(timeout) or not 0 < timeout <= 60
                    or any(not 1 <= value <= 2**31 - 1 for value in integers)):
                raise ValueError
            return cls(timeout, *integers)
        except (ValueError, OverflowError):
            raise HTTPException(503, "College authentication protection configuration is invalid") from None


class IntrospectionGuard:
    def __init__(self, settings: IntrospectionSettings, clock=time.monotonic):
        self.settings = settings
        self.clock = clock
        self.lock = Lock()
        self.slots = BoundedSemaphore(settings.max_concurrent)
        self.global_bucket = (float(settings.global_per_minute), clock())
        self.peers = OrderedDict()

    def _refill(self, bucket, capacity, now):
        tokens, previous = bucket
        return min(capacity, tokens + max(0, now - previous) * capacity / 60)

    def admit(self, client_host: str | None):
        try:
            # Ignore every forwarding header; unknown/non-IP peers share one bucket.
            peer = str(ip_address(client_host))
        except (ValueError, TypeError):
            peer = "unknown"
        with self.lock:
            now = self.clock()
            # LRU order also records last admission time. Fully refilled idle buckets
            # can be removed without giving an active attacker a fresh budget.
            while self.peers and next(iter(self.peers.values()))[1] <= now - 60:
                self.peers.popitem(last=False)
            global_tokens = self._refill(self.global_bucket, self.settings.global_per_minute, now)
            self.global_bucket = (global_tokens, now)
            if global_tokens < 1:
                self._limited(60 / self.settings.global_per_minute)
            self.global_bucket = (global_tokens - 1, now)
            if peer not in self.peers and len(self.peers) >= self.settings.max_peers:
                self._limited(60)
            tokens = self._refill(self.peers.get(peer, (float(self.settings.peer_per_minute), now)),
                                  self.settings.peer_per_minute, now)
            self.peers[peer] = (tokens - 1 if tokens >= 1 else tokens, now)
            self.peers.move_to_end(peer)
            if tokens < 1:
                self._limited((1 - tokens) * 60 / self.settings.peer_per_minute)

    @staticmethod
    def _limited(delay):
        raise HTTPException(429, "Too many college authentication attempts",
                            headers={"Retry-After": str(max(1, math.ceil(delay)))})

    @contextmanager
    def provider_slot(self):
        if not self.slots.acquire(blocking=False):
            raise HTTPException(503, "College authentication provider is busy", headers={"Retry-After": "1"})
        try:
            yield
        finally:
            self.slots.release()


@lru_cache(maxsize=1)
def _configured_guard():
    # Configuration is stable for this worker's lifetime; restart to change limits.
    return IntrospectionGuard(IntrospectionSettings.from_environment())


_initialization_lock = Lock()


def get_introspection_guard():
    # lru_cache alone may execute its factory multiple times on concurrent misses.
    with _initialization_lock:
        return _configured_guard()
