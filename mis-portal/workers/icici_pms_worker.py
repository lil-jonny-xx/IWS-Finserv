#!/usr/bin/env python3
"""
ICICI Prudential PMS Worker — IWS MIS Portal

Scrapes the ICICI Prudential Alternate Investments "PMS & AIF Digital Journey"
investor portal for each entity that holds an ICICI Prudential PMS account, then
parses and upserts the holdings. Like the CAMS CAS and Nuvama PMS pipelines this
is a login-gated web portal with no investor API, so Playwright (headed via Xvfb
+ stealth) drives the browser.

Login is PAN + Password + OTP (2FA). Unlike Nuvama, this portal forces an OTP
step. The OTP is delivered to the investor's registered mobile/email, and the
registered email autoforwards to the central collector inbox (address in .env)
— the SAME inbox the CAS pipeline reads. So after clicking GENERATE OTP we poll
that inbox (via gmail_worker) for the fresh ICICI OTP mail and read the code,
exactly as cas_automation_worker waits for CAS PDFs.

Flow per entity:
  1. Log in: enter PAN → Next → enter Password → Next → Generate OTP.
  2. Poll the central Gmail inbox for the OTP mail, extract the code, submit it.
  3. Dashboard → "List Of Strategies": for each PMS strategy, open View Portfolio
     → Holdings and parse that grid (Description · Units · Cost · Market Price ·
     Market Value · G/L · %G/L · %Asset). Both strategies aggregate into one
     entity holdings set. Holdings is read out of the grid's React state rather
     than the DOM — see _react_rows.
  4. Upsert into pms_holding with source='icici_pms', SOURCE-SCOPED full-replace
     (delete-then-insert WHERE entity_id AND source) — Rajani Corp also has a
     zerodha_pms source, so an entity-wide delete would wipe it.

Credentials live in .env as ICICI_PMS_{ENTITY}_{PAN,PASSWORD}; see the
"ICICI Prudential PMS" section there. Add a block per entity with an account.

NOTE: The portal's grid is rendered client-side and its exact URL / selectors /
column layout can only be confirmed against a live login. Selectors below are
best-effort with fallbacks; a screenshot is saved at every step under
_SCREENSHOT_DIR for first-run tuning, mirroring cams_trigger_worker.py and
nuvama_pms_worker.py.

Run:  python workers/icici_pms_worker.py
Cron: invoke via workers/cron_wrapper.py like the other workers.
"""
import logging
import os
import re
import base64
import hashlib
import random
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth
from pyvirtualdisplay import Display

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))   # workers/ — for gmail_worker
load_dotenv("/var/www/mis-portal/.env", override=True)

import gmail_worker  # central-inbox reader (reused from the CAS pipeline)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Entities that may hold an ICICI Prudential PMS account: (entity_code, env_prefix).
# entity_code must match entity.entity_name in the DB.
PMS_ENTITIES = [
    ("Rajani Corp", "RAJANICORP"),
]

SOURCE = "icici_pms"

# external_cashflow.broker value for this account. Kept distinct from any
# direct-broker code, and mirrored by PMS_SOURCE_FLOW_BROKER in main.py so the
# PMS endpoint can find these flows for a money-weighted return.
FLOW_BROKER = "icici_pms"

LOGIN_URL = os.environ.get("ICICI_PMS_LOGIN_URL", "").strip()

WORKERS_DIR     = Path(__file__).parent
BROWSER_PROFILE_DIR = Path("/var/www/mis-portal/.icici_pms_browser_profile")
_SCREENSHOT_DIR     = "/home/SAdmin/.cams-screenshots"

NAV_TIMEOUT    = 60_000
ACTION_TIMEOUT = 15_000

# OTP retrieval from the central collector inbox (address in .env).
GMAIL_TOKEN_CENTRAL = str(WORKERS_DIR / os.environ.get(
    "GMAIL_TOKEN_CENTRAL", "gmail_token_central.json"))
# Best-effort filters; tune against a real OTP mail. Sender substring + a code
# regex over the body. Defaults are deliberately loose ("icici" + "otp").
OTP_FROM_FILTER  = os.environ.get("ICICI_PMS_OTP_FROM", "icici")
OTP_SUBJECT_HINT = os.environ.get("ICICI_PMS_OTP_SUBJECT", "OTP")
OTP_CODE_REGEX   = os.environ.get("ICICI_PMS_OTP_REGEX", r"\b(\d{4,8})\b")
OTP_WAIT_S       = int(os.environ.get("ICICI_PMS_OTP_WAIT_S", "180"))
OTP_POLL_S       = int(os.environ.get("ICICI_PMS_OTP_POLL_S", "10"))

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


@dataclass
class PmsConfig:
    code:     str   # entity_name in DB, e.g. "Rajani Corp"
    prefix:   str   # env prefix, e.g. "RAJANICORP"
    pan:      str
    password: str


class PmsPartialScrape(Exception):
    """Raised when a freshly parsed holdings set looks truncated/partial, so the
    previous snapshot is kept instead of being overwritten with bad data."""


