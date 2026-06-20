"""
Interactive Brokers wrapper — Flex Web Service (automated, no gateway needed).

Currency : USD (per-position currency is read from the statement; the account
           base currency, used for cash, defaults to USD).
Auth     : A Flex Web Service token + a saved Flex Query that includes the
           "Open Positions" (and optionally "Cash Report") sections. The daily
           sync pulls the generated statement over HTTPS — no TWS / IB Gateway
           session required.

Setup (one-time, in IBKR Client Portal → Settings → Account Settings):
  1. Reporting → Flex Web Service → enable, generate a Token (valid ~1 year).
  2. Reporting → Flex Queries → create an *Activity* Flex Query with the
     "Open Positions" section (Summary level) and "Cash Report" section.
     Format: XML. Note the Query ID.

Env vars (per entity, replace {CODE} with the entity code e.g. DHR):
  IBKR_{CODE}_FLEX_TOKEN      — Flex Web Service token
  IBKR_{CODE}_QUERY_ID        — saved Flex Query ID (Open Positions + Cash Report)
  IBKR_{CODE}_TRADES_QUERY_ID — optional; saved Flex Query with the Trades section,
                                used once by the inception backfill (Since / XIRR)
  IBKR_{CODE}_BASE_CURRENCY   — optional; account base currency for cash (default USD)
"""
import logging
import os
import time
import xml.etree.ElementTree as ET
from decimal import Decimal

import requests

from equity.models import EquityHolding

logger = logging.getLogger(__name__)

SUPPORTED_ENTITIES: list[str] = []   # configured via .env per entity
CURRENCY = "USD"
_LABEL   = "ibkr"

_SEND_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
_GET_URL  = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"
_VERSION  = "3"

# entity_name (DB) → env var prefix when they differ (matches the other adapters)
_ENV_PREFIX = {
    "Rajani Corp": "RAJANIRCORP",
}


def _env(entity_code: str, key: str, required: bool = True, default: str = "") -> str:
    prefix = _ENV_PREFIX.get(entity_code, entity_code)
    val = os.environ.get(f"IBKR_{prefix}_{key}", default)
    if required and not val:
        raise KeyError(f"IBKR_{prefix}_{key} not set in .env")
    return val


def cash_currency(entity_code: str) -> str:
    return _env(entity_code, "BASE_CURRENCY", required=False, default=CURRENCY) or CURRENCY


# ---------------------------------------------------------------------------
# Flex Web Service — two-step fetch
# ---------------------------------------------------------------------------

