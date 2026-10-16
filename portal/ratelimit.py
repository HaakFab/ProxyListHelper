"""Token-bucket rate limiting, backed by the `rate_limit_buckets` table.

Deliberately not in-memory: the portal runs a single uvicorn worker (see
deploy/docker-compose.yml), but a restart would otherwise reset every limiter
to full, and a determined attacker restarting nothing needs that anyway. A
tiny DB table is cheap at this scale (a few hundred rows at most) and survives
a redeploy.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import RateLimitBucket, utcnow


@dataclass(frozen=True)
class Limit:
    capacity: float          # bucket size = max burst
    refill_per_second: float  # steady-state rate


# Limits named in portal_implementation.md §11.
LIMITS = {
    "gate": Limit(capacity=5, refill_per_second=5 / (15 * 60)),          # 5 / 15 min
    "submission_create": Limit(capacity=20, refill_per_second=20 / 3600),  # 20 / hour
    "recheck": Limit(capacity=10, refill_per_second=10 / 3600),           # 10 / hour
    "export": Limit(capacity=1, refill_per_second=1 / 3600),             # 1 / hour
    "worker_poll": Limit(capacity=60, refill_per_second=1),              # 60 / min
}


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"rate limit exceeded, retry after {retry_after_seconds:.0f}s")


def check_and_consume(session: Session, scope: str, identity: str, cost: float = 1.0) -> None:
    """Raises RateLimitExceeded if this (scope, identity) has no tokens left;
    otherwise consumes `cost` tokens. Call this BEFORE doing the guarded work,
    not after -- a limiter checked after the fact protects nothing.
    """
    limit = LIMITS[scope]
    key = f"{scope}:{identity}"
    now = utcnow()

    bucket = session.get(RateLimitBucket, key)
    if bucket is None:
        bucket = RateLimitBucket(key=key, tokens=limit.capacity, updated_at=now)
        session.add(bucket)

    elapsed = max((now - bucket.updated_at).total_seconds(), 0.0)
    bucket.tokens = min(limit.capacity, bucket.tokens + elapsed * limit.refill_per_second)
    bucket.updated_at = now

    if bucket.tokens < cost:
        deficit = cost - bucket.tokens
        retry_after = deficit / limit.refill_per_second if limit.refill_per_second > 0 else 3600
        raise RateLimitExceeded(retry_after)

    bucket.tokens -= cost
