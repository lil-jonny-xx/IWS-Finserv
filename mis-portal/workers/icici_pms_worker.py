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
registered email autoforwards to the central collector inbox (***REMOVED***)
— the SAME inbox the CAS pipeline reads. So after clicking GENERATE OTP we poll
that inbox (via gmail_worker) for the fresh ICICI OTP mail and read the code,
exactly as cas_automation_worker waits for CAS PDFs.

Flow per entity:
  1. Log in: enter PAN → Next → enter Password → Next → Generate OTP.
  2. Poll the central Gmail inbox for the OTP mail, extract the code, submit it.
  3. Dashboard → "List Of Strategies": for each PMS strategy, open View Portfolio
     and parse the Security Allocation table (Security · Sector · Security % ·
     Value). Both strategies aggregate into one entity holdings set.
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

LOGIN_URL = os.environ.get("ICICI_PMS_LOGIN_URL", "").strip()

WORKERS_DIR     = Path(__file__).parent
BROWSER_PROFILE_DIR = Path("/var/www/mis-portal/.icici_pms_browser_profile")
_SCREENSHOT_DIR     = "/home/SAdmin/.cams-screenshots"

NAV_TIMEOUT    = 60_000
ACTION_TIMEOUT = 15_000

# OTP retrieval from the central collector inbox (***REMOVED***).
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
    query = f"from:{OTP_FROM_FILTER} after:{after_ts}"
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
        'button:has-text("Send OTP")', 'button[type="submit"]',
    ], "Generate OTP")
    page.wait_for_timeout(random.randint(1500, 2500))

    code = fetch_otp(otp_request_ts)
    if not code:
        _shot(page, f"icici_otp_timeout_{cfg.prefix}.png")
        raise RuntimeError(
            f"[{cfg.code}] No OTP received in the central inbox — confirm the "
            f"registered email autoforwards ICICI OTP mails to the collector.")

    # OTP entry: a single box or 4-8 separate digit inputs.
    if not _fill(page, [
        'input[formcontrolname="otp"]',
        'input[placeholder*="OTP" i]',
        'input[name*="otp" i]',
    ], code, "OTP single box", optional=True):
        boxes = page.locator('input[maxlength="1"]')
        n = boxes.count()
        if n and n >= len(code):
            for i, ch in enumerate(code):
                boxes.nth(i).fill(ch)
        else:
            raise RuntimeError(f"[{cfg.code}] Could not locate OTP input field(s)")
    page.wait_for_timeout(random.randint(300, 700))
    _click(page, [
        'button:has-text("Verify")', 'button:has-text("Submit")',
        'button:has-text("Login")', 'button:has-text("Confirm")',
        'button[type="submit"]',
    ], "OTP submit")

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
def _parse_security_allocation(page, cfg: PmsConfig, tag: str) -> list[dict]:
    """Parse one strategy's 'Security Allocation' table:
       columns Security · Sector · Security % · Value.
    Returns row dicts in the pms_holding contract (market_value required)."""
    rows: list[dict] = []
    # The allocation grid is an HTML table or a CDK/material grid. Read every
    # row's cells generically and map by position, skipping the header.
    candidates = [
        "table:has-text('Security Allocation') tr",
        "table tr",
        "[role='row']",
    ]
    grid = []
    for sel in candidates:
        try:
            loc = page.locator(sel)
            cnt = loc.count()
            if cnt < 2:
                continue
            for i in range(cnt):
                cells = loc.nth(i).locator("td, th, [role='cell'], [role='gridcell']")
                vals = [cells.nth(j).inner_text().strip() for j in range(cells.count())]
                if vals:
                    grid.append(vals)
            if grid:
                break
        except Exception:
            continue

    for vals in grid:
        if len(vals) < 4:
            continue
        name = vals[0].strip()
        if not name or name.lower() in ("security", "total"):
            continue
        sector = vals[1].strip()
        weight = _num(vals[2])
        value  = _num(vals[3])
        if value is None:
            continue
        is_cash = name.strip().lower() == "cash" or sector.upper() in ("(NULL)", "NULL", "")
        rows.append({
            "holding_type":  "cash" if is_cash else "equity",
            "security_name": name,
            "isin":          None,
            "quantity":      None,
            "avg_cost":      None,
            "cost":          None,
            "current_price": None,
            "market_value":  value,
            "weight_pct":    weight,   # per-strategy weight; recomputed globally in upsert
        })
    logger.info(f"[{cfg.code}] {tag}: parsed {len(rows)} allocation rows")
    return rows


