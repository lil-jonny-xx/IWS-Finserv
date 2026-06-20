"""
Vested broker wrapper (US equities for Indian investors).

Currency : USD
Auth     : No retail API — Vested is built on DriveWealth, whose API is
           partner-only. Holdings + cash are therefore scraped from the Vested
           portal by workers/vested_worker.py (the PMS pattern: log in, read the
           pages, write a feed cache) and read here via equity/brokers/_feed.py.
           A manual .env feed can also be used (override / no-credentials case).

Env vars (per entity, replace {CODE} with the entity code e.g. DHR):
  Scraper  (workers/vested_worker.py):
    VESTED_{CODE}_USERNAME / VESTED_{CODE}_PASSWORD   — portal login
  Manual feed / override (optional):
    VESTED_{CODE}_HOLDINGS        — inline JSON array of positions   (or)
    VESTED_{CODE}_HOLDINGS_FILE   — path to a JSON file with the array
    VESTED_{CODE}_CASH            — un-invested cash (USD)
"""
from decimal import Decimal

from equity.brokers import _feed
from equity.models import EquityHolding

SUPPORTED_ENTITIES: list[str] = []   # populated via .env/scraper; no fixed list
CURRENCY = "USD"
_PREFIX  = "VESTED"
_LABEL   = "vested"


def configured(entity_code: str) -> bool:
    """True when a scraper cache or a manual feed exists for this entity."""
    return _feed.configured(_PREFIX, entity_code)


def fetch_holdings(entity_code: str) -> list[dict]:
    return _feed.load_feed(_PREFIX, entity_code)


def fetch_cash_balance(entity_code: str) -> Decimal:
    """Un-invested cash, in USD (converted to INR by refresh_broker_cash)."""
    return _feed.load_cash(_PREFIX, entity_code)


def normalise(entity_id: int, entity_code: str, raw: list[dict]) -> list[EquityHolding]:
    return _feed.normalise(entity_id, raw, _LABEL, CURRENCY, default_exchange="NASDAQ",
                           as_of_date=_feed.load_as_of(_PREFIX, entity_code))
