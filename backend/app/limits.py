"""Rate limits for the public site: every clip and question costs model credits, and a guest session is free to start.

Kept in memory, which suits the single replica this app runs on. Admins aren't limited.
"""

import ipaddress
import logging
import math
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

log = logging.getLogger("gaugelens.limits")
_warned = False
HOUR = 3600.0
MAX_KEYS = 50_000                # beyond this, keys with no recent hits are dropped


class RateLimiter:
    """Sliding windows by key. The clock is injectable for tests."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.hits: dict[str, deque] = defaultdict(deque)

    def retry_after(self, key: str, limit: int, window_s: float = HOUR) -> float:
        """0 and the hit is counted if allowed; otherwise the seconds until it would be, and it isn't counted."""
        now = self.clock()
        hits = self.hits[key]
        while hits and now - hits[0] >= window_s:
            hits.popleft()
        if len(hits) >= limit:
            return window_s - (now - hits[0])
        hits.append(now)
        if len(self.hits) > MAX_KEYS:
            self.hits = defaultdict(deque, {k: q for k, q in self.hits.items() if q and now - q[-1] < window_s})
        return 0.0


def client_ip(request: Request) -> str:
    """The caller's address. Proxies append to X-Forwarded-For, so anything a client sends itself is on the left:
    reading from the right, the first public address is the one our ingress saw. Internal hops are skipped."""
    for part in reversed(request.headers.get("x-forwarded-for", "").split(",")):
        try:
            address = ipaddress.ip_address(part.strip())
        except ValueError:
            continue
        if address.is_global:
            return str(address)
    global _warned
    if not _warned:                                # behind our ingress this would make every visitor share one limit
        _warned = True
        log.warning("no public address in X-Forwarded-For: rate limits are using the connecting peer's address")
    return request.client.host if request.client else "unknown"


def enforce(request: Request, key: str, limit: int, doing: str) -> None:
    """429 with Retry-After when `key` is over `limit` per hour. `doing` finishes "Too many ... ; try again in N minutes"."""
    wait = request.app.state.limiter.retry_after(key, limit)
    if wait:
        minutes = math.ceil(wait / 60)
        raise HTTPException(status_code=429, headers={"Retry-After": str(math.ceil(wait))},
                            detail=f"Too many {doing}; try again in {minutes} minute{'' if minutes == 1 else 's'}.")


def caller_key(principal, request: Request) -> tuple[str, bool]:
    """(the key a caller's usage is counted under, is it a guest). A new guest session mustn't reset the count, so
    guests count by address; accounts count by account."""
    return (f"ip:{client_ip(request)}", True) if principal.kind == "guest" else (f"user:{principal.user_id}", False)
