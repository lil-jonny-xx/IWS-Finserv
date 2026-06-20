"""
DBS Wealth Singapore broker wrapper.

Currency : SGD
Auth     : No retail API — DBS private banking. Holdings + cash are scraped from
           the DBS iWealth portal by workers/dbs_worker.py (the PMS pattern: log
           in, download the holdings statement, parse it, write a feed cache) and
           read here via equity/brokers/_feed.py. A manual .env feed can also be
           used (override / no-credentials case).

Env vars (per entity, replace {CODE} with the entity code e.g. DHR):
  Scraper  (workers/dbs_worker.py):
    DBS_{CODE}_USERNAME / DBS_{CODE}_PASSWORD   — portal login
  Manual feed / override (optional):
    DBS_{CODE}_HOLDINGS        — inline JSON array of positions   (or)
    DBS_{CODE}_HOLDINGS_FILE   — path to a JSON file with the array
    DBS_{CODE}_CASH            — cash balance (SGD)
"""
from decimal import Decimal

from equity.brokers import _feed
from equity.models import EquityHolding

SUPPORTED_ENTITIES: list[str] = []   # populated via .env/scraper; no fixed list
CURRENCY = "SGD"
_PREFIX  = "DBS"
_LABEL   = "dbs"


def configured(entity_code: str) -> bool:
    """True when a scraper cache or a manual feed exists for this entity."""
    return _feed.configured(_PREFIX, entity_code)


def fetch_holdings(entity_code: str) -> list[dict]:
    return _feed.load_feed(_PREFIX, entity_code)


def fetch_cash_balance(entity_code: str) -> Decimal:
    """Cash balance, in SGD (converted to INR by refresh_broker_cash)."""
    return _feed.load_cash(_PREFIX, entity_code)


def normalise(entity_id: int, entity_code: str, raw: list[dict]) -> list[EquityHolding]:
    return _feed.normalise(entity_id, raw, _LABEL, CURRENCY, default_exchange="SGX",
                           as_of_date=_feed.load_as_of(_PREFIX, entity_code))
