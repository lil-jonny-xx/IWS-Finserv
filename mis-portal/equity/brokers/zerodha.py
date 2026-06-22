"""
Zerodha Kite Connect broker wrapper.

Entities : DHR, HHR, SDR, Rajani Corp  (separate Kite Connect app per account, ₹500/month each)
Auth     : Playwright login (headless) + pyotp TOTP → request_token
           → kite.generate_session() → access_token (expires 6 AM IST daily)
           Refreshed automatically by token_refresh_worker.py at 6:30 AM IST.

Kite app setup (one-time per entity):
  1. Create app at kite.trade/developers
  2. Set Redirect URL to: http://127.0.0.1
  3. Note down API Key and API Secret
  4. Enable TOTP on the Zerodha account and save the base32 secret key

Env vars required (per entity, replace {CODE} with DHR / HHR / SDR):
  ZERODHA_{CODE}_API_KEY
  ZERODHA_{CODE}_API_SECRET
  ZERODHA_{CODE}_CLIENT_ID      — Zerodha user ID e.g. ZX1234
  ZERODHA_{CODE}_PASSWORD
  ZERODHA_{CODE}_TOTP_SECRET    — base32 key from TOTP setup, NOT the 6-digit pin
"""
import os
import logging
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

import pyotp
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright

from equity import tokens
from equity.models import EquityHolding

logger = logging.getLogger(__name__)

SUPPORTED_ENTITIES = ["DHR", "HHR", "SDR", "Rajani Corp"]

# Maps entity_name (DB) → env var prefix when they differ
_ENV_PREFIX = {
    "Rajani Corp": "RAJANIRCORP",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _env(entity_code: str, key: str) -> str:
    prefix = _ENV_PREFIX.get(entity_code, entity_code)
    return os.environ[f"ZERODHA_{prefix}_{key}"]


def _kite_client(entity_code: str) -> KiteConnect:
    api_key      = _env(entity_code, "API_KEY")
    access_token = (
        os.environ.get(f"ZERODHA_{entity_code}_ACCESS_TOKEN")
        or tokens.get(f"zerodha_{entity_code}")
    )
    if not access_token:
        raise RuntimeError(
            f"No Zerodha access token for {entity_code}. "
            "Run token_refresh_worker.py first."
        )
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


# ---------------------------------------------------------------------------
# Token refresh (called by token_refresh_worker.py at 6:30 AM IST)
# ---------------------------------------------------------------------------

def refresh_access_token(entity_code: str) -> str:
    """
    Headless Playwright login to Kite → exchanges request_token for access_token.
    Persists the new token to equity_tokens.json.
    """
    api_key     = _env(entity_code, "API_KEY")
    api_secret  = _env(entity_code, "API_SECRET")
    client_id   = _env(entity_code, "CLIENT_ID")
    password    = _env(entity_code, "PASSWORD")
    totp_secret = _env(entity_code, "TOTP_SECRET")

    kite      = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    logger.info(f"[{entity_code}] Zerodha: starting headless login")

    redirect_url = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()

        # Capture the redirect to http://127.0.0.1?request_token=... via request event.
        # Chrome can't load the URL (no server there) but the request fires before the error.
        def on_request(req):
            nonlocal redirect_url
            if "request_token" in req.url:
                redirect_url = req.url

        page.on("request", on_request)

        page.goto(login_url)

        # Step 1 — user ID + password
        page.wait_for_selector("input#userid", timeout=10_000)
        page.fill("input#userid", client_id)
        page.fill("input#password", password)
        page.click("button[type='submit']")

        # Step 2 — TOTP screen (Zerodha reuses id=userid with type=number for the TOTP field)
        page.wait_for_selector("input#userid[type='number']", timeout=10_000)
        totp_code = pyotp.TOTP(totp_secret).now()
        page.fill("input#userid[type='number']", totp_code)

        # Step 3 — Authorize screen (Zerodha added a permissions consent step)
        # Click "Authorize" if it appears; redirect fires after this.
        try:
            page.wait_for_selector("button:has-text('Authorize')", timeout=10_000)
            page.click("button:has-text('Authorize')")
        except Exception:
            pass  # Screen may not appear on all accounts

        page.wait_for_timeout(8_000)
        browser.close()

    if not redirect_url or "request_token" not in redirect_url:
        raise RuntimeError(
            f"[{entity_code}] Zerodha login failed: request_token not captured. "
            f"Got: {redirect_url}"
        )

    # Extract request_token from redirect URL query string
    params        = parse_qs(urlparse(redirect_url).query)
    request_token = params.get("request_token", [None])[0]
    if not request_token:
        raise RuntimeError(
            f"[{entity_code}] request_token not found in redirect URL: {redirect_url}"
        )

    # Exchange request_token for access_token
    session      = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]

    tokens.save(f"zerodha_{entity_code}", access_token)
    logger.info(f"[{entity_code}] Zerodha access token refreshed and saved")
    return access_token


# ---------------------------------------------------------------------------
# Holdings fetch
# ---------------------------------------------------------------------------

def fetch_holdings(entity_code: str) -> list[dict]:
    """
    Raw holdings from Kite Connect.

    Relevant fields per item:
      tradingsymbol, isin, exchange, quantity, t1_quantity,
      average_price, last_price, pnl, day_change, day_change_percentage
    """
    kite     = _kite_client(entity_code)
    holdings = kite.holdings()
    logger.info(f"[{entity_code}] Zerodha: fetched {len(holdings)} holdings")
    return holdings


def fetch_cash_balance(entity_code: str) -> Decimal:
    """
    Available cash in the Zerodha equity segment, from Kite Connect margins.

    Uses the segment `net` (the free/available balance Kite shows on the funds
    screen). Falls back to available.live_balance, then 0. For a Zerodha-run PMS
    this is the un-invested cash sitting in the account.
    """
    kite = _kite_client(entity_code)
    m    = kite.margins("equity") or {}

    net = m.get("net")
    if net is None:
        net = (m.get("available") or {}).get("live_balance")
    bal = Decimal(str(net or 0))
    logger.info(f"[{entity_code}] Zerodha: equity cash balance ₹{bal}")
    return bal


# ---------------------------------------------------------------------------
# Normalise to EquityHolding dataclass
# ---------------------------------------------------------------------------

def normalise(entity_id: int, entity_code: str, raw: list[dict]) -> list[EquityHolding]:
    """
    Convert Kite Connect holding dicts to EquityHolding objects.
    Metrics (pnl, returns, exposure) are computed later in equity_sync_worker.
    """
    result = []
    for h in raw:
        qty = Decimal(str(h["quantity"]))
        avg = Decimal(str(h["average_price"]))
        ltp = Decimal(str(h["last_price"]))

        # Skip zero-quantity holdings (demat positions fully sold but not settled)
        if qty <= 0:
            continue

        result.append(EquityHolding(
            entity_id            = entity_id,
            broker               = "zerodha",
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
