"""
Angel One SmartAPI broker wrapper.

Entities : DHR, HHR
Auth     : Pure HTTP — SmartAPI generateSession endpoint with pyotp TOTP.
           No browser needed. Token expires daily.
           Refreshed automatically by token_refresh_worker.py at 6:30 AM IST.

SmartAPI app setup (one-time per entity):
  1. Register at smartapi.angelone.in → Create app → get API Key (free)
  2. Enable TOTP on the Angel One account and save the base32 secret key

Env vars required (per entity, replace {CODE} with DHR / HHR):
  ANGEL_{CODE}_API_KEY
  ANGEL_{CODE}_CLIENT_ID      — Angel One client ID / login ID
  ANGEL_{CODE}_PASSWORD       — Angel One login PIN (4-digit MPIN usually)
  ANGEL_{CODE}_TOTP_SECRET    — base32 key from TOTP setup, NOT the 6-digit pin
"""
import os
import logging
from decimal import Decimal

import pyotp
from SmartApi import SmartConnect

from equity import tokens
from equity.models import EquityHolding

logger = logging.getLogger(__name__)

SUPPORTED_ENTITIES = ["DHR", "HHR"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _env(entity_code: str, key: str) -> str:
    return os.environ[f"ANGEL_{entity_code}_{key}"]


def _smart_client(entity_code: str) -> SmartConnect:
    api_key      = _env(entity_code, "API_KEY")
    # tokens.json is the authoritative, daily-refreshed store (written by
    # token_refresh_worker and by refresh_access_token below). Read it FIRST so a
    # stale value pinned in .env can never mask the fresh token — that mismatch is
    # exactly what froze Angel cash/holdings with AB1007 for a full day. The env
    # var remains only as a fallback for first-run before any refresh has happened.
    access_token = (
        tokens.get(f"angel_one_{entity_code}")
        or os.environ.get(f"ANGEL_{entity_code}_ACCESS_TOKEN")
    )
    if not access_token:
        raise RuntimeError(
            f"No Angel One access token for {entity_code}. "
            "Run token_refresh_worker.py first."
        )
    obj = SmartConnect(api_key=api_key)
    obj.setAccessToken(access_token)
    return obj


# ---------------------------------------------------------------------------
# Token refresh (called by token_refresh_worker.py at 6:30 AM IST)
# ---------------------------------------------------------------------------

def refresh_access_token(entity_code: str) -> str:
    """
    Calls SmartAPI generateSession with live TOTP — no browser needed.
    Persists the new JWT to equity_tokens.json.
    """
    api_key     = _env(entity_code, "API_KEY")
    client_id   = _env(entity_code, "CLIENT_ID")
    password    = _env(entity_code, "PASSWORD")
    totp_secret = _env(entity_code, "TOTP_SECRET")

    totp_code = pyotp.TOTP(totp_secret).now()
    obj       = SmartConnect(api_key=api_key)

    logger.info(f"[{entity_code}] Angel One: calling generateSession")
    resp = obj.generateSession(client_id, password, totp_code)

    if not resp.get("status"):
        raise RuntimeError(
            f"[{entity_code}] Angel One generateSession failed: {resp.get('message')}"
        )

    access_token  = resp["data"]["jwtToken"].removeprefix("Bearer ")
    refresh_token = resp["data"].get("refreshToken", "")

    tokens.save(f"angel_one_{entity_code}", access_token)
    # Keep the live process env in sync too, so the .env fallback in _smart_client
    # never serves a stale token after a refresh (mirrors the Dhan token flow).
    os.environ[f"ANGEL_{entity_code}_ACCESS_TOKEN"] = access_token
    # Store refresh token separately in case we want to use it for mid-day refresh
    if refresh_token:
        tokens.save(f"angel_one_{entity_code}_refresh", refresh_token)

    logger.info(f"[{entity_code}] Angel One access token refreshed and saved")
    return access_token


# ---------------------------------------------------------------------------
# Intraday self-heal
# ---------------------------------------------------------------------------

def _call_with_reauth(entity_code: str, fn):
    """Run fn(); on any failure refresh the access token once and retry.

    Angel One allows a single active API session, so the daily JWT dies the moment
    the user (or another app) logs in — every call then returns AB1007 'Invalid
    Token' until the next 6:30 AM refresh. Re-running generateSession mints a fresh
    JWT and makes ours the active session again, so cash/holdings self-heal mid-day
    instead of freezing. If the refresh itself fails (e.g. a network blip), the
    second attempt raises and the caller keeps the last known value — same as before.
    """
    try:
        return fn()
    except Exception as e:
        logger.warning(
            f"[{entity_code}] Angel One call failed ({e}); "
            f"refreshing token and retrying once"
        )
        refresh_access_token(entity_code)
        return fn()


# ---------------------------------------------------------------------------
# Holdings fetch
# ---------------------------------------------------------------------------

def fetch_holdings(entity_code: str) -> list[dict]:
    return _call_with_reauth(entity_code, lambda: _fetch_holdings(entity_code))


def _fetch_holdings(entity_code: str) -> list[dict]:
    """
    Raw holdings from SmartAPI.

    Relevant fields per item:
      tradingsymbol, isin, exchange, quantity, averageprice,
      ltp, profitandloss, pnlpercentage, symboltoken
    """
    obj  = _smart_client(entity_code)
    resp = obj.holding()

    if not resp.get("status"):
        raise RuntimeError(
            f"[{entity_code}] Angel One holdings failed: {resp.get('message')}"
        )

    holdings = resp.get("data") or []
    logger.info(f"[{entity_code}] Angel One: fetched {len(holdings)} holdings")
    return holdings


def fetch_trades(entity_code: str) -> list[dict]:
    return _call_with_reauth(entity_code, lambda: _fetch_trades(entity_code))


def _fetch_trades(entity_code: str) -> list[dict]:
    """Today's executed trades from SmartAPI tradeBook (current day only).
    Each item: tradingsymbol, transactiontype(BUY/SELL), fillsize, fillprice,
    tradeid, exchange, filltime, symboltoken."""
    obj  = _smart_client(entity_code)
    resp = obj.tradeBook() or {}
    if not resp.get("status"):
        # No trades today returns status True + empty data; a real failure has a message.
        msg = resp.get("message") or ""
        if "no data" not in msg.lower() and resp.get("data") is None and msg:
            raise RuntimeError(f"[{entity_code}] Angel One tradeBook failed: {msg}")
    trades = resp.get("data") or []
    logger.info(f"[{entity_code}] Angel One: fetched {len(trades)} trades")
    return trades


def fetch_cash_balance(entity_code: str) -> Decimal:
    return _call_with_reauth(entity_code, lambda: _fetch_cash_balance(entity_code))


def _fetch_cash_balance(entity_code: str) -> Decimal:
    """
    Available cash for the Angel One account via SmartAPI RMS limits.
    Uses `availablecash` (the free cash), falling back to `net`, then 0.
    """
    obj  = _smart_client(entity_code)
    resp = obj.rmsLimit() or {}
    if not resp.get("status"):
        # e.g. Invalid Token — raise so the caller keeps the last known balance
        # instead of overwriting it with a misleading 0.
        raise RuntimeError(f"[{entity_code}] Angel One RMS failed: {resp.get('message')}")
    data = resp.get("data") or {}

    raw = data.get("availablecash")
    if raw in (None, ""):
        raw = data.get("net")
    try:
        bal = Decimal(str(raw)) if raw not in (None, "") else Decimal("0")
    except Exception:
        bal = Decimal("0")
    logger.info(f"[{entity_code}] Angel One: cash balance ₹{bal}")
    return bal


# ---------------------------------------------------------------------------
# Normalise to EquityHolding dataclass
# ---------------------------------------------------------------------------

def normalise(entity_id: int, entity_code: str, raw: list[dict]) -> list[EquityHolding]:
    """
    Convert SmartAPI holding dicts to EquityHolding objects.
    Metrics (pnl, returns, exposure) are computed later in equity_sync_worker.
    """
    result = []
    for h in raw:
        qty = Decimal(str(h["quantity"]))
        avg = Decimal(str(h["averageprice"]))
        ltp = Decimal(str(h["ltp"]))

        if qty <= 0:
            continue

        result.append(EquityHolding(
            entity_id            = entity_id,
            broker               = "angel_one",
            symbol               = h["tradingsymbol"] or h.get("isin", ""),
            isin                 = h.get("isin", ""),
            exchange             = h.get("exchange", "NSE"),
            quantity             = qty,
            avg_cost             = avg,
            cost                 = (qty * avg).quantize(Decimal("0.01")),
            current_price        = ltp,
            current_market_value = (qty * ltp).quantize(Decimal("0.01")),
            angel_one_token      = str(h.get("symboltoken", "") or ""),
        ))

    return result
