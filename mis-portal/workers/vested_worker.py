#!/usr/bin/env python3
"""
Vested Worker — IWS MIS Portal.

Vested (US equities for Indian investors) is a DriveWealth-backed SPA with no
retail/investor API, so — exactly like the Nuvama PMS worker — we drive a
browser: log in, read the current holdings, and read the transaction history.
Only HHR holds a Vested account.

Two data products per run:
  * HOLDINGS — the current snapshot from the "US Stocks and ETFs" page
    (downloadable XLSX/PDF; we also capture the JSON the page loads). This
    populates quantity / avg cost / current value (→ equity_holding via the
    `vested` adapter + equity sync, which converts USD→INR).
  * TRANSACTIONS — from the profile menu → Transactions, with a date filter.
    These are NOT stored in a table; they feed a per-symbol cash-flow ledger
    kept in the feed cache from which we derive each holding's
    first_invested_date (since-held → CAGR) and money-weighted XIRR (native USD).
    First run pulls the FULL history; every run after pulls just the previous
    business day and appends it (the daily cron fires just before US open).

Login is two-step: email + password, then a PIN page, then the dashboard.

Credentials (.env):
  VESTED_HHR_USERNAME   — login email
  VESTED_HHR_PASSWORD   — login password
  VESTED_HHR_PIN        — the numeric PIN for the second step
Optional:
  VESTED_BASE_URL, VESTED_LOGIN_URL, VESTED_RANDOMIZE=0,
  VESTED_HOLDINGS_SCHEMA_READY=1   (once the XLSX column mapping is finalised)

NOTE: Vested's exact selectors / JSON field names / XLSX layout can only be
confirmed against a live login; everything is best-effort with fallbacks and
screenshots at each step (_SHOTS_DIR) for first-run tuning, mirroring Nuvama.

Run:  /var/www/.venv/bin/python workers/vested_worker.py
Cron: via workers/cron_wrapper.py, daily shortly before US market open.
"""
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

import _portal_scraper as ps          # noqa: E402
from equity.brokers import _feed       # noqa: E402

