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
    access_token = (
        os.environ.get(f"ANGEL_{entity_code}_ACCESS_TOKEN")
        or tokens.get(f"angel_one_{entity_code}")
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

    access_token  = resp["data"]["jwtToken"]
    refresh_token = resp["data"].get("refreshToken", "")

    tokens.save(f"angel_one_{entity_code}", access_token)
    # Store refresh token separately in case we want to use it for mid-day refresh
    if refresh_token:
        tokens.save(f"angel_one_{entity_code}_refresh", refresh_token)

    logger.info(f"[{entity_code}] Angel One access token refreshed and saved")
    return access_token


# ---------------------------------------------------------------------------
# Holdings fetch
# ---------------------------------------------------------------------------

def fetch_holdings(entity_code: str) -> list[dict]:
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
            symbol               = h["tradingsymbol"],
            isin                 = h.get("isin", ""),
            exchange             = h.get("exchange", "NSE"),
            quantity             = qty,
            avg_cost             = avg,
            cost                 = (qty * avg).quantize(Decimal("0.01")),
            current_price        = ltp,
            current_market_value = (qty * ltp).quantize(Decimal("0.01")),
        ))

    return result
