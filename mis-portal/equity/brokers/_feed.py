"""
Shared holdings/cash reader for brokers that have no usable public API.

Used by Vested (DriveWealth-backed; API is partner-only) and DBS Wealth
Singapore (private bank; no retail API). Two data sources are supported, in
priority order:

  1. A **scraper cache file** written by the portal scraper workers
     (workers/vested_worker.py, workers/dbs_worker.py). The scraper logs in,
     reads the holdings/cash off the portal pages or a downloaded statement —
     the PMS pattern — and writes the parsed result here. This is the normal
     path once credentials are configured. Default location:
         equity/feeds/{broker}_{entity}.json     (e.g. vested_hhr.json)
     Format:
         {"broker": "vested", "entity": "HHR", "currency": "USD",
          "as_of": "2026-06-19T03:21:00+00:00",
          "cash": "1234.56",
          "holdings": [
            {"symbol": "AAPL", "isin": "US0378331005", "exchange": "NASDAQ",
             "quantity": 10, "avg_cost": 150.0, "current_price": 190.25},
            ...
          ]}

  2. A **manual `.env` feed** — same pattern the Dhan adapter uses for its price
     feed — for transcribing a statement by hand (or overriding the scraper):
         {PREFIX}_{CODE}_HOLDINGS       = <inline JSON array>            (or)
         {PREFIX}_{CODE}_HOLDINGS_FILE  = /path/to/holdings.json
         {PREFIX}_{CODE}_CASH           = 1234.56   # un-invested cash, native ccy
     The file may be either a bare JSON array or the cache object above.

All money figures are in the broker's NATIVE currency (USD/SGD). Conversion to
INR happens later in equity_sync_worker.apply_fx().
"""
import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from equity.models import EquityHolding

logger = logging.getLogger(__name__)

# entity_name (DB) → env var prefix when they differ (matches the other adapters)
_ENV_PREFIX = {
    "Rajani Corp": "RAJANIRCORP",
}

# Where the scraper workers drop the parsed holdings/cash for each (broker, entity).
FEED_DIR = Path(__file__).resolve().parent.parent / "feeds"

# A scraper cache older than this is still used (better stale than empty), but a
# warning is logged so a wedged login surfaces in the run logs.
STALE_AFTER_HOURS = int(os.environ.get("INTL_FEED_STALE_AFTER_HOURS", "48"))


def _prefix(entity_code: str) -> str:
    return _ENV_PREFIX.get(entity_code, entity_code)


def _key(broker_prefix: str, entity_code: str, suffix: str) -> str:
    return f"{broker_prefix}_{_prefix(entity_code)}_{suffix}"


def cache_path(broker_prefix: str, entity_code: str) -> Path:
    """Default scraper cache file for an (broker, entity), e.g. vested_hhr.json."""
    return FEED_DIR / f"{broker_prefix.lower()}_{_prefix(entity_code).lower()}.json"


# ---------------------------------------------------------------------------
# Scraper cache — written by the worker, read by the adapter
# ---------------------------------------------------------------------------

def write_feed_cache(
    broker_prefix: str,
    entity_code: str,
    currency: str,
    holdings: list[dict],
    cash: Decimal | float | str | None = None,
    as_of: datetime | None = None,
    ledger: dict | None = None,
    ledger_through: str | None = None,
) -> Path:
    """Persist a freshly scraped holdings/cash set for the adapter to read.

    Single source of the on-disk format so the scraper and adapter never drift.
    Returns the path written. Money figures stay in NATIVE currency.

    Each holding dict may carry optional ``first_invested_date`` ("YYYY-MM-DD")
    and ``xirr_pct`` (native money-weighted return); the adapter surfaces these.

    ``ledger`` maps symbol → list of {"date","amount"} dated cash flows (buys
    negative, sells positive). It is the persisted transaction history the
    foreign-broker scrapers accumulate — full on first run, previous-business-day
    appends after — and recompute XIRR / first_invested_date from each run.
    ``ledger_through`` is the last business date already ingested (incremental
    cursor). Both are preserved verbatim for the next run.
    """
    FEED_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(broker_prefix, entity_code)
    payload = {
        "broker":   broker_prefix.lower(),
        "entity":   _prefix(entity_code),
        "currency": currency.upper(),
        "as_of":    (as_of or datetime.now(timezone.utc)).isoformat(),
        "cash":     str(cash) if cash is not None else None,
        "holdings": holdings,
        "ledger":   ledger or {},
        "ledger_through": ledger_through,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.chmod(tmp, 0o600)
    tmp.replace(path)   # atomic — never expose a half-written feed to the sync
    logger.info(
        f"[{entity_code}/{broker_prefix}] wrote feed cache "
        f"({len(holdings)} holdings, cash={payload['cash']}, "
        f"ledger_through={ledger_through}) → {path.name}"
    )
    return path


def load_cache_obj(broker_prefix: str, entity_code: str) -> dict | None:
    """Public reader for the scraper — returns the whole cache object (holdings,
    ledger, ledger_through, …) so a daily run can resume incremental ingestion,
    or None when there is no cache yet (→ first run, full history)."""
    return _read_cache(broker_prefix, entity_code)


def load_as_of(broker_prefix: str, entity_code: str) -> "date | None":
    """The date the holdings were actually scraped (feed cache `as_of`), so the
    portal can show the true last-snapshot date — these brokers refresh only a
    few times a week, not on every daily equity sync. None for a manual env feed."""
    cache = _read_cache(broker_prefix, entity_code)
    if cache and cache.get("as_of"):
        try:
            return datetime.fromisoformat(cache["as_of"]).date()
        except Exception:
            return None
    return None


def _read_cache(broker_prefix: str, entity_code: str) -> dict | None:
    """Load the scraper cache object, logging a warning if it is stale."""
    path = cache_path(broker_prefix, entity_code)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"[{entity_code}/{broker_prefix}] unreadable feed cache {path.name}: {e}")
        return None
    as_of = data.get("as_of")
    if as_of:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(as_of)).total_seconds() / 3600
            if age_h > STALE_AFTER_HOURS:
                logger.warning(
                    f"[{entity_code}/{broker_prefix}] feed cache is {age_h:.0f}h old "
                    f"(>{STALE_AFTER_HOURS}h) — scraper may be wedged; using it anyway"
                )
        except Exception:
            pass
    return data