# ---------------------------------------------------------------------------
# Env / DB helpers
# ---------------------------------------------------------------------------
def _env(prefix: str, key: str, required: bool = False) -> str:
    val = os.environ.get(f"ICICI_PMS_{prefix}_{key}", "").strip()
    if required and not val:
        raise KeyError(f"ICICI_PMS_{prefix}_{key} not set in .env")
    return val


def load_configs() -> list[PmsConfig]:
    """Return a PmsConfig for every entity whose ICICI PMS PAN is set in .env."""
    configs = []
    for code, prefix in PMS_ENTITIES:
        pan = _env(prefix, "PAN")
        if not pan:
            continue
        configs.append(PmsConfig(
            code=code,
            prefix=prefix,
            pan=pan,
            password=_env(prefix, "PASSWORD", required=True),
        ))
    return configs


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def now_utc():
    return datetime.now(timezone.utc)


def load_entity_id(conn, entity_name: str) -> int | None:
    cur = conn.cursor()
    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (entity_name,))
    row = cur.fetchone()
    cur.close()
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# OTP via the central Gmail inbox (forwarded from the registered email)
# ---------------------------------------------------------------------------
def _message_body_text(msg: dict) -> str:
    """Flatten a Gmail message into decoded text (plain + html parts)."""
    out = []

    def walk(part):
        body = part.get("body", {})
        data = body.get("data")
        if data:
            try:
                out.append(base64.urlsafe_b64decode(data).decode("utf-8", "ignore"))
            except Exception:
                pass
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload", {}))
    # also the snippet — sometimes the code is right there
    if msg.get("snippet"):
        out.append(msg["snippet"])
    return "\n".join(out)


def fetch_otp(after_ts: int) -> str | None:
    """Poll the central inbox for an ICICI OTP mail newer than `after_ts` and
    return the extracted code, or None on timeout."""
    service = gmail_worker._get_service(GMAIL_TOKEN_CENTRAL)
    # in:anywhere is REQUIRED: the ICICI OTP mail (alternates@icicipruamc.com)
    # forwards into the collector and Gmail files it under SPAM, which the default
    # search scope excludes. Without this the poll never sees it.
    query = f"in:anywhere from:{OTP_FROM_FILTER} after:{after_ts}"
    if OTP_SUBJECT_HINT:
        query += f' subject:"{OTP_SUBJECT_HINT}"'
    deadline = time.time() + OTP_WAIT_S
    pat = re.compile(OTP_CODE_REGEX)
    logger.info(f"Waiting for ICICI OTP mail (q={query!r}, timeout {OTP_WAIT_S}s)...")
    while time.time() < deadline:
        try:
            messages = gmail_worker._search_messages(service, query)
        except Exception as e:
            logger.warning(f"OTP inbox search failed: {e}")
            messages = []
        for m in messages:
            full = gmail_worker._get_message(service, m["id"])
            # internalDate is ms since epoch — guard against stale mails the
            # query's day-granular `after:` might still return.
            try:
                if int(full.get("internalDate", "0")) // 1000 < after_ts - 5:
                    continue
            except Exception:
                pass
            body = _message_body_text(full)
            mm = pat.search(body)
            if mm:
                code = mm.group(1)
                logger.info(f"OTP received ({len(code)} digits)")
                return code
        remaining = int(deadline - time.time())
        logger.info(f"No OTP yet — retry in {OTP_POLL_S}s (~{remaining}s left)")
        time.sleep(OTP_POLL_S)
    logger.error("Timed out waiting for ICICI OTP mail")
    return None


# ---------------------------------------------------------------------------
# Playwright helpers (best-effort selectors with fallbacks + screenshots)
# ---------------------------------------------------------------------------
def _shot(page, filename: str):
    try:
        Path(_SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=os.path.join(_SCREENSHOT_DIR, filename))
        logger.info(f"Screenshot saved: {filename}")
    except Exception as e:
        logger.debug(f"Screenshot {filename} failed: {e}")


def _fill(page, selectors, value, label, optional=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=ACTION_TIMEOUT)
            loc.click(timeout=ACTION_TIMEOUT, force=True)
            page.wait_for_timeout(random.randint(150, 400))
            loc.fill("")
            loc.type(str(value), delay=random.randint(40, 90))
            logger.debug(f"Filled {label} via {sel}")
            return True
        except Exception:
            continue
    if optional:
        logger.info(f"{label} field not found — skipping (optional)")
        return False
    raise RuntimeError(f"Could not find {label} field. Tried: {selectors}")


def _click(page, selectors, label, optional=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=ACTION_TIMEOUT)
            loc.click(timeout=ACTION_TIMEOUT, force=True)
            logger.debug(f"Clicked {label} via {sel}")
            return True
        except Exception:
            continue
    if optional:
        logger.info(f"{label} not found — skipping (optional)")
        return False
    raise RuntimeError(f"Could not click {label}. Tried: {selectors}")