def _fetch_statement_xml(entity_code: str, query_id: str = "") -> ET.Element:
    token    = _env(entity_code, "FLEX_TOKEN")
    query_id = query_id or _env(entity_code, "QUERY_ID")

    # Step 1 — request statement generation, get a reference code.
    resp = requests.get(
        _SEND_URL,
        params={"t": token, "q": query_id, "v": _VERSION},
        timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        raise RuntimeError(
            f"[{entity_code}] IBKR Flex SendRequest failed: "
            f"{root.findtext('ErrorCode')} {root.findtext('ErrorMessage')}"
        )
    reference_code = (root.findtext("ReferenceCode") or "").strip()
    get_url        = (root.findtext("Url") or _GET_URL).strip()
    if not reference_code:
        raise RuntimeError(f"[{entity_code}] IBKR Flex: no ReferenceCode returned")

    # Step 2 — poll for the generated statement (IBKR may report "in progress").
    last_msg = ""
    for attempt in range(6):
        time.sleep(2 if attempt == 0 else 5)
        r = requests.get(
            get_url,
            params={"t": token, "q": reference_code, "v": _VERSION},
            timeout=60,
        )
        r.raise_for_status()
        stmt = ET.fromstring(r.content)

        # Ready statements are rooted at <FlexQueryResponse>. A still-generating
        # or errored response comes back as <FlexStatementResponse> with a Status.
        if stmt.tag == "FlexQueryResponse":
            logger.info(f"[{entity_code}] IBKR Flex statement retrieved (attempt {attempt + 1})")
            return stmt

        last_msg = f"{stmt.findtext('ErrorCode')} {stmt.findtext('ErrorMessage')}"
        logger.info(f"[{entity_code}] IBKR Flex not ready ({last_msg}); retrying")

    raise RuntimeError(f"[{entity_code}] IBKR Flex statement not ready after retries: {last_msg}")


# ---------------------------------------------------------------------------
# Holdings fetch
# ---------------------------------------------------------------------------

def fetch_holdings(entity_code: str) -> list[dict]:
    """
    Open positions from the Flex statement, as attribute dicts.

    Relevant OpenPosition attributes:
      symbol, isin, currency, position, costBasisPrice, markPrice,
      listingExchange, assetCategory, levelOfDetail
    """
    root = _fetch_statement_xml(entity_code)
    positions = [
        el.attrib
        for el in root.iter("OpenPosition")
        # SUMMARY rows aggregate the LOT rows — keep only SUMMARY to avoid
        # double-counting when the query is configured at lot level.
        if el.attrib.get("levelOfDetail", "SUMMARY") != "LOT"
    ]
    logger.info(f"[{entity_code}] IBKR: fetched {len(positions)} open positions")
    return positions


def fetch_trades(entity_code: str) -> list[dict]:
    """
    All executed trades from the *Trades* Flex query, as attribute dicts.

    Uses a SEPARATE saved query — IBKR_{CODE}_TRADES_QUERY_ID — that has the
    "Trades" section enabled (the daily QUERY_ID is positions/cash only). Used by
    the one-time inception backfill (equity/ibkr_backfill_inception.py).

    NOTE: the Flex Web Service caps a single statement at 365 days and does not
    accept date parameters — the range is whatever the saved query specifies. For
    an account older than a year, run the backfill once per year-window, changing
    the saved query's custom date range between runs; the ledger upsert dedups by
    `tradeID`, so re-imported trades are harmless.

    Relevant Trade attributes:
      symbol, isin, currency, tradeID, tradeDate (YYYYMMDD), buySell,
      quantity (signed: sells negative), tradePrice, ibCommission (negative),
      netCash (signed cash impact incl. commission)
    """
    query_id = _env(entity_code, "TRADES_QUERY_ID")
    root = _fetch_statement_xml(entity_code, query_id)
    trades = [el.attrib for el in root.iter("Trade")]
    logger.info(f"[{entity_code}] IBKR: fetched {len(trades)} trades")
    return trades


def fetch_cash_balance(entity_code: str) -> Decimal:
    """
    Ending cash from the Flex Cash Report, in the account base currency
    (BASE_SUMMARY row). Returns 0 if the query has no Cash Report section.
    """
    root = _fetch_statement_xml(entity_code)
    for el in root.iter("CashReportCurrency"):
        if el.attrib.get("currency") == "BASE_SUMMARY":
            try:
                return Decimal(str(el.attrib.get("endingCash") or "0"))
            except Exception:
                return Decimal("0")
    return Decimal("0")


# ---------------------------------------------------------------------------
# Normalise to EquityHolding (native currency; converted to INR later)
# ---------------------------------------------------------------------------

def normalise(entity_id: int, entity_code: str, raw: list[dict]) -> list[EquityHolding]:
    result = []
    for h in raw:
        try:
            qty = Decimal(str(h.get("position") or "0"))
            avg = Decimal(str(h.get("costBasisPrice") or "0"))
            ltp = Decimal(str(h.get("markPrice") or h.get("costBasisPrice") or "0"))
        except Exception:
            continue

        if qty <= 0:
            continue

        result.append(EquityHolding(
            entity_id            = entity_id,
            broker               = _LABEL,
            symbol               = h.get("symbol", "") or h.get("isin", ""),
            isin                 = h.get("isin", "") or "",
            exchange             = h.get("listingExchange", "") or "NASDAQ",
            quantity             = qty,
            avg_cost             = avg,
            cost                 = (qty * avg).quantize(Decimal("0.01")),
            current_price        = ltp,
            current_market_value = (qty * ltp).quantize(Decimal("0.01")),
            currency             = (h.get("currency") or CURRENCY).upper(),
        ))
    return result
