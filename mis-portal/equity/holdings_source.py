"""
Single shared source for live broker holdings.

Before this, three workers each hit the broker's `fetch_holdings` independently:
  • equity_price_worker      — every 60s (light refresh: qty / avg / new positions)
  • equity_snapshot_worker   — hourly + open/close (position diff -> trade detection)
  • broker_txn_sync_worker   — daily (symbol -> ISIN map for trades lacking an ISIN)

They pull the SAME data over the SAME (entity, broker) accounts, on overlapping ticks.
This routes all of them through one function with a short-TTL cross-process cache, so
within the cache window only one real broker round-trip happens.

Roles:
  • The 60s price worker is the authoritative fetcher — it calls with refresh=True,
    which always fetches fresh AND repopulates the cache.
  • The lower-frequency consumers call without refresh and reuse that copy when it is
    fresher than `max_age`.

Fail-safe by construction: any cache miss / staleness / Redis or JSON error falls
through to a direct broker fetch, so behaviour is never worse than fetching directly.
The raw broker payload round-trips losslessly through JSON (verified for zerodha /
angel_one / dhan), so a cached copy fed to `normalise()` is identical to a fresh one.
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_redis = None


def _redis_client():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis(
            host="localhost", port=6379, db=0,
            password=os.getenv("REDIS_PASSWORD", ""), decode_responses=True,
        )
    return _redis


def _key(broker: str, entity_code: str) -> str:
    return f"holdings_raw:{broker}:{entity_code}"


def cached_fetch_holdings(broker_module, broker: str, entity_code: str,
                          max_age: float = 90.0, refresh: bool = False):
    """Raw broker holdings, reusing a recent cross-process copy when possible.

    refresh=True  -> always fetch fresh and repopulate the cache (the price worker).
    refresh=False -> return the cached copy if younger than max_age, else fetch.

    Never raises for cache reasons: a Redis/JSON problem just means a direct fetch.
    """
    key = _key(broker, entity_code)
    r = None
    try:
        r = _redis_client()
        if not refresh:
            blob = r.get(key)
            if blob:
                obj = json.loads(blob)
                if obj.get("raw") is not None and (time.time() - obj.get("ts", 0)) <= max_age:
                    return obj["raw"]
    except Exception as e:
        logger.debug(f"holdings cache read skipped ({broker}/{entity_code}): {e}")
        r = None

    raw = broker_module.fetch_holdings(entity_code)   # authoritative fetch

    try:
        if r is not None and raw is not None:
            r.set(key, json.dumps({"ts": time.time(), "raw": raw}, default=str),
                  ex=int(max(max_age * 4, 300)))
    except Exception as e:
        logger.debug(f"holdings cache write skipped ({broker}/{entity_code}): {e}")

    return raw