def _has_text(page, *needles) -> bool:
    try:
        content = page.content().lower()
        return any(n.lower() in content for n in needles)
    except Exception:
        return False


def _num(s) -> Decimal | None:
    """Parse a numeric cell: strip ₹, commas, %, spaces; handle blanks/NULL."""
    if s is None:
        return None
    txt = str(s).strip()
    if not txt or txt.upper() in ("NULL", "(NULL)", "-", "NA", "N/A"):
        return None
    txt = txt.replace("₹", "").replace(",", "").replace("%", "").strip()
    # lakh/crore suffixes occasionally appear in summary cells
    mult = Decimal(1)
    low = txt.lower()
    if low.endswith(" l") or low.endswith("l"):
        pass  # the allocation table uses raw values; leave L on security names alone
    try:
        return Decimal(txt) * mult
    except (InvalidOperation, ValueError):
        return None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def _login(page, cfg: PmsConfig):
    if not LOGIN_URL:
        raise RuntimeError(
            "ICICI_PMS_LOGIN_URL not set in .env — set the portal login URL first.")
    logger.info(f"[{cfg.code}] Logging in to ICICI Prudential PMS portal")
    page.goto(LOGIN_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(1200, 2200))
    _shot(page, f"icici_login_{cfg.prefix}.png")

    # --- Step 1: PAN ---
    _fill(page, [
        'input[formcontrolname="pan"]',
        'input[placeholder*="PAN" i]',
        'input[name*="pan" i]',
        'input[type="text"]',
    ], cfg.pan.upper(), "PAN")
    page.wait_for_timeout(random.randint(300, 700))
    _click(page, [
        'input[class*="nextBtnLogin"]', 'input[type="submit"]',
        'button:has-text("Next")', 'button:has-text("NEXT")',
        'button[type="submit"]',
    ], "PAN Next")
    page.wait_for_timeout(random.randint(1200, 2200))

    # --- Step 2: Password (field appears after PAN is accepted) ---
    _shot(page, f"icici_password_{cfg.prefix}.png")
    _fill(page, [
        'input[formcontrolname="password"]',
        'input[type="password"]',
        'input[placeholder*="Password" i]',
    ], cfg.password, "password")
    page.wait_for_timeout(random.randint(300, 700))
    _click(page, [
        'input[class*="nextBtnLogin"]', 'input[type="submit"]',
        'button:has-text("Next")', 'button:has-text("NEXT")',
        'button:has-text("Login")', 'button[type="submit"]',
    ], "password Next")
    page.wait_for_timeout(random.randint(1500, 2500))

    if _has_text(page, "invalid", "incorrect", "wrong password", "does not match"):
        _shot(page, f"icici_loginfail_{cfg.prefix}.png")
        raise RuntimeError(f"[{cfg.code}] Login appears rejected — check PAN/password")

    # --- Step 3: OTP ---
    _shot(page, f"icici_otp_{cfg.prefix}.png")
    # Record the moment we ask for a fresh OTP so we only read mails after it.
    otp_request_ts = int(time.time())
    _click(page, [
        'button:has-text("Generate OTP")', 'button:has-text("GENERATE OTP")',
        'button:has-text("Send OTP")',
        'input[type="submit"]', 'button[type="submit"]',
    ], "Generate OTP")
    page.wait_for_timeout(random.randint(1500, 2500))

    code = fetch_otp(otp_request_ts)
    if not code:
        _shot(page, f"icici_otp_timeout_{cfg.prefix}.png")
        raise RuntimeError(
            f"[{cfg.code}] No OTP received in the central inbox — confirm the "
            f"registered email autoforwards ICICI OTP mails to the collector.")

    # OTP entry: ICICI renders 6 separate single-digit boxes (class OTP_inputOtp)
    # and AUTO-SUBMITS on the final keystroke — there is no Verify/Submit button.
    # Type with real key events (.press) so the component's auto-advance/submit
    # handler fires; .fill() would set the value without triggering it. A single
    # combined OTP box is handled as a fallback for other layouts.
    _shot(page, f"icici_otpentry_{cfg.prefix}.png")
    boxes = page.locator('input[class*="OTP_inputOtp"], input[maxlength="1"]')
    nb = boxes.count()
    if nb >= len(code):
        for i, ch in enumerate(code):
            b = boxes.nth(i)
            b.click()
            b.press(ch)
            page.wait_for_timeout(random.randint(80, 180))
    elif not _fill(page, [
        'input[formcontrolname="otp"]', 'input[placeholder*="OTP" i]',
        'input[name*="otp" i]',
    ], code, "OTP single box", optional=True):
        raise RuntimeError(f"[{cfg.code}] Could not locate OTP input field(s)")
    page.wait_for_timeout(random.randint(700, 1200))
    # Most layouts auto-submit; click a verify/submit button only if one exists.
    _click(page, [
        'button:has-text("Verify")', 'button:has-text("Submit")',
        'button:has-text("Login")', 'button:has-text("Confirm")',
        'input[type="submit"]',
    ], "OTP submit", optional=True)

    # Logged in when the Dashboard chrome appears.
    try:
        page.get_by_text("Dashboard", exact=False).first.wait_for(
            state="visible", timeout=NAV_TIMEOUT)
    except Exception:
        page.wait_for_timeout(4000)
    _shot(page, f"icici_dashboard_{cfg.prefix}.png")
    if _has_text(page, "invalid otp", "otp expired", "incorrect otp"):
        raise RuntimeError(f"[{cfg.code}] OTP rejected by the portal")