ps.basic_logging()
logger = logging.getLogger("vested_worker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL  = os.environ.get("VESTED_BASE_URL", "https://app.vestedfinance.com")
LOGIN_URL = os.environ.get("VESTED_LOGIN_URL", f"{BASE_URL.rstrip('/')}/login")
TXN_URL   = os.environ.get("VESTED_TXN_URL",
                           f"{BASE_URL.rstrip('/')}/en/global/transaction-history")
# Backend API the transaction-history page calls (separate host → needs the
# page's Authorization header replayed for pagination).
TXN_API   = os.environ.get(
    "VESTED_TXN_API",
    "https://vested-api-prod-ga.vestedfinance.com/transaction-history/user-transaction")
CURRENCY  = "USD"
BROKER    = "vested"
_SHOTS_DIR = "/home/SAdmin/.vested-screenshots"

HOLDINGS_SCHEMA_READY = os.environ.get(
    "VESTED_HOLDINGS_SCHEMA_READY", "0").strip().lower() in ("1", "true", "yes")

# Only HHR holds a Vested account: (entity_code, env_prefix). Processed only if
# its USERNAME is set in .env.
ENTITIES = [("HHR", "HHR")]

RANDOMIZE      = os.environ.get("VESTED_RANDOMIZE", "1").strip().lower() in ("1", "true", "yes")
START_JITTER_S = int(os.getenv("VESTED_START_JITTER_S", str(5 * 60)))

CFG = ps.PortalCfg(
    name="vested",
    base_url=BASE_URL,
    download_dir=Path("/var/www/mis-portal/.vested_downloads"),
    profile_dir=Path("/var/www/mis-portal/.vested_browser_profile"),
    session_dir=Path("/var/www/mis-portal/.vested_sessions"),
    shots_dir=_SHOTS_DIR,
)


class VestedSchemaUnknown(Exception):
    """Raised when the downloaded holdings XLSX layout has not been mapped yet."""


@dataclass
class VestedConfig:
    code:     str
    prefix:   str
    username: str
    password: str
    pin:      str


def _env(prefix: str, key: str, required: bool = False) -> str:
    val = os.environ.get(f"VESTED_{prefix}_{key}", "")
    if required and not val:
        raise KeyError(f"VESTED_{prefix}_{key} not set in .env")
    return val


def load_configs() -> list[VestedConfig]:
    configs = []
    for code, prefix in ENTITIES:
        if not _env(prefix, "USERNAME"):
            continue
        configs.append(VestedConfig(
            code=code, prefix=prefix,
            username=_env(prefix, "USERNAME", required=True),
            password=_env(prefix, "PASSWORD", required=True),
            pin=_env(prefix, "PIN"),
        ))
    return configs


# ---------------------------------------------------------------------------
# Login — email + password, then PIN, then dashboard
# ---------------------------------------------------------------------------
def login(page, cfg: VestedConfig):
    logger.info(f"[{cfg.code}] logging in to Vested")
    page.goto(LOGIN_URL, timeout=ps.NAV_TIMEOUT, wait_until="domcontentloaded")
    _wait_for_entry(page)
    ps.shot(page, CFG, f"vested_login_{cfg.prefix}.png")

    # A live persistent session redirects /login straight to the app home — no
    # email/PIN form at all. Detect that and skip the login flow entirely.
    if _is_logged_in(page):
        logger.info(f"[{cfg.code}] already authenticated (persistent session)")
        return

    # A persistent profile often keeps the email/password session alive and drops
    # us straight on the 6-digit PIN page — skip the credential form in that case.
    if _pin_prompt_present(page):
        logger.info(f"[{cfg.code}] remembered session — straight to PIN page")
    else:
        # Vested's login page first offers three sign-in methods (email, Google,
        # phone). Choose "email" to reveal the email + password form on the next
        # step. optional=True in case Vested ever lands directly on the form.
        ps.click(page, [
            'button:has-text("Continue with email")', 'button:has-text("Sign in with email")',
            'button:has-text("Log in with email")', 'button:has-text("Login with email")',
            'a:has-text("Continue with email")', 'a:has-text("Log in with email")',
            'button:has-text("Email")', 'a:has-text("Email")',
            '[data-testid*="email" i]',
        ], "email login method", optional=True)
        page.wait_for_timeout(random.randint(900, 1600))
        ps.shot(page, CFG, f"vested_login_email_{cfg.prefix}.png")

        ps.fill(page, [
            'input[name="email"]', 'input[type="email"]', 'input[name="username"]',
            'input[id*="email" i]', 'input[placeholder*="email" i]', 'input[type="text"]',
        ], cfg.username, "email")
        ps.fill(page, [
            'input[name="password"]', 'input[type="password"]',
            'input[id*="pass" i]', 'input[placeholder*="password" i]',
        ], cfg.password, "password")
        page.wait_for_timeout(random.randint(300, 700))
        ps.click(page, [
            'button[type="submit"]', 'button:has-text("Log In")', 'button:has-text("Login")',
            'button:has-text("Continue")', 'button:has-text("Sign In")', 'input[type="submit"]',
        ], "login button")

        try:
            page.wait_for_load_state("domcontentloaded", timeout=ps.ACTION_TIMEOUT)
        except Exception:
            pass

        # After the password submit Vested shows a loading spinner, THEN the
        # 6-digit PIN page. Wait for the PIN field to actually render before
        # deciding — a fixed sleep raced past it and made us skip the PIN.
        _wait_for_pin_page(page)

    ps.shot(page, CFG, f"vested_pin_{cfg.prefix}.png")

    if ps.has_text(page, "invalid", "incorrect", "wrong password", "try again") \
            and "login" in page.url.lower():
        raise RuntimeError(f"[{cfg.code}] email/password rejected — check credentials")

    # --- PIN step ---------------------------------------------------------
    _enter_pin(page, cfg)

    page.wait_for_timeout(3000)
    ps.shot(page, CFG, f"vested_dashboard_{cfg.prefix}.png")

    # If the PIN field is still here, the PIN didn't go through.
    if _pin_prompt_present(page):
        raise RuntimeError(f"[{cfg.code}] still on the PIN page after entry — check "
                           f"VESTED_{cfg.prefix}_PIN")

    # A genuine email/SMS OTP challenge (distinct from the app PIN above). Match
    # specific phrases, not the bare substring "otp" — that also appears in the
    # PIN page's "Login with OTP" alternative link and caused a false abort.
    if ps.has_text(page, "verification code", "one-time password", "otp sent",
                   "enter the otp", "code sent to", "verify your identity"):
        raise RuntimeError(f"[{cfg.code}] Vested asked for an email/SMS OTP — needs an "
                           f"interactive step; cannot complete unattended")


_PIN_SELECTOR = ('input[name="pin"], input[id*="pin" i], input[placeholder*="pin" i], '
                 'input[maxlength="1"], input[autocomplete="one-time-code"]')

_EMAIL_FIELD   = ('input[type="email"], input[name="email"], '
                  'input[placeholder*="email" i]')
_EMAIL_METHOD  = ('button:has-text("Email"), a:has-text("Email"), '
                  'button:has-text("Continue with email"), [data-testid*="email" i]')


def _wait_for_entry(page, timeout_ms: int = 25000):
    """Wait for the page to settle into a recognisable state: a login control
    (method chooser / email form / PIN page) OR — when a live persistent session
    redirects /login to the app — the authenticated home. Lets login() branch
    correctly instead of racing a spinner or waiting out the full timeout.
    """
    try:
        page.wait_for_function(
            """() => {
                const t = (document.body && document.body.innerText || '').toLowerCase();
                const hasLogin = !!document.querySelector(
                    'input[type=email],input[name=email],input[name=pin],'
                    + 'input[id*=pin i],input[placeholder*=pin i]');
                const hasMethod = Array.from(document.querySelectorAll('button,a'))
                    .some(e => /email/i.test(e.innerText || ''));
                const hasApp = /us stocks|global mutual funds|private markets|buying power/.test(t);
                return hasLogin || hasMethod || hasApp;
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        page.wait_for_timeout(2500)


def _is_logged_in(page) -> bool:
    """True if a live persistent session put us on the authenticated app home.

    Vested is a SPA that renders the dashboard while the URL stays /login, so we
    detect by content: no login control present AND dashboard markers present.
    Needles avoid '&' (page.content() HTML-encodes it as '&amp;').
    """
    try:
        if page.locator('input[type=email], input[name=email], '
                        + _PIN_SELECTOR).count() > 0:
            return False
    except Exception:
        pass
    return ps.has_text(page, "global mutual funds", "private markets",
                       "view portfolio performance", "buying power",
                       "unrealized p", "us stocks")


def _wait_for_pin_page(page, timeout_ms: int = 25000):
    """Wait for the post-password page to settle on the PIN prompt.

    Vested renders a spinner first, so we poll for the PIN field. If it never
    appears (e.g. a remembered device skips straight to the app), fall back to a
    short settle so the caller can detect whatever did load.
    """
    try:
        page.wait_for_selector(_PIN_SELECTOR, timeout=timeout_ms, state="visible")
    except Exception:
        page.wait_for_timeout(2500)


def _enter_pin(page, cfg: VestedConfig):
    """Fill the PIN page. Vested may render one PIN field or N single-digit boxes."""
    if not _pin_prompt_present(page):
        logger.info(f"[{cfg.code}] no PIN prompt detected — continuing")
        return
    if not cfg.pin:
        raise RuntimeError(f"[{cfg.code}] PIN prompt shown but VESTED_{cfg.prefix}_PIN is not set")

    # Single combined PIN field.
    for sel in ('input[name="pin"]', 'input[id*="pin" i]',
                'input[placeholder*="PIN" i]', 'input[autocomplete="one-time-code"]'):
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                loc.click(force=True)
                loc.type(cfg.pin, delay=random.randint(60, 130))
                _submit_pin(page)
                return
        except Exception:
            continue

    # N single-digit boxes — type one digit per input.
    boxes = page.locator(
        'input[maxlength="1"], input[inputmode="numeric"]:visible, '
        'input[type="tel"]:visible, input[type="password"]:visible'
    )
    try:
        n = boxes.count()
    except Exception:
        n = 0
    if n >= len(cfg.pin):
        for i, digit in enumerate(cfg.pin):
            try:
                boxes.nth(i).click(force=True)
                boxes.nth(i).type(digit, delay=random.randint(60, 130))
            except Exception:
                page.keyboard.type(digit, delay=90)
        _submit_pin(page)
        return

    # Last resort — type into whatever is focused.
    page.keyboard.type(cfg.pin, delay=90)
    _submit_pin(page)


def _pin_prompt_present(page) -> bool:
    if ps.has_text(page, "enter your pin", "enter pin", "your pin",
                   "digit pin", "4-digit", "6-digit", "access vested"):
        return True
    try:
        loc = page.locator(_PIN_SELECTOR).first
        return loc.count() > 0 and loc.is_visible()
    except Exception:
        return False


def _submit_pin(page):
    page.wait_for_timeout(300)
    ps.click(page, [
        'button[type="submit"]', 'button:has-text("Verify")', 'button:has-text("Continue")',
        'button:has-text("Submit")', 'button:has-text("Login")',
    ], "PIN submit", optional=True)


def logout(page):
    try:
        ps.click(page, ['[data-toggle="dropdown"]', '.avatar', '.profile', '.user-menu',
                        'button[aria-label*="profile" i]'], "account menu", optional=True)
        page.wait_for_timeout(400)
        ps.click(page, ['a:has-text("Log Out")', 'a:has-text("Logout")', 'a:has-text("Sign Out")',
                        '[href*="logout" i]'], "logout", optional=True)
        page.wait_for_timeout(1000)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Holdings — US Stocks and ETFs page (JSON capture primary; XLSX + DOM backup)
# ---------------------------------------------------------------------------
_POS_NAME_KEYS = ("symbol", "ticker", "instrumentsymbol", "securitysymbol")
_POS_QTY_KEYS  = ("quantity", "units", "shares", "openqty", "qty")
# NB: per-share average cost only. Vested's "costBasis" is the TOTAL invested
# (qty × avg), so it must NOT be a candidate here or it overwrites the per-share
# figure — the total is captured separately for validation via _POS_COST_KEYS.
_POS_AVG_KEYS  = ("averagecost", "averageprice", "avgprice", "avgcost", "buyprice")
_POS_COST_KEYS = ("costbasis", "totalinvested", "investedvalue", "investedamount")
_POS_LTP_KEYS  = ("lastprice", "marketprice", "currentprice", "ltp", "closeprice", "price")
_POS_MVAL_KEYS = ("marketvalue", "currentvalue", "value", "mktval", "positionvalue")
_CASH_KEYS     = ("cashbalance", "availablecash", "cash", "buyingpower",
                  "cashavailable", "withdrawablecash", "totalcash")


def _pick(d: dict, *cands):
    low = {k.lower(): k for k in d}
    for c in cands:
        if c in low:
            return d[low[c]]
    for c in cands:
        for lk, orig in low.items():
            if c in lk:
                return d[orig]
    return None


def _find_positions(node) -> list[dict]:
    if isinstance(node, list):
        if node and isinstance(node[0], dict):
            d0 = node[0]
            if _pick(d0, *_POS_NAME_KEYS) and (
                _pick(d0, *_POS_QTY_KEYS) is not None or _pick(d0, *_POS_MVAL_KEYS) is not None
            ):
                return node
        for it in node:
            r = _find_positions(it)
            if r:
                return r
    elif isinstance(node, dict):
        for v in node.values():
            r = _find_positions(v)
            if r:
                return r
    return []


def _find_cash(node) -> Decimal | None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in _CASH_KEYS and isinstance(v, (int, float, str)):
                c = ps.num(v)
                if c is not None:
                    return c
        for v in node.values():
            c = _find_cash(v)
            if c is not None:
                return c
    elif isinstance(node, list):
        for it in node:
            c = _find_cash(it)
            if c is not None:
                return c
    return None


def _holdings_from_json(payload) -> tuple[list[dict], Decimal | None]:
    records = _find_positions(payload)
    holdings: list[dict] = []
    if records:
        logger.info(f"Position JSON record keys: {sorted(records[0].keys())}")
        for d in records:
            sym = _pick(d, *_POS_NAME_KEYS)
            qty = ps.num(_pick(d, *_POS_QTY_KEYS))
            if not sym or qty is None or qty <= 0:
                continue
            avg  = ps.num(_pick(d, *_POS_AVG_KEYS))
            ltp  = ps.num(_pick(d, *_POS_LTP_KEYS))
            mval = ps.num(_pick(d, *_POS_MVAL_KEYS))
            cost = ps.num(_pick(d, *_POS_COST_KEYS))
            if ltp is None and mval is not None and qty:
                ltp = mval / qty
            # Fall back to total-invested / qty only if no per-share avg was found.
            if avg is None and cost is not None and qty:
                avg = cost / qty
            holdings.append({
                "symbol":        str(sym).strip().upper(),
                "isin":          (_pick(d, "isin") or ""),
                "exchange":      (_pick(d, "exchange", "listingexchange", "mic") or "NASDAQ"),
                "quantity":      str(qty),
                "avg_cost":      str(avg if avg is not None else (ltp or 0)),
                "current_price": str(ltp if ltp is not None else (avg or 0)),
            })
    return holdings, _find_cash(payload)


def _holdings_from_dom(page) -> list[dict]:
    """Parse the Portfolio holdings table by HEADER LABEL (not cell position).

    Vested's table is: Symbol | Current Price | 1D Return | Current Value |
    Overall Return | Total Invested | Average Cost | Qty. The return columns hold
    two numbers each, so positional parsing mis-maps the cells — we locate each
    column by its header text instead, which is robust to that.
    """
    data = page.evaluate("""() => {
      const tables = Array.from(document.querySelectorAll('table'));
      let target = null;
      for (const t of tables) {
        const h = (t.innerText || '').toLowerCase();
        if ((h.includes('symbol') || h.includes('ticker') || h.includes('stock'))
            && (h.includes('qty') || h.includes('quantity') || h.includes('shares')
                || h.includes('value'))) { target = t; break; }
      }
      if (!target) return {headers: [], rows: []};
      const clean = c => (c.innerText || '').replace(/\\s+/g, ' ').trim();
      let headers = Array.from(target.querySelectorAll('thead th')).map(clean);
      if (!headers.length)
        headers = Array.from(target.querySelectorAll('[role=columnheader]')).map(clean);
      const body = target.querySelector('tbody') || target;
      const rows = Array.from(body.querySelectorAll('tr')).map(tr =>
        Array.from(tr.querySelectorAll('td,th')).map(clean));
      return {headers, rows};
    }""")
    headers = data.get("headers") or []
    grid    = data.get("rows") or []
    if not grid:
        return []

    def _map_header(cells: list[str]) -> dict:
        m: dict = {}
        for i, c in enumerate(cells):
            t = (c or "").lower()
            if ("symbol" in t or "ticker" in t) and "symbol" not in m:
                m["symbol"] = i
            elif "current price" in t:
                m["price"] = i
            elif "current value" in t or "market value" in t:
                m["mval"] = i
            elif "total invested" in t or "invested" in t:
                m["cost"] = i
            elif "average cost" in t or "avg cost" in t:
                m["avg"] = i
            elif "qty" in t or "quantity" in t or t.strip() == "shares":
                m["qty"] = i
        return m

    # Prefer mapping by real header labels (thead / role=columnheader). If Vested
    # renders the header outside the table, fall back to the known fixed layout:
    # Symbol | Current Price | 1D Return | Current Value | Overall Return |
    # Total Invested | Average Cost | Qty.
    cols = _map_header(headers)
    if not ("symbol" in cols and "qty" in cols):
        cols = {"symbol": 0, "price": 1, "mval": 3, "cost": 5, "avg": 6, "qty": 7}
        logger.info("[vested] using fixed column layout (header row not in table)")
    else:
        logger.info(f"[vested] mapped holdings columns from header: {cols}")

    def _cell(cells, key):
        i = cols.get(key)
        return cells[i] if i is not None and i < len(cells) else None

    holdings: list[dict] = []
    for cells in grid:
        sym_raw = _cell(cells, "symbol") or (cells[0] if cells else "")
        match = re.search(r"[A-Z][A-Z0-9.\-]{0,9}", (sym_raw or "").upper())
        sym = match.group(0) if match else (sym_raw or "").strip().upper()
        if not sym or sym in ("SYMBOL", "TICKER", "STOCK", "NAME", "TOTAL"):
            continue

        qty   = ps.num(_cell(cells, "qty"))
        avg   = ps.num(_cell(cells, "avg"))
        price = ps.num(_cell(cells, "price"))
        mval  = ps.num(_cell(cells, "mval"))
        cost  = ps.num(_cell(cells, "cost"))
        if qty is None or qty <= 0:
            continue

        # Derive per-share figures if a column was missing.
        if avg is None and cost is not None and qty:
            avg = cost / qty
        if price is None and mval is not None and qty:
            price = mval / qty

        holdings.append({
            "symbol":        sym, "isin": "", "exchange": "NASDAQ",
            "quantity":      str(qty),
            "avg_cost":      str(avg if avg is not None else (price or 0)),
            "current_price": str(price if price is not None else (avg or 0)),
        })
    return holdings


def parse_holdings_xlsx(path: Path, cfg: VestedConfig) -> tuple[list[dict], Decimal | None]:
    """Parse the downloaded holdings workbook. Schema-capture-first: on first run
    logs the layout and raises VestedSchemaUnknown so the column mapping can be
    finalised against the real file (then set VESTED_HOLDINGS_SCHEMA_READY=1)."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if not HOLDINGS_SCHEMA_READY:
        out = []
        for ws in wb.worksheets:
            out.append(f"  sheet '{ws.title}':")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 25:
                    out.append("    ..."); break
                out.append(f"    r{i}: {[str(c) for c in row if c is not None][:14]}")
        wb.close()
        logger.warning(f"[{cfg.code}] holdings XLSX schema not mapped — layout below; "
                       f"implement parse_holdings_xlsx() and set "
                       f"VESTED_HOLDINGS_SCHEMA_READY=1.\n" + "\n".join(out))
        raise VestedSchemaUnknown(path.name)
    wb.close()
    # TODO(once schema known): map workbook rows → holding dicts + cash.
    raise NotImplementedError("parse_holdings_xlsx mapping pending real schema")


def scrape_holdings(page, cfg: VestedConfig) -> tuple[list[dict], Decimal | None]:
    captured: list = []

    def _on_response(resp):
        try:
            if "json" not in resp.headers.get("content-type", ""):
                return
            url = resp.url.lower()
            if any(k in url for k in ("portfolio", "holding", "position", "account", "summary")):
                captured.append(resp.json())
        except Exception:
            pass

    page.on("response", _on_response)
    ps.click(page, [
        'a:has-text("US Stocks")', 'a:has-text("US Stocks and ETFs")',
        'a:has-text("Stocks and ETFs")', 'button:has-text("US Stocks")',
        'a:has-text("Portfolio")', 'a:has-text("Holdings")', '[href*="portfolio" i]',
    ], "US Stocks and ETFs nav", optional=True)
    page.wait_for_timeout(5000)
    ps.shot(page, CFG, f"vested_holdings_{cfg.prefix}.png")

    holdings: list[dict] = []
    cash: Decimal | None = None
    for payload in captured:
        h, c = _holdings_from_json(payload)
        if h and not holdings:
            holdings = h
        if c is not None and cash is None:
            cash = c

    # Download the official workbook (archival + future primary once mapped).
    path = _download_holdings(page, cfg)
    if path is not None and not holdings:
        try:
            holdings, c = parse_holdings_xlsx(path, cfg)
            if c is not None and cash is None:
                cash = c
        except (VestedSchemaUnknown, NotImplementedError):
            logger.info(f"[{cfg.code}] XLSX mapping pending — using JSON/DOM holdings this run")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    if not holdings:
        logger.info(f"[{cfg.code}] no holdings from JSON/XLSX — trying DOM parse")
        holdings = _holdings_from_dom(page)

    logger.info(f"[{cfg.code}] parsed {len(holdings)} Vested holdings, cash={cash}")
    if not holdings:
        ps.shot(page, CFG, f"vested_empty_{cfg.prefix}.png")
    return holdings, cash


def _download_holdings(page, cfg: VestedConfig) -> Path | None:
    for sel in ('a:has-text("Excel")', 'button:has-text("Excel")',
                'a:has-text("Download")', 'button:has-text("Download")',
                'a:has-text("Export")', '[aria-label*="download" i]', '[class*="download" i]'):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            with page.expect_download(timeout=ps.ACTION_TIMEOUT) as dl:
                loc.click(force=True)
            return ps.save_download(dl.value, CFG, cfg.prefix + "_holdings")
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Transactions — profile menu → Transactions, date-filtered
# ---------------------------------------------------------------------------
# Prefer ISO "timestamp" over Vested's human "date" ("Jun 18, 2026"), which
# str[:10] would mangle into an unparseable value.
_TXN_DATE_KEYS = ("timestamp", "tradedate", "transactiondate", "createdat",
                  "executedat", "settledate", "date")
_TXN_SYM_KEYS  = ("symbol", "ticker", "instrumentsymbol", "securitysymbol")
_TXN_TYPE_KEYS = ("type", "side", "transactiontype", "action", "ordertype", "activitytype")
_TXN_AMT_KEYS  = ("amount", "netamount", "value", "totalamount", "tradevalue", "grossamount")
_TXN_QTY_KEYS  = ("quantity", "units", "shares", "qty")
_TXN_PRICE_KEYS = ("price", "executionprice", "tradeprice", "avgprice")

# Vested settles via DriveWealth, whose ledger codes are: SPUR (stock purchase),
# SSAL (stock sale), DIV (dividend), DIVTAX (div withholding tax), INT (cash
# interest), FEE, ORDER, CSD/CSR (cash settlement), JNLC (journal cash).
_SELL_WORDS = ("sell", "sld", "sale", "ssal", "redeem", "redemption")
_BUY_WORDS  = ("buy", "bot", "spur", "purchase", "subscription")
_INFLOW_WORDS = ("dividend", "div", "coupon")     # DIV (per-symbol dividend)
# Account-level / non-position flows excluded from a holding's XIRR. "divtax"
# before "div" and "tax" guard the dividend-tax rows; "int"/"order"/cash codes
# keep cash-account movements out.
_SKIP_WORDS = ("deposit", "withdraw", "transfer", "fee", "tax", "fund", "ach",
               "wire", "divtax", "int", "order", "csd", "csr", "jnlc")


def _txn_sign_amount(ttype: str, amount: Decimal | None, qty: Decimal | None,
                     price: Decimal | None) -> Decimal | None:
    """Signed cash flow for a holding's XIRR: buys negative (cash out), sells &
    dividends positive (cash in). Account-level cash moves (deposit/withdraw) are
    skipped — they are not a return on the security."""
    t = (ttype or "").strip().lower()
    amt = amount
    if amt is None and qty is not None and price is not None:
        amt = qty * price
    if amt is None:
        return None
    amt = abs(amt)
    # Skip first so e.g. "DIVTAX" (dividend withholding tax) is excluded rather
    # than counted as a positive dividend by the "div" match below.
    if any(w in t for w in _SKIP_WORDS):
        return None
    if any(w in t for w in _SELL_WORDS) or any(w in t for w in _INFLOW_WORDS):
        return amt
    if any(w in t for w in _BUY_WORDS):
        return -amt
    # Unknown type with a signed amount → trust its sign (negative == outflow).
    if amount is not None and amount < 0:
        return amount
    return None


def _txns_from_json(payload) -> list[dict]:
    def _find(node):
        if isinstance(node, list):
            if node and isinstance(node[0], dict):
                d0 = node[0]
                if _pick(d0, *_TXN_DATE_KEYS) and (
                    _pick(d0, *_TXN_AMT_KEYS) is not None or _pick(d0, *_TXN_QTY_KEYS) is not None
                ):
                    return node
            for it in node:
                r = _find(it)
                if r:
                    return r
        elif isinstance(node, dict):
            for v in node.values():
                r = _find(v)
                if r:
                    return r
        return []

    records = _find(payload)
    if not records:
        return []
    return _parse_txn_records(records)


def _parse_txn_records(records: list) -> list[dict]:
    """Map raw transaction records → signed {date,symbol,amount} cash flows.

    Vest/basket buys (orderEntity=VEST, e.g. "Blockchain Ecosystem") wrap their
    real per-stock legs in a nested `transactions[]` array and carry NO symbol or
    amount at the top level — flatten those so each leg is counted.
    """
    flat: list = []
    for r in records:
        if isinstance(r, dict) and isinstance(r.get("transactions"), list) and r["transactions"]:
            flat.extend(r["transactions"])
        else:
            flat.append(r)

    out = []
    for d in flat:
        if not isinstance(d, dict):
            continue
        dt  = _pick(d, *_TXN_DATE_KEYS)
        sym = _pick(d, *_TXN_SYM_KEYS)
        if not dt or not sym:
            continue
        amt = _txn_sign_amount(
            str(_pick(d, *_TXN_TYPE_KEYS) or ""),
            ps.num(_pick(d, *_TXN_AMT_KEYS)),
            ps.num(_pick(d, *_TXN_QTY_KEYS)),
            ps.num(_pick(d, *_TXN_PRICE_KEYS)),
        )
        if amt is None:
            continue
        out.append({"date": str(dt)[:10], "symbol": str(sym).strip().upper(), "amount": float(amt)})
    return out


def scrape_transactions(page, cfg: VestedConfig, from_date: date, to_date: date) -> list[dict]:
    """Pull the full COMPLETED transaction history and return signed cash flows.

    Loads /en/global/transaction-history (so the SPA authenticates the call and
    we can grab its Authorization header), then paginates the backend API
    directly — the page itself only fetches page 1.
    """
    auth_headers: dict = {}
    first_pages: list = []

    def _on_request(req):
        if "user-transaction" in req.url:
            try:
                auth_headers.update(req.headers)
            except Exception:
                pass

    def _on_response(resp):
        try:
            if "user-transaction" in resp.url and "json" in resp.headers.get("content-type", ""):
                first_pages.append(resp.json())
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("response", _on_response)

    page.goto(TXN_URL, timeout=ps.NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)   # let the SPA fetch page 1 (captures auth header)
    ps.shot(page, CFG, f"vested_transactions_{cfg.prefix}.png")

    records = _paginate_transactions(page, auth_headers, first_pages)
    txns = _parse_txn_records(records) if records else _txns_from_dom(page)

    # Keep only the requested window.
    out = []
    for t in txns:
        try:
            d = date.fromisoformat(t["date"])
        except Exception:
            continue
        if from_date <= d <= to_date:
            out.append(t)
    logger.info(f"[{cfg.code}] {len(out)} transactions in {from_date}..{to_date} "
                f"({len(records)} records, {len(txns)} cash flows total)")
    return out


def _paginate_transactions(page, auth_headers: dict, first_pages: list) -> list[dict]:
    """All COMPLETED transaction records, paginating the backend API with the
    page's own Authorization header (the API host is separate from the app)."""
    # Dedup by transactionId across the SPA's nav fetch + our API pagination.
    by_id: dict = {}

    def _add(recs):
        for r in recs:
            by_id[r.get("transactionId") or len(by_id)] = r

    for pl in first_pages:
        if isinstance(pl, dict):
            _add(pl.get("completed") or [])

    if "authorization" not in {k.lower() for k in auth_headers}:
        logger.warning("[vested] no Authorization header captured — using nav records only")
        return list(by_id.values())

    # Replay the exact browser request headers (Vested's API 401s on a partial
    # header set), dropping only hop-by-hop / pseudo headers.
    block = {"host", "content-length", "connection", "accept-encoding"}
    fwd = {k: v for k, v in auth_headers.items()
           if not k.startswith(":") and k.lower() not in block}

    rc = page.context.request
    for pg in range(1, 201):
        try:
            r = rc.get(f"{TXN_API}?type=COMPLETED&symbol=&page={pg}", headers=fwd, timeout=30000)
            if not r.ok:
                logger.warning(f"[vested] txn page {pg} HTTP {r.status}; body={r.text()[:160]}")
                break
            data = r.json()
        except Exception as e:
            logger.warning(f"[vested] txn page {pg} fetch failed: {e}")
            break
        recs = (data.get("completed") if isinstance(data, dict) else None) or []
        logger.info(f"[vested] api page {pg}: {len(recs)} records")
        if not recs:
            break
        _add(recs)
    logger.info(f"[vested] total {len(by_id)} unique completed transactions")
    return list(by_id.values())


def _apply_date_filter(page, cfg: VestedConfig, from_date: date, to_date: date):
    """Best-effort: fill From/To date inputs (several common formats) and apply.
    The caller re-filters by date anyway, so a missed filter is not fatal — it
    just pulls more rows than needed."""
    for iso, names in ((from_date.isoformat(), ("from", "start", "fromdate", "startdate")),
                       (to_date.isoformat(),   ("to", "end", "todate", "enddate"))):
        sels = [f'input[name*="{n}" i]' for n in names] + \
               [f'input[id*="{n}" i]' for n in names] + \
               [f'input[placeholder*="{n}" i]' for n in names]
        ps.fill(page, sels + ['input[type="date"]'], iso, f"date {iso}",
                clear=True, optional=True)
    ps.click(page, ['button:has-text("Apply")', 'button:has-text("Filter")',
                    'button:has-text("Search")', 'button:has-text("Go")'],
             "apply date filter", optional=True)


def _txns_from_dom(page) -> list[dict]:
    grid = page.evaluate("""() => {
      const tables = Array.from(document.querySelectorAll('table'));
      let target = null;
      for (const t of tables) {
        const h = (t.innerText || '').toLowerCase();
        if (h.includes('date') && (h.includes('symbol') || h.includes('type')
            || h.includes('amount') || h.includes('quantity'))) { target = t; break; }
      }
      if (!target) return {head: [], rows: []};
      const trs = Array.from(target.querySelectorAll('tr'));
      const head = trs.length ? Array.from(trs[0].querySelectorAll('td,th'))
          .map(c => (c.innerText||'').trim().toLowerCase()) : [];
      const rows = trs.slice(1).map(tr =>
        Array.from(tr.querySelectorAll('td,th')).map(c => (c.innerText||'').replace(/\\s+/g,' ').trim()));
      return {head, rows};
    }""")
    head = grid.get("head") or []
    rows = grid.get("rows") or []
    if not rows:
        return []

    def col(*names):
        for i, h in enumerate(head):
            if any(n in h for n in names):
                return i
        return None

    i_date  = col("date")
    i_sym   = col("symbol", "ticker", "stock", "security")
    i_type  = col("type", "side", "action", "transaction")
    i_amt   = col("amount", "value", "total")
    i_qty   = col("quantity", "units", "shares", "qty")
    i_price = col("price")

    out = []
    for cells in rows:
        def at(i):
            return cells[i] if (i is not None and i < len(cells)) else None
        dt = at(i_date)
        sym = at(i_sym)
        dm = re.search(r'\d{4}-\d{2}-\d{2}', str(dt) or "")
        if not dm or not sym:
            continue
        amt = _txn_sign_amount(str(at(i_type) or ""), ps.num(at(i_amt)),
                               ps.num(at(i_qty)), ps.num(at(i_price)))
        if amt is None:
            continue
        out.append({"date": dm.group(0), "symbol": str(sym).strip().upper(), "amount": float(amt)})
    return out


# ---------------------------------------------------------------------------
# Per-entity orchestration
# ---------------------------------------------------------------------------
def process_entity(cfg: VestedConfig) -> dict:
    """Scrape holdings + the incremental transactions, merge into the ledger,
    derive since-held + XIRR, and write the feed cache. Returns a small summary.
    """
    today = date.today()
    cache = _feed.load_cache_obj(BROKER.upper(), cfg.code)
    from_d, to_d, first_run = ps.incremental_range(cache, today)

    with ps.browser(CFG) as (ctx, page):
        try:
            login(page, cfg)
            holdings, cash = scrape_holdings(page, cfg)
            if not holdings:
                raise RuntimeError("no holdings parsed from Vested")

            txns: list[dict] = []
            if to_d >= from_d:
                txns = scrape_transactions(page, cfg, from_d, to_d)
            else:
                logger.info(f"[{cfg.code}] nothing new to pull (through {to_d.isoformat()})")
            ps.save_browser_session(CFG, cfg.prefix, ctx)
        finally:
            try:
                logout(page)
            except Exception:
                pass

    ledger = ps.merge_and_metric(holdings, (cache or {}).get("ledger"), txns, today)
    _feed.write_feed_cache(BROKER.upper(), cfg.code, CURRENCY, holdings,
                           cash=cash, as_of=None, ledger=ledger,
                           ledger_through=to_d.isoformat())
    return {"holdings": len(holdings), "txns": len(txns), "first_run": first_run}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run():
    started = ps.now_utc()
    configs = load_configs()
    if not configs:
        logger.warning("No Vested entities configured in .env — nothing to do.")
        return

    conn = ps.get_db()
    processed = failed = 0
    errors = []

    if RANDOMIZE and START_JITTER_S > 0 and len(configs) >= 1:
        jitter = random.uniform(0, START_JITTER_S)
        logger.info(f"Start jitter: waiting {jitter/60:.0f}m")
        time.sleep(jitter)

    try:
        for cfg in configs:
            logger.info(f"=== [{cfg.code}] Vested sync ===")
            try:
                if ps.load_entity_id(conn, cfg.code) is None:
                    raise RuntimeError(f"Entity '{cfg.code}' not found in DB")
                summary = process_entity(cfg)
                processed += summary["holdings"]
                logger.info(f"[{cfg.code}] OK — {summary['holdings']} holdings, "
                            f"{summary['txns']} new txns "
                            f"({'first run' if summary['first_run'] else 'incremental'})")
            except Exception as e:
                failed += 1
                errors.append(f"{cfg.code}: {e}")
                logger.error(f"[{cfg.code}] failed: {e}")

        status = "success" if failed == 0 else ("partial" if processed else "failed")
        ps.log_run(conn, "vested", status, processed, failed, started,
                   error="; ".join(errors) if errors else None)
        logger.info(f"=== Vested done: {processed} holdings, {failed} failed ===")
    finally:
        conn.close()


def main():
    lock = ps.Lock("vested_worker")
    if not lock.acquire():
        sys.exit(1)
    try:
        run()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
