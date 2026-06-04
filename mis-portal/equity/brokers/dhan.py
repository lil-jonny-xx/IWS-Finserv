"""
Dhan broker wrapper.

Entities : HHR
Auth     : Client ID + access token (30-day lived).
           No daily refresh — update DHAN_HHR_ACCESS_TOKEN in .env each month
           from the Dhan developer portal (dhanhq.co/developers).

Env vars required:
  DHAN_HHR_CLIENT_ID
  DHAN_HHR_ACCESS_TOKEN
"""
import os
import logging
from decimal import Decimal

from dhanhq import dhanhq
from dhanhq.dhan_context import DhanContext

from equity.models import EquityHolding

logger = logging.getLogger(__name__)

SUPPORTED_ENTITIES = ["HHR"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _env(entity_code: str, key: str) -> str:
    return os.environ[f"DHAN_{entity_code}_{key}"]


def _dhan_client(entity_code: str) -> dhanhq:
    client_id    = _env(entity_code, "CLIENT_ID")
    access_token = _env(entity_code, "ACCESS_TOKEN")
    return dhanhq(DhanContext(client_id, access_token))


# ---------------------------------------------------------------------------
# Holdings fetch
# ---------------------------------------------------------------------------

def fetch_holdings(entity_code: str) -> list[dict]:
    """
    Raw holdings from Dhan.

    Relevant fields per item:
      tradingSymbol, securityId, exchangeSegment, isin,
      totalQty, dpQty, t1Qty, availableQty,
      avgCostPrice, lastTradedPrice, closingPrice
    """
    dhan = _dhan_client(entity_code)
    resp = dhan.get_holdings()

    if resp.get("status") == "failure":
        raise RuntimeError(
            f"[{entity_code}] Dhan holdings failed: {resp.get('remarks')}"
        )

    holdings = resp.get("data") or []
    logger.info(f"[{entity_code}] Dhan: fetched {len(holdings)} holdings")
    return holdings


# ---------------------------------------------------------------------------
# Normalise to EquityHolding dataclass
# ---------------------------------------------------------------------------

def normalise(entity_id: int, entity_code: str, raw: list[dict]) -> list[EquityHolding]:
    """
    Convert Dhan holding dicts to EquityHolding objects.
    Metrics (pnl, returns, exposure) are computed later in equity_sync_worker.

    Dhan exchange segment codes → human-readable exchange:
      NSE_EQ → NSE, BSE_EQ → BSE
    """
    _exchange_map = {
        "NSE_EQ": "NSE",
        "BSE_EQ": "BSE",
        "NSE":    "NSE",
        "BSE":    "BSE",
    }

    result = []
    for h in raw:
        qty = Decimal(str(h["totalQty"]))
        avg = Decimal(str(h["avgCostPrice"]))
        ltp = Decimal(str(h["lastTradedPrice"]))

        if qty <= 0:
            continue

        exchange = _exchange_map.get(h.get("exchangeSegment", ""), "NSE")

        result.append(EquityHolding(
            entity_id            = entity_id,
            broker               = "dhan",
            symbol               = h["tradingSymbol"],
            isin                 = h.get("isin", ""),
            exchange             = exchange,
            quantity             = qty,
            avg_cost             = avg,
            cost                 = (qty * avg).quantize(Decimal("0.01")),
            current_price        = ltp,
            current_market_value = (qty * ltp).quantize(Decimal("0.01")),
        ))

    return result