# ---------------------------------------------------------------------------
# Navigation + parsing
# ---------------------------------------------------------------------------
# The portal's grids are MUI DataGrids: the DOM only ever holds the current
# 5-row page, but React keeps the WHOLE dataset in the fiber tree, with fields
# the table never renders. So we read component state instead of scraping cells
# — no pagination, no ₹/comma parsing, full float precision.
_REACT_ROWS_JS = """
(key) => {
  const root = document.querySelector("#root") || document.body;
  const k = Object.keys(root).find(x => x.startsWith("__reactContainer$") ||
                                        x.startsWith("__reactFiber$"));
  if (!k) return null;
  let best = null, seen = 0;
  const hit = a => Array.isArray(a) && a.length && a[0] &&
                   typeof a[0] === "object" && (key in a[0]);
  const visit = f => {
    if (!f || seen > 60000) return;
    seen++;
    for (const bag of [f.memoizedProps, f.memoizedState]) {
      if (!bag || typeof bag !== "object") continue;
      let s = bag, d = 0;
      while (s && d < 8) {
        for (const v of Object.values(s)) {
          if (hit(v) && (!best || v.length > best.length)) best = v;
        }
        s = s.next || s.baseState || null; d++;
      }
    }
    visit(f.child); visit(f.sibling);
  };
  const c = root[k];
  visit(c && c.stateNode ? c.stateNode.current : c);
  return best;
}
"""


def _react_rows(page, key: str) -> list[dict]:
    """Longest array in the React tree whose rows carry `key`. [] if not found."""
    try:
        return page.evaluate(_REACT_ROWS_JS, key) or []
    except Exception as e:
        logger.warning(f"React state read for key={key!r} failed: {e}")
        return []


