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


def _generate_token(entity_code: str, max_attempts: int = 3) -> str:
    """Generate a fresh token via PIN + TOTP (headless). Returns new token.

    Dhan intermittently rejects a TOTP that was generated near the end of its
    30-second window (the code expires in transit / on a small clock skew). A
    single rejection here used to freeze the entity's holdings for the whole
    day, so retry with a brand-new code taken just after the next window
    boundary. Attempt 1 uses the current code (fast path); retries wait for a
    fresh window so the resubmitted code has its full lifetime.
    """
    import time

    client_id  = _env(entity_code, "CLIENT_ID")
    api_key    = _env(entity_code, "API_KEY")
    api_secret = _env(entity_code, "API_SECRET")
    pin        = _env(entity_code, "PIN")
    totp_gen   = pyotp.TOTP(_env(entity_code, "TOTP_SECRET"))

    last_data = None
    last_totp = None
    for attempt in range(1, max_attempts + 1):
        # On a retry, always roll into the next window: a rejected code is spent,
        # and an immediate retry lands in the same window and resubmits it.
        if attempt > 1:
            secs_into_window = time.time() % 30
            time.sleep(30 - secs_into_window + 0.5)

        totp = totp_gen.now()
        if totp == last_totp:  # paranoia: never resubmit a rejected code
            time.sleep(30 - (time.time() % 30) + 0.5)
            totp = totp_gen.now()
        last_totp = totp
        # SDK's generate_token() omits app_id/app_secret headers — call directly.
        resp = requests.post(
            "https://auth.dhan.co/app/generateAccessToken",
            params={"dhanClientId": client_id, "pin": pin, "totp": totp},
            headers={"app_id": api_key, "app_secret": api_secret},
            timeout=20,
        )
        data = resp.json()
        last_data = data
        new_token = data.get("accessToken") or data.get("access_token") or data.get("token")
        if new_token:
            _save_token_to_env(entity_code, new_token)
            logger.info(f"[{entity_code}] Dhan token generated via PIN+TOTP (attempt {attempt})")
            return new_token

        logger.warning(
            f"[{entity_code}] Dhan generateAccessToken attempt "
            f"{attempt}/{max_attempts} rejected: {data}"
        )

    raise RuntimeError(
        f"generateAccessToken failed after {max_attempts} attempts: {last_data}"
    )


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
        remarks = resp.get("remarks")
        code = remarks.get("error_code") if isinstance(remarks, dict) else ""
        msg  = remarks.get("error_message", "") if isinstance(remarks, dict) else str(remarks)
        # DH-1111 "No holdings available" is how Dhan reports an empty *or* not-yet-
        # ready holdings snapshot (common pre-market). Treat it as empty rather than a
        # hard error: the caller's "no holdings → skip" guard then leaves the last good
        # holdings untouched instead of aborting the whole sync. Real errors still raise.
        if code == "DH-1111" or "no holdings" in str(msg).lower():
            logger.warning(
                f"[{entity_code}] Dhan: no holdings available (DH-1111) — "
                f"keeping existing holdings, not treating as a failure"
            )
            return []
        raise RuntimeError(f"[{entity_code}] Dhan holdings failed: {remarks}")

    holdings = resp.get("data") or []
    logger.info(f"[{entity_code}] Dhan: fetched {len(holdings)} holdings")
    return holdings


def _raise_on_failure(resp: dict, entity_code: str, what: str) -> dict:
    """Raise if Dhan reported a failure, so a broken call can't masquerade as no data.

    dhanhq returns {'status':'failure', 'remarks': {...}, 'data': ''} for every error —
    expired token, bad TOTP, HTTP 5xx, timeout. `'' or []` then collapses to an empty
    list that is indistinguishable from a genuine "nothing happened", so an auth
    failure would silently log as "fetched 0 trades" and leave a real gap in the
    ledger. Raising instead lets the caller's error handling and the staleness monitor
    see it. Callers that have a legitimate empty case (e.g. holdings' DH-1111) handle
    it before calling this.
    """
    if (resp or {}).get("status") == "failure":
        remarks = resp.get("remarks")
        raise RuntimeError(f"[{entity_code}] Dhan {what} failed: {remarks}")
    return resp or {}