# ---------------------------------------------------------------------------
# Holdings / cash readers (scraper cache first, then manual .env)
# ---------------------------------------------------------------------------

def load_feed(broker_prefix: str, entity_code: str) -> list[dict]:
    """Read the raw holdings list — scraper cache first, then the manual env feed."""
    # 1) Manual inline env explicitly overrides everything (handy for testing).
    inline = os.environ.get(_key(broker_prefix, entity_code, "HOLDINGS"), "").strip()
    if inline:
        return json.loads(inline)

    # 2) Explicit file path in env (bare array or the cache object).
    path = os.environ.get(_key(broker_prefix, entity_code, "HOLDINGS_FILE"), "").strip()
    if path:
        data = json.loads(Path(path).read_text())
        return data["holdings"] if isinstance(data, dict) else data

    # 3) The scraper cache written by the portal worker (the normal path).
    cache = _read_cache(broker_prefix, entity_code)
    if cache is not None:
        return cache.get("holdings") or []

    logger.info(f"[{entity_code}/{broker_prefix}] no holdings feed configured")
    return []


def load_cash(broker_prefix: str, entity_code: str) -> Decimal:
    # 1) Manual env override.
    raw = os.environ.get(_key(broker_prefix, entity_code, "CASH"), "").strip()
    if raw:
        try:
            return Decimal(raw)
        except Exception:
            return Decimal("0")

    # 2) Scraper cache.
    cache = _read_cache(broker_prefix, entity_code)
    if cache is not None and cache.get("cash") not in (None, ""):
        try:
            return Decimal(str(cache["cash"]))
        except Exception:
            return Decimal("0")
    return Decimal("0")


def configured(broker_prefix: str, entity_code: str) -> bool:
    """True if this (broker, entity) has any data source — manual env or a cache
    file. equity_sync uses this (plus a credentials check) to auto-activate an
    international account from .env alone."""
    if os.environ.get(_key(broker_prefix, entity_code, "HOLDINGS")):
        return True
    if os.environ.get(_key(broker_prefix, entity_code, "HOLDINGS_FILE")):
        return True
    return cache_path(broker_prefix, entity_code).exists()


def normalise(
    entity_id: int,
    raw: list[dict],
    broker_label: str,
    currency: str,
    default_exchange: str,
    as_of_date: "date | None" = None,
) -> list[EquityHolding]:
    """Convert feed dicts → EquityHolding objects in native currency.

    Carries the scraper-derived transaction metrics through when present:
    `first_invested_date` (date of first buy → CAGR anchor) and `xirr_pct`
    (native money-weighted return). `as_of_date` (the scrape date) is stamped on
    each holding so the portal shows the true last-snapshot date rather than the
    daily-sync date (compute_metrics preserves it).
    """
    result = []
    for h in raw:
        qty = Decimal(str(h["quantity"]))
        avg = Decimal(str(h["avg_cost"]))
        ltp = Decimal(str(h.get("current_price", h["avg_cost"])))

        if qty <= 0:
            continue

        fid = None
        if h.get("first_invested_date"):
            try:
                fid = date.fromisoformat(str(h["first_invested_date"])[:10])
            except ValueError:
                fid = None
        xirr = None
        if h.get("xirr_pct") not in (None, ""):
            try:
                xirr = Decimal(str(h["xirr_pct"]))
            except Exception:
                xirr = None

        result.append(EquityHolding(
            entity_id            = entity_id,
            broker               = broker_label,
            symbol               = h["symbol"],
            isin                 = h.get("isin", "") or "",
            exchange             = h.get("exchange", default_exchange),
            quantity             = qty,
            avg_cost             = avg,
            cost                 = (qty * avg).quantize(Decimal("0.01")),
            current_price        = ltp,
            current_market_value = (qty * ltp).quantize(Decimal("0.01")),
            currency             = currency,
            first_invested_date  = fid,
            xirr_inception_pct   = xirr,
            as_of_date           = as_of_date,
        ))
    return result