def _dec(v) -> Decimal | None:
    """Numbers arrive from React as float OR string ('143364.35'); via str() so
    binary float noise never reaches the DB."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _parse_holdings(page, cfg: PmsConfig, tag: str) -> list[dict]:
    """Parse one strategy's 'Holdings' tab — Description · Units · Cost ·
    Market Price · Market Value · G/L · %G/L · %Asset.

    This replaced the old 'Allocation' tab parse (Security/Sector/%/Value),
    which was the only tab the worker ever opened and which carries no units or
    cost — that is why every icici_pms row used to land with NULL quantity /
    avg_cost / cost / current_price while the other two PMS sources were fully
    populated.

    The row's `stock` field is the ASSET CLASS ("Equity", "Mutual Funds",
    "Cash and equivalent"), not a ticker. Anything that is not cash is stored as
    'equity': _pms_aggregate in main.py buckets strictly on 'equity'/'cash', so
    a third holding_type would silently drop out of both the P&L and the cash
    totals. The liquid ETF is already carried as 'equity' today.
    """
    rows: list[dict] = []
    for r in _react_rows(page, "marketPrice"):
        name = (r.get("description") or "").strip()
        mv = _dec(r.get("marketValue"))
        if not name or mv is None:
            continue
        asset_class = (r.get("stock") or "").strip().lower()
        is_cash = asset_class.startswith("cash")
        # For cash the portal fills units/cost with the balance itself and
        # prices it at 0 — meaningless as quantity/price, so leave them NULL.
        qty   = None if is_cash else _dec(r.get("units"))
        price = None if is_cash else _dec(r.get("marketPrice"))
        cost  = _dec(r.get("cost"))
        rows.append({
            "holding_type":  "cash" if is_cash else "equity",
            "security_name": name,
            "isin":          None,   # the portal exposes no ISIN on any tab
            "quantity":      qty,
            "avg_cost":      (cost / qty) if (cost is not None and qty) else None,
            "cost":          cost,
            "current_price": price,
            "market_value":  mv,
            "weight_pct":    _dec(r.get("asset")),  # per-strategy; recomputed in upsert
        })
    priced = sum(1 for r in rows if r["holding_type"] != "cash" and r["quantity"])
    logger.info(f"[{cfg.code}] {tag}: parsed {len(rows)} holdings rows "
                f"({priced} with units/cost)")
    return rows


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
# How each of the portal's transaction types is treated.
#
# Only money the INVESTOR moves in or out is an external cash flow. Everything
# else happens inside the account and is already reflected in its market value,
# so counting it as a flow would double-count it in the money-weighted return:
#   BUY/SELL  — reshuffles the same money between cash and securities.
#   DIV       — income credited into the account, not withdrawn.
#   EXP       — management/operational fees deducted from the account.
#   TDI/TDO   — transfers between the capital and TDS sub-accounts; net zero.
#   SIT/SOT   — STP between the two strategies. Both strategies aggregate into
#               ONE icici_pms account here, so these net out entirely.
FLOW_TYPES = {
    "II+": "DEPOSIT",      # Initial Investment / Fresh Inflow / New Subscription
}
INTERNAL_TYPES = {"BUY", "SELL", "DIV", "EXP", "TDI", "TDO", "SIT", "SOT"}


def _parse_transactions(page, cfg: PmsConfig, tag: str) -> list[dict]:
    """Parse one strategy's 'Transactions' tab out of the grid's React state.

    The grid paginates five rows at a time over ~46 pages, but the whole history
    is already in component state (paging fires no request), so this reads it in
    one go — and picks up units/unitPrice/exchange, which the table never shows.
    """
    rows: list[dict] = []
    for r in _react_rows(page, "tranDate"):
        try:
            d = datetime.strptime(r["tranDate"], "%d/%m/%Y").date()
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({
            "client_id":  r.get("clientId"),
            "date":       d,
            "type":       (r.get("tranType") or "").strip().upper(),
            "type_desc":  (r.get("tranTypeDesc") or "").strip(),
            "security":   (r.get("security") or "").strip(),
            "amount":     _dec(r.get("amount")),
            "units":      _dec(r.get("units")),
            "unit_price": _dec(r.get("unitPrice")),
        })
    unknown = {r["type"] for r in rows} - set(FLOW_TYPES) - INTERNAL_TYPES
    if unknown:
        # Loud, because an unclassified type is silently absent from both the
        # realised P&L and the cash flows.
        logger.warning(f"[{cfg.code}] {tag}: UNKNOWN transaction types {sorted(unknown)} "
                       f"— classify them in FLOW_TYPES/INTERNAL_TYPES")
    logger.info(f"[{cfg.code}] {tag}: parsed {len(rows)} transactions")
    return rows


def _txn_ref(t: dict, suffix: str, seq: int) -> str:
    """Stable dedup key for a transaction-derived row.

    NOT the portal's own row id ("10143236-14/08/2026-7") — its trailing number
    is the row's position in the returned array, so it shifts as new activity
    lands and the same trade would be re-inserted under a new ref. This hashes
    the content instead, with `seq` separating rows that are identical in every
    field (split fills on the same day at the same price).
    """
    key = "|".join(str(x) for x in (
        t["client_id"], t["date"], t["type"], t["security"],
        t["units"], t["unit_price"], t["amount"], seq, suffix))
    return hashlib.sha1(key.encode()).hexdigest()[:32]


def _sequenced(txns: list[dict]) -> list[tuple[dict, int]]:
    """Pair each transaction with an occurrence index among rows identical to
    it, so _txn_ref stays deterministic across runs regardless of grid order."""
    ordered = sorted(txns, key=lambda t: (
        t["date"], t["type"], t["security"],
        str(t["amount"]), str(t["units"]), str(t["unit_price"])))
    seen: dict[tuple, int] = {}
    out = []
    for t in ordered:
        k = (t["client_id"], t["date"], t["type"], t["security"],
             str(t["amount"]), str(t["units"]), str(t["unit_price"]))
        seen[k] = seen.get(k, -1) + 1
        out.append((t, seen[k]))
    return out


def _reconciles(txns: list[dict], holdings: list[dict], cfg: PmsConfig) -> bool:
    """True when every security's BUY − SELL equals the units currently held.

    This is the gate on writing realised P&L. FIFO is only correct over a
    COMPLETE history: if an opening lot were missing, a sale would be matched
    against the wrong lot and booked at the wrong cost — and because realised
    rows are deduped and never revised, that error would be permanent. A clean
    reconciliation proves the history reaches back to inception. It also fails
    closed on a corporate action (a split changes units without a BUY), which
    is exactly when the naive cost basis would be wrong.
    """
    net: dict[str, Decimal] = {}
    for t in txns:
        if t["type"] == "BUY":
            net[t["security"]] = net.get(t["security"], Decimal(0)) + (t["units"] or Decimal(0))
        elif t["type"] == "SELL":
            net[t["security"]] = net.get(t["security"], Decimal(0)) - (t["units"] or Decimal(0))
    held = {h["security_name"]: (h["quantity"] or Decimal(0))
            for h in holdings if h["holding_type"] != "cash"}
    bad = [(n, net.get(n, Decimal(0)), held.get(n, Decimal(0)))
           for n in set(net) | set(held)
           if net.get(n, Decimal(0)) != held.get(n, Decimal(0))]
    if bad:
        for n, a, b in bad[:5]:
            logger.warning(f"[{cfg.code}] reconcile MISMATCH {n}: txn-net {a} vs held {b}")
        logger.warning(f"[{cfg.code}] {len(bad)} security(ies) do not reconcile — "
                       f"skipping realised P&L (holdings + flows still written)")
        return False
    logger.info(f"[{cfg.code}] transactions reconcile against holdings "
                f"({len(held)} securities) — realised P&L is safe to compute")
    return True


def _fifo_realised(txns: list[dict]) -> list[dict]:
    """FIFO-match SELLs against earlier BUYs, one realised row per matched lot.

    ICICI publishes no realised capital-gains statement through this portal, so
    unlike Nuvama/Zerodha (whose statements arrive pre-computed) the P&L is
    derived here. Same FIFO convention used for Indian direct equity.
    """
    lots: dict[tuple, list[list]] = {}
    out: list[dict] = []
    for t, seq in _sequenced(txns):
        if t["type"] not in ("BUY", "SELL"):
            continue
        qty, px = t["units"], t["unit_price"]
        if not qty or px is None:
            continue
        key = (t["client_id"], t["security"])
        if t["type"] == "BUY":
            lots.setdefault(key, []).append([qty, px, t["date"]])
            continue
        remaining, lot_no = qty, 0
        queue = lots.setdefault(key, [])
        while remaining > 0 and queue:
            lot = queue[0]
            take = min(remaining, lot[0])
            out.append({
                "security_name":   t["security"],
                "quantity":        take,
                "buy_date":        lot[2],
                "sale_date":       t["date"],
                "purchase_amount": (take * lot[1]).quantize(Decimal("0.01")),
                "sale_amount":     (take * px).quantize(Decimal("0.01")),
                "pnl":             (take * (px - lot[1])).quantize(Decimal("0.01")),
                "source_ref":      _txn_ref(t, f"fifo{lot_no}", seq),
            })
            lot[0] -= take
            remaining -= take
            lot_no += 1
            if lot[0] <= 0:
                queue.pop(0)
        if remaining > 0:
            # _reconciles should have caught this; belt and braces.
            logger.warning(f"SELL of {t['security']} on {t['date']} exceeds held lots "
                           f"by {remaining} units — that portion is not booked")
    return out


def _capital_flows(txns: list[dict]) -> list[dict]:
    """External cash flows only — see FLOW_TYPES for why everything else is
    excluded. Investor-signed to match import_ledgers: a deposit is negative
    (money left the investor), a withdrawal positive."""
    out = []
    for t, seq in _sequenced(txns):
        ft = FLOW_TYPES.get(t["type"])
        if ft is None or t["amount"] is None:
            continue
        amt = abs(t["amount"])
        out.append({
            "flow_date":  t["date"],
            "flow_type":  ft,
            "amount":     -amt if ft == "DEPOSIT" else amt,
            "notes":      t["type_desc"],
            "source_ref": _txn_ref(t, "flow", seq),
        })
    return out


def collect_holdings(page, cfg: PmsConfig) -> tuple[list[dict], list[dict]]:
    """From the Dashboard, open each PMS strategy's portfolio and aggregate both
    its Holdings and its Transactions across strategies into one entity set.

    Both tabs are read in the same visit: reaching a strategy costs a dashboard
    reload plus an SPA drill-down, and the login that got us here costs a
    single-use OTP.
    """
    all_rows: list[dict] = []
    all_txns: list[dict] = []
    # We're on the dashboard right after login. Capture its URL so we can return
    # to the strategy list reliably between strategies — drilling into a strategy's
    # tabs makes SPA back-navigation flaky, so we just reload the dashboard.
    page.wait_for_timeout(1500)
    dashboard_url = page.url

    PORT_SEL = 'button:has-text("Portfolio"), a:has-text("Portfolio")'
    try:
        page.wait_for_selector(PORT_SEL, timeout=ACTION_TIMEOUT)
    except Exception:
        _shot(page, f"icici_nostrategies_{cfg.prefix}.png")
    n = page.locator(PORT_SEL).count()
    logger.info(f"[{cfg.code}] {n} strategy portfolio link(s) found")

    failures = 0
    for idx in range(n):
        try:
            if idx > 0:
                # Reload the dashboard list before opening the next strategy. The
                # list is fetched client-side AFTER the document loads, so wait
                # generously (NAV_TIMEOUT, with one retry) for the Portfolio links
                # to re-appear — a short wait here is the main cause of a strategy
                # being missed.
                for attempt in range(2):
                    page.goto(dashboard_url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector(PORT_SEL, state="visible", timeout=NAV_TIMEOUT)
                        break
                    except Exception:
                        if attempt == 1:
                            raise
                page.wait_for_timeout(1500)
            page.locator(PORT_SEL).nth(idx).click(timeout=ACTION_TIMEOUT, force=True)
            page.wait_for_timeout(1800)
            # Land on Strategy Details → View Portfolio, then switch to the
            # Holdings tab. Allocation is the tab that opens by default and is
            # the one this worker used to read; Holdings is the only tab that
            # carries units and cost, so the click is REQUIRED, not optional —
            # if it silently fails we would parse Allocation and write NULLs.
            _click(page, ['button:has-text("View Portfolio")',
                          'a:has-text("View Portfolio")'], "View Portfolio", optional=True)
            page.wait_for_timeout(1000)
            _click(page, ['[role="tab"]:has-text("Holdings")',
                          'a:has-text("Holdings")', 'button:has-text("Holdings")',
                          'text="Holdings"'], "Holdings tab")
            page.wait_for_timeout(2000)
            _shot(page, f"icici_strategy{idx}_{cfg.prefix}.png")
            srows = _parse_holdings(page, cfg, f"strategy[{idx}]")
            if not srows:
                raise PmsPartialScrape(f"strategy[{idx}] yielded 0 rows")
            all_rows.extend(srows)

            # Transactions are best-effort: they feed realised P&L and cash
            # flows, both of which can be rebuilt on a later run, whereas the
            # holdings snapshot is the daily critical path. A transactions
            # failure must not cost us the holdings.
            try:
                _click(page, ['[role="tab"]:has-text("Transactions")',
                              'a:has-text("Transactions")',
                              'button:has-text("Transactions")'], "Transactions tab")
                page.wait_for_timeout(2500)
                all_txns.extend(_parse_transactions(page, cfg, f"strategy[{idx}]"))
            except Exception as e:
                logger.warning(f"[{cfg.code}] strategy[{idx}] transactions failed: {e}")
        except Exception as e:
            failures += 1
            logger.warning(f"[{cfg.code}] strategy[{idx}] parse failed: {e}")

    # A source-scoped full-replace means a partial scrape (one strategy missed)
    # would overwrite the good snapshot with an undercount. Reject it instead and
    # keep the previous snapshot — the next run self-heals.
    if failures:
        raise PmsPartialScrape(
            f"{failures}/{n} strategies failed to parse — keeping previous snapshot")
    return all_rows, all_txns


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------
def _validate(rows: list[dict]):
    if not rows:
        raise PmsPartialScrape("no rows parsed")
    total = sum((r["market_value"] for r in rows), Decimal(0))
    if total <= 0:
        raise PmsPartialScrape(f"non-positive total market value ({total})")
    # Cost/units completeness is the whole point of reading the Holdings tab.
    # If the tab click silently landed on Allocation, or ICICI reshapes the
    # grid's React state, every non-cash row comes back priceless — which is
    # exactly the failure that went unnoticed for months. Refuse the snapshot
    # rather than overwrite good rows with NULLs.
    invested = [r for r in rows if r["holding_type"] != "cash"]
    if invested and not any(r["quantity"] and r["cost"] for r in invested):
        raise PmsPartialScrape(
            f"{len(invested)} non-cash rows but none carry units+cost — "
            f"wrong tab, or the Holdings grid's state shape changed")


def _aggregate_rows(rows: list[dict]) -> list[dict]:
    """Merge rows that share (holding_type, security_name) — the pms_holding
    unique key. Each strategy carries its own 'Cash' line, and a security may be
    held in more than one strategy, so we sum value/cost/qty into one entity-level
    row. avg_cost is recomputed from the merged cost/qty when both are present."""
    from collections import OrderedDict

    def _add(a, b):
        if a is None and b is None:
            return None
        return (a or Decimal(0)) + (b or Decimal(0))

    merged: "OrderedDict[tuple, dict]" = OrderedDict()
    for r in rows:
        key = (r["holding_type"], r["security_name"])
        if key not in merged:
            merged[key] = dict(r)
            continue
        m = merged[key]
        m["market_value"] = _add(m.get("market_value"), r.get("market_value"))
        m["cost"]         = _add(m.get("cost"), r.get("cost"))
        m["quantity"]     = _add(m.get("quantity"), r.get("quantity"))
        if not m.get("isin"):
            m["isin"] = r.get("isin")
    for m in merged.values():
        if m.get("cost") and m.get("quantity"):
            m["avg_cost"] = m["cost"] / m["quantity"]
    return list(merged.values())


def upsert(conn, entity_id: int, rows: list[dict], as_on: date | None = None) -> int:
    """Source-scoped full-replace of this entity's ICICI PMS holdings.

    Delete-then-insert WHERE entity_id AND source='icici_pms' (NOT entity-wide:
    Rajani Corp also carries a zerodha_pms source). Weights are recomputed over
    the combined total across strategies so they sum to 100% on the PMS page.
    """
    as_on = as_on or date.today()
    rows = _aggregate_rows(rows)   # collapse duplicate (holding_type, security_name)
    total = sum((r["market_value"] for r in rows), Decimal(0)) or Decimal(1)
    cur = conn.cursor()
    cur.execute("DELETE FROM pms_holding WHERE entity_id = %s AND source = %s",
                (entity_id, SOURCE))
    count = 0
    for r in rows:
        weight = (r["market_value"] / total * 100).quantize(Decimal("0.0001"))
        cur.execute(
            """
            INSERT INTO pms_holding
                (entity_id, as_on_date, holding_type, security_name, isin,
                 quantity, avg_cost, cost, current_price, market_value,
                 weight_pct, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entity_id, as_on,
                r["holding_type"], r["security_name"], r.get("isin"),
                r.get("quantity"), r.get("avg_cost"), r.get("cost"),
                r.get("current_price"), r["market_value"], weight,
                SOURCE, now_utc(),
            ),
        )
        count += 1
    cur.close()
    return count