def fetch_trades(entity_code: str, from_date: str, to_date: str) -> list[dict]:
    """Executed trades over a date range from Dhan trade history (paginated).
    Unlike Zerodha/Angel, Dhan exposes a date-ranged history, so gaps self-heal.
    Each item: tradingSymbol/customSymbol, securityId, isin, transactionType
    (BUY/SELL), tradedQuantity, tradedPrice, exchangeSegment, exchangeTime."""
    dhan = _dhan_client(entity_code)
    out, page = [], 0
    while True:
        resp = dhan.get_trade_history(from_date=from_date, to_date=to_date, page_number=page)
        data = _raise_on_failure(resp, entity_code, f"trade history {from_date}→{to_date}").get("data") or []
        if not data:
            break
        out.extend(data)
        page += 1
        if page > 50:                       # safety cap
            break
    logger.info(f"[{entity_code}] Dhan: fetched {len(out)} trades {from_date}→{to_date}")
    return out


def fetch_ledger(entity_code: str, from_date: str, to_date: str) -> list[dict]:
    """Cash ledger over a date range from Dhan (deposits/withdrawals/charges).
    Each item: date, voucherdate, narration/description, debit, credit, runbal."""
    dhan = _dhan_client(entity_code)
    resp = dhan.ledger_report(from_date=from_date, to_date=to_date)
    data = _raise_on_failure(resp, entity_code, f"ledger {from_date}→{to_date}").get("data") or []
    logger.info(f"[{entity_code}] Dhan: fetched {len(data)} ledger rows {from_date}→{to_date}")
    return data


def fetch_cash_balance(entity_code: str) -> Decimal:
    """
    Available cash for the Dhan account via the fund-limit endpoint.
    Uses `availabelBalance` (Dhan's spelling), falling back to
    `withdrawableBalance`, then 0.
    """
    dhan = _dhan_client(entity_code)
    resp = dhan.get_fund_limits() or {}
    if resp.get("status") == "failure":
        remarks = resp.get("remarks")
        code = remarks.get("error_code") if isinstance(remarks, dict) else ""
        msg  = remarks.get("error_message", "") if isinstance(remarks, dict) else str(remarks)
        # DH-906 "Incorrect request ... cannot be processed" is how Dhan's fund-limit
        # endpoint reports that funds aren't computed yet — routine before market open
        # (the cash-side twin of DH-1111 on holdings). The caller isolates the failure
        # and keeps the last-known cash; the 10:00 IST catch-up sync then refreshes it.
        # Raise with a clear, non-alarming message so it doesn't read as a hard failure.
        if code == "DH-906" or "cannot be processed" in str(msg).lower():
            raise RuntimeError(
                f"[{entity_code}] Dhan fund limits not ready (DH-906, common pre-market) "
                f"— keeping last-known cash"
            )
        raise RuntimeError(f"[{entity_code}] Dhan fund limits failed: {remarks}")
    data = resp.get("data") or {}

    raw = data.get("availabelBalance")
    if raw in (None, ""):
        raw = data.get("withdrawableBalance")
    try:
        bal = Decimal(str(raw)) if raw not in (None, "") else Decimal("0")
    except Exception:
        bal = Decimal("0")
    logger.info(f"[{entity_code}] Dhan: cash balance ₹{bal}")
    return bal


# ---------------------------------------------------------------------------
# Normalise to EquityHolding dataclass
# ---------------------------------------------------------------------------

def fetch_positions(entity_code: str) -> list[dict]:
    """Today's UNSETTLED delivery activity, normalised to the shared position shape.

    Dhan's holdings feed does not carry a same-day buy at all (verified live: a
    100 NCC buy was absent from `get_holdings` the same afternoon), so it sits here
    until it settles into `t1Qty` — which `totalQty` already includes — the next
    session. The carryForward quantities are what stop the two overlapping: a leg
    held from a previous session has them non-zero and belongs to the holdings feed.

    Restricted to CNC, and ISIN is resolved by the caller from the symbol.
    """
    resp = _dhan_client(entity_code).get_positions()
    rows = (resp or {}).get("data") or []
    out  = []
    for p in rows:
        if (p.get("productType") or "").upper() != "CNC":
            continue
        qty = int(p.get("netQty") or 0)
        cf  = int(p.get("carryForwardBuyQty") or 0) + int(p.get("carryForwardSellQty") or 0)
        if qty == 0 or cf != 0:
            continue
        out.append({
            "symbol":   p.get("tradingSymbol"),
            "isin":     None,
            "quantity": Decimal(str(qty)),
            "avg_cost": Decimal(str(p.get("costPrice") or p.get("buyAvg") or 0)),
            "ltp":      None,          # Dhan positions carry no LTP — priced from the holding
            "exchange": "NSE" if str(p.get("exchangeSegment", "")).startswith("NSE") else "BSE",
        })
    logger.info(f"[{entity_code}] Dhan: fetched {len(out)} unsettled position(s)")
    return out


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
