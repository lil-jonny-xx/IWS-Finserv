"""
Dhan broker wrapper.

Entities : HHR, Rajani Corp
Auth     : 24-hour access token, auto-renewed daily via /RenewToken.
           If renewal fails, falls back to TOTP-based headless generation.

Env vars required:
  DHAN_HHR_CLIENT_ID
  DHAN_HHR_ACCESS_TOKEN     — current token (updated in-place after renewal)
  DHAN_HHR_API_KEY          — app_id from dhanhq.co/developers
  DHAN_HHR_API_SECRET       — app_secret from dhanhq.co/developers
  DHAN_HHR_PIN              — Dhan login PIN (for headless TOTP generation)
  DHAN_HHR_TOTP_SECRET      — base32 TOTP secret (for headless generation)
"""
import os
import re
import logging
from decimal import Decimal
from pathlib import Path

import pyotp
import requests
from dhanhq import dhanhq
from dhanhq.dhan_context import DhanContext
from dhanhq.auth import DhanLogin

from equity.models import EquityHolding

logger = logging.getLogger(__name__)

SUPPORTED_ENTITIES = ["HHR", "Rajani Corp"]
ENV_FILE = Path("/var/www/mis-portal/.env")

# Maps entity_name (DB) → env var prefix when they differ
_ENV_PREFIX = {
    "Rajani Corp": "RAJANIRCORP",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _env(entity_code: str, key: str, required: bool = True) -> str:
    prefix = _ENV_PREFIX.get(entity_code, entity_code)
    val = os.environ.get(f"DHAN_{prefix}_{key}", "")
    if required and not val:
        raise KeyError(f"DHAN_{prefix}_{key} not set in .env")
    return val


def _save_token_to_env(entity_code: str, new_token: str):
    """Overwrite the access token in .env in-place."""
    prefix = _ENV_PREFIX.get(entity_code, entity_code)
    key = f"DHAN_{prefix}_ACCESS_TOKEN"
    try:
        text = ENV_FILE.read_text()
        updated = re.sub(
            rf"^({re.escape(key)}=).*$",
            rf"\g<1>{new_token}",
            text,
            flags=re.MULTILINE,
        )
        ENV_FILE.write_text(updated)
        os.environ[key] = new_token
        logger.info(f"[{entity_code}] Dhan access token saved to .env")
    except Exception as e:
        logger.warning(f"[{entity_code}] Could not save Dhan token to .env: {e}")


def _renew_token(entity_code: str) -> str:
    """Try to renew the current token via /RenewToken. Returns new token."""
    client_id    = _env(entity_code, "CLIENT_ID")
    access_token = _env(entity_code, "ACCESS_TOKEN")
    login = DhanLogin(client_id)
    resp  = login.renew_token(access_token)
    new_token = resp.get("accessToken") or resp.get("access_token") or resp.get("token")
    if not new_token:
        raise RuntimeError(f"RenewToken response missing token: {resp}")
    _save_token_to_env(entity_code, new_token)
    logger.info(f"[{entity_code}] Dhan token renewed via /RenewToken")
    return new_token


def _generate_token(entity_code: str) -> str:
    """Generate a fresh token via PIN + TOTP (headless). Returns new token."""
    client_id  = _env(entity_code, "CLIENT_ID")
    api_key    = _env(entity_code, "API_KEY")
    api_secret = _env(entity_code, "API_SECRET")
    pin        = _env(entity_code, "PIN")
    totp       = pyotp.TOTP(_env(entity_code, "TOTP_SECRET")).now()
    # SDK's generate_token() omits app_id/app_secret headers — call directly.
    resp = requests.post(
        "https://auth.dhan.co/app/generateAccessToken",
        params={"dhanClientId": client_id, "pin": pin, "totp": totp},
        headers={"app_id": api_key, "app_secret": api_secret},
    )
    data = resp.json()
    new_token = data.get("accessToken") or data.get("access_token") or data.get("token")
    if not new_token:
        raise RuntimeError(f"generateAccessToken response missing token: {data}")
    _save_token_to_env(entity_code, new_token)
    logger.info(f"[{entity_code}] Dhan token generated via PIN+TOTP")
    return new_token


def refresh_access_token(entity_code: str) -> str:
    """
    Renew token if possible; fall back to headless generation.
    Called at the start of each equity sync.
    """
    # Try renewal first (works when current token is still valid)
    try:
        return _renew_token(entity_code)
    except Exception as e:
        logger.warning(f"[{entity_code}] Dhan renewal failed ({e}), trying PIN+TOTP generation")

    # Fall back to TOTP-based generation
    pin    = _env(entity_code, "PIN",         required=False)
    secret = _env(entity_code, "TOTP_SECRET", required=False)
    if pin and secret:
        return _generate_token(entity_code)

    raise RuntimeError(
        f"[{entity_code}] Cannot obtain Dhan token: renewal failed and "
        "DHAN_{entity_code}_PIN / DHAN_{entity_code}_TOTP_SECRET not set. "
        "Generate a token manually from web.dhan.co and update .env."
    )


def _dhan_client(entity_code: str) -> dhanhq:
    client_id    = _env(entity_code, "CLIENT_ID")
    access_token = _env(entity_code, "ACCESS_TOKEN")
    return dhanhq(DhanContext(client_id, access_token))


# ---------------------------------------------------------------------------
# Holdings fetch
# ---------------------------------------------------------------------------

def fetch_holdings(entity_code: str) -> list[dict]:
    """
    Raw holdings from Dhan. Auto-renews token before fetching.

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