def upsert_realised(conn, entity_id: int, rows: list[dict]) -> int:
    """Insert FIFO-derived realised lots, skipping any already booked.

    Append-only, NOT the delete-then-insert used for holdings: a realised lot is
    a historical fact, and pms_realised is deduped on (source, source_ref) by a
    partial unique index. Re-running the worker re-derives identical refs and
    inserts nothing.
    """
    if not rows:
        return 0
    cur = conn.cursor()
    added = 0
    for r in rows:
        cur.execute(
            """
            INSERT INTO pms_realised
                (entity_id, source, security_name, isin, quantity, buy_date,
                 sale_date, purchase_amount, sale_amount, pnl, source_ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, source_ref) WHERE source_ref IS NOT NULL
            DO NOTHING
            """,
            (entity_id, SOURCE, r["security_name"], None, r["quantity"],
             r["buy_date"], r["sale_date"], r["purchase_amount"],
             r["sale_amount"], r["pnl"], r["source_ref"]),
        )
        added += cur.rowcount
    cur.close()
    return added


def upsert_cashflows(conn, entity_id: int, rows: list[dict]) -> int:
    """Insert external cash flows not already recorded.

    external_cashflow has no unique index on source_ref, so this checks before
    inserting — the same pattern broker_txn_sync_worker uses.
    """
    if not rows:
        return 0
    cur = conn.cursor()
    added = 0
    for r in rows:
        cur.execute("SELECT 1 FROM external_cashflow WHERE source_ref = %s",
                    (r["source_ref"],))
        if cur.fetchone():
            continue
        cur.execute(
            """
            INSERT INTO external_cashflow
                (entity_id, broker, flow_date, flow_type, symbol,
                 amount_native, currency, source, source_ref, notes)
            VALUES (%s, %s, %s, %s, NULL, %s, 'INR', %s, %s, %s)
            """,
            (entity_id, FLOW_BROKER, r["flow_date"], r["flow_type"],
             r["amount"], SOURCE, r["source_ref"], r["notes"]),
        )
        added += 1
    cur.close()
    return added


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_entity(cfg: PmsConfig) -> int:
    display = Display(visible=False, size=(1440, 900))
    display.start()
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = BROWSER_PROFILE_DIR / stale
        if p.exists() or p.is_symlink():
            p.unlink()
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            Stealth().apply_stealth_sync(page)
            try:
                _login(page, cfg)
                rows, txns = collect_holdings(page, cfg)
                _validate(rows)

                conn = get_db()
                try:
                    entity_id = load_entity_id(conn, cfg.code)
                    if entity_id is None:
                        raise RuntimeError(f"entity '{cfg.code}' not found in DB")
                    n = upsert(conn, entity_id, rows)
                    conn.commit()
                    total = sum((r["market_value"] for r in rows), Decimal(0))
                    logger.info(f"[{cfg.code}] Upserted {n} rows into pms_holding "
                                f"(source={SOURCE}, total ₹{total})")

                    # Committed separately, and never allowed to fail the run:
                    # the holdings snapshot above is the daily critical path,
                    # while realised lots and flows are append-only history that
                    # the next run re-derives.
                    if txns:
                        try:
                            flows = _capital_flows(txns)
                            f = upsert_cashflows(conn, entity_id, flows)
                            r = 0
                            if _reconciles(txns, _aggregate_rows(rows), cfg):
                                r = upsert_realised(conn, entity_id, _fifo_realised(txns))
                            conn.commit()
                            logger.info(f"[{cfg.code}] {len(txns)} transactions → "
                                        f"+{r} realised lot(s), +{f} cash flow(s)")
                        except Exception as e:
                            conn.rollback()
                            logger.error(f"[{cfg.code}] transaction persist FAILED "
                                         f"(holdings are committed): {e}")
                    return n
                finally:
                    conn.close()
            finally:
                ctx.close()
    finally:
        display.stop()


def run() -> int:
    configs = load_configs()
    if not configs:
        logger.info("No ICICI PMS entities configured (set ICICI_PMS_*_PAN) — nothing to do.")
        return 0
    if not LOGIN_URL:
        logger.error("ICICI_PMS_LOGIN_URL is not set in .env — cannot run.")
        return 1
    passed, failed = [], []
    for cfg in configs:
        try:
            process_entity(cfg)
            passed.append(cfg.code)
        except Exception as e:
            logger.error(f"[{cfg.code}] FAILED: {e}")
            failed.append(cfg.code)
    logger.info(f"=== ICICI PMS done. Passed: {passed} | Failed: {failed} ===")
    return 1 if failed else 0


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