def collect_holdings(page, cfg: PmsConfig) -> list[dict]:
    """From the Dashboard, open each PMS strategy's portfolio and aggregate the
    security-allocation rows across strategies into one entity holdings set."""
    all_rows: list[dict] = []
    _click(page, ['a:has-text("Dashboard")', 'span:has-text("Dashboard")'],
           "Dashboard", optional=True)
    page.wait_for_timeout(1500)

    # Ensure the PMS tab (not AIF) is active on the strategy list.
    _click(page, ['button:has-text("PMS")', 'a:has-text("PMS")'], "PMS tab", optional=True)
    page.wait_for_timeout(800)

    # Each strategy row exposes a "Portfolio" action. Count them up-front because
    # navigating into a strategy re-renders the list.
    portfolio_btns = page.locator(
        'button:has-text("Portfolio"), a:has-text("Portfolio")')
    n = portfolio_btns.count()
    logger.info(f"[{cfg.code}] {n} strategy portfolio link(s) found")
    if n == 0:
        _shot(page, f"icici_nostrategies_{cfg.prefix}.png")

    for idx in range(n):
        try:
            page.locator('button:has-text("Portfolio"), a:has-text("Portfolio")'
                         ).nth(idx).click(timeout=ACTION_TIMEOUT, force=True)
            page.wait_for_timeout(1800)
            # Land on Strategy Details → View Portfolio → Allocation.
            _click(page, ['button:has-text("View Portfolio")',
                          'a:has-text("View Portfolio")'], "View Portfolio", optional=True)
            page.wait_for_timeout(1000)
            _click(page, ['button:has-text("Allocation")', 'a:has-text("Allocation")',
                          'div:has-text("Allocation")'], "Allocation tab", optional=True)
            page.wait_for_timeout(1200)
            _shot(page, f"icici_strategy{idx}_{cfg.prefix}.png")
            all_rows.extend(_parse_security_allocation(page, cfg, f"strategy[{idx}]"))
        except Exception as e:
            logger.warning(f"[{cfg.code}] strategy[{idx}] parse failed: {e}")
        finally:
            # Back to the dashboard list for the next strategy.
            _click(page, ['a:has-text("Dashboard")', 'span:has-text("Dashboard")',
                          'button:has-text("Back")'], "back to Dashboard", optional=True)
            page.wait_for_timeout(1500)
            _click(page, ['button:has-text("PMS")', 'a:has-text("PMS")'],
                   "PMS tab", optional=True)
            page.wait_for_timeout(600)

    return all_rows


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------
def _validate(rows: list[dict]):
    if not rows:
        raise PmsPartialScrape("no rows parsed")
    total = sum((r["market_value"] for r in rows), Decimal(0))
    if total <= 0:
        raise PmsPartialScrape(f"non-positive total market value ({total})")


def upsert(conn, entity_id: int, rows: list[dict], as_on: date | None = None) -> int:
    """Source-scoped full-replace of this entity's ICICI PMS holdings.

    Delete-then-insert WHERE entity_id AND source='icici_pms' (NOT entity-wide:
    Rajani Corp also carries a zerodha_pms source). Weights are recomputed over
    the combined total across strategies so they sum to 100% on the PMS page.
    """
    as_on = as_on or date.today()
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
                rows = collect_holdings(page, cfg)
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
