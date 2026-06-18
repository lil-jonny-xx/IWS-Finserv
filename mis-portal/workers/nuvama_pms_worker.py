#!/usr/bin/env python3
"""
Nuvama PMS Worker — IWS MIS Portal

Downloads the portfolio report for each Nuvama-managed PMS account from the
Nuvama Asset Services "WealthSpectrum" client-reporting portal, then parses and
upserts it. Unlike the broker APIs this is a login-gated web portal with no
investor API, so we automate it the same way as the CAMS CAS pipeline:
Playwright (headed via Xvfb + stealth) drives the browser.

Flow per entity:
  1. Log in (username + password from .env — no OTP).
  2. Open the Portfolio page and parse the full holdings table — equity shares
     AND the Cash & Equivalent breakdown (this single table has everything,
     with weights and totals).
  3. Upsert into pms_holding (delete-then-insert per entity).
  4. Log out to free the license seat (WealthSpectrum allows one live session).

Optionally (NUVAMA_PMS_DOWNLOAD_FILE=1) the "Account Statement" report is also
downloaded from Reports → Report Executions for archival; it is not used for the
data (the report does not even include the cash balance) and adds session time.

Credentials live in .env as NUVAMA_PMS_{ENTITY}_{USERNAME,PASSWORD,OWNER}; see
the "Nuvama PMS" section there. Add a block per entity that holds a PMS account.

NOTE: The portal's report grid is rendered client-side and its exact selectors /
column layout can only be confirmed against a live login. Selectors below are
best-effort with fallbacks + screenshots at each step (saved under
_SCREENSHOT_DIR) for first-run tuning — mirroring cams_trigger_worker.py. The
parser captures and logs the real workbook schema on first run (see
parse_report) so the column mapping can be finalised against actual data
instead of guessed.

Run:  python workers/nuvama_pms_worker.py
Cron: invoke via workers/cron_wrapper.py like the other workers.
"""
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL      = os.environ.get(
    "NUVAMA_PMS_BASE_URL",
    "https://eclientreporting.nuvamaassetservices.com/wealthspectrum/app",
)
LOGIN_URL     = f"{BASE_URL.rstrip('/')}/login"
REPORT_NAME   = os.environ.get("NUVAMA_PMS_REPORT", "Account Statement")
OUTPUT_FORMAT = os.environ.get("NUVAMA_PMS_OUTPUT_FORMAT", "XLSX")
# Holdings + cash are read directly off the Portfolio page (single source). The
# Account Statement report is only downloaded if this is enabled (e.g. to keep
# the official file); it is not needed for the data and adds session time.
DOWNLOAD_REPORT_FILE = os.environ.get(
    "NUVAMA_PMS_DOWNLOAD_FILE", "").strip().lower() in ("1", "true", "yes")

# Entities that may hold a Nuvama PMS account: (entity_code, env_prefix).
# entity_code must match entity.entity_name in the DB; env_prefix follows the
# .env convention (Rajani Corp → RAJANIRCORP). Only entities whose USERNAME is
# actually set in .env are processed.
PMS_ENTITIES = [
    ("DHR", "DHR"),
    ("HHR", "HHR"),
    ("SDR", "SDR"),
]

DOWNLOAD_DIR        = Path("/var/www/mis-portal/.pms_downloads")
BROWSER_PROFILE_DIR = Path("/var/www/mis-portal/.nuvama_browser_profile")
SESSION_DIR         = Path("/var/www/mis-portal/.nuvama_sessions")
_SCREENSHOT_DIR     = "/home/SAdmin/.cams-screenshots"

# WealthSpectrum allows ONE live session per license, so every browser login
# claims (and risks locking) the single seat. To avoid logging in on every run
# we capture the cookies from the one browser login into a single
# requests.Session, persist them per entity, and reuse that session for the
# lightweight data fetches on later runs — re-logging-in through the browser
# only when the saved session has aged out / gone dead.
SESSION_MAX_AGE_S   = 30 * 60   # portal idles a session out in ~30 min

NAV_TIMEOUT      = 60_000
ACTION_TIMEOUT   = 15_000
EXECUTE_TIMEOUT  = 180_000   # report generation can take a while

# --- Partial-scrape guard ---------------------------------------------------
# A re-scrape full-replaces the entity's holdings (delete-then-insert), so a
# truncated Portfolio page — e.g. equities loaded but the Cash & Equivalent
# section slow/missing — would silently wipe real rows. Before committing we
# sanity-check the freshly parsed set and reject it (keeping the previous
# snapshot) when it looks partial.
PMS_REQUIRE_CASH = os.environ.get(
    "NUVAMA_PMS_REQUIRE_CASH", "1").strip().lower() in ("1", "true", "yes")
# Reject when the new total market value has collapsed against the last stored
# snapshot by more than this fraction. Legit withdrawals rarely breach it; a
# dropped section usually halves the total or worse. Set 0 to disable.
PMS_MAX_VALUE_DROP = Decimal(os.environ.get("NUVAMA_PMS_MAX_VALUE_DROP", "0.40"))

# --- Anti-fingerprint pacing (mirrors the CAMS CAS request cadence) ---------
# Each entity is a separate WealthSpectrum login, so hitting them in a fixed
# order at the same clock minute every night is a recognisably bot-like pattern.
# Same shape as cas_automation_worker: randomise the ORDER, jitter the start
# TIME, and wait a randomised DELAY between entities (the ceiling itself drawn
# 45–75 min, the delay drawn 30 min..ceiling). All knobs tunable in .env; set
# NUVAMA_PMS_RANDOMIZE=0 to run back-to-back with no waits (e.g. manual testing).
PMS_RANDOMIZE        = os.environ.get(
    "NUVAMA_PMS_RANDOMIZE", "1").strip().lower() in ("1", "true", "yes")
PMS_START_JITTER_S   = int(os.getenv("NUVAMA_PMS_START_JITTER_S",   str(20 * 60)))  # 0..20 min
PMS_DELAY_FLOOR_S    = int(os.getenv("NUVAMA_PMS_DELAY_FLOOR_S",    str(30 * 60)))  # 30 min
PMS_DELAY_CEIL_MIN_S = int(os.getenv("NUVAMA_PMS_DELAY_CEIL_MIN_S", str(45 * 60)))  # 45 min
PMS_DELAY_CEIL_MAX_S = int(os.getenv("NUVAMA_PMS_DELAY_CEIL_MAX_S", str(75 * 60)))  # 75 min

IST = timezone(timedelta(hours=5, minutes=30))   # for human-readable fire times

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
    code:     str   # entity_name in DB, e.g. "DHR"
    prefix:   str   # env prefix, e.g. "DHR"
    username: str
    password: str
    owner:    str   # value to pick in the report Scope→Owner dropdown


class PmsSchemaUnknown(Exception):
    """Raised when the downloaded report's column layout has not yet been mapped.
    The real schema is logged so the mapping can be implemented against it."""


class PmsPartialScrape(Exception):
    """Raised when a freshly parsed holdings set looks truncated/partial, so the
    previous snapshot is kept instead of being overwritten with bad data."""


# ---------------------------------------------------------------------------
# Env / DB helpers
# ---------------------------------------------------------------------------
def _env(prefix: str, key: str, required: bool = False) -> str:
    val = os.environ.get(f"NUVAMA_PMS_{prefix}_{key}", "")
    if required and not val:
        raise KeyError(f"NUVAMA_PMS_{prefix}_{key} not set in .env")
    return val


def load_configs() -> list[PmsConfig]:
    """Return a PmsConfig for every entity whose PMS username is set in .env."""
    configs = []
    for code, prefix in PMS_ENTITIES:
        username = _env(prefix, "USERNAME")
        if not username:
            continue
        configs.append(PmsConfig(
            code=code,
            prefix=prefix,
            username=username,
            password=_env(prefix, "PASSWORD", required=True),
            owner=_env(prefix, "OWNER") or code,
        ))
    return configs


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def now_utc():
    return datetime.now(timezone.utc)


def load_entity_id(conn, entity_name: str):
    cur = conn.cursor()
    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (entity_name,))
    row = cur.fetchone()
    cur.close()
    return row["id"] if row else None


def log_run(conn, status, processed, failed, started, error=None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ingestion_run
            (run_type, run_date, status, records_processed,
             records_failed, error_message, started_at, completed_at)
        VALUES ('nuvama_pms', %s, %s, %s, %s, %s, %s, %s)
        """,
        (date.today(), status, processed, failed, error, started, now_utc()),
    )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Playwright download
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Session persistence — log in once (browser), reuse via one requests.Session
# ---------------------------------------------------------------------------
import requests  # noqa: E402  (kept beside its only users)


def _session_file(cfg: PmsConfig) -> Path:
    return SESSION_DIR / f"{cfg.prefix}.session.json"


def _endpoints_file(cfg: PmsConfig) -> Path:
    return SESSION_DIR / f"{cfg.prefix}.endpoints.json"


def _save_browser_session(cfg: PmsConfig, ctx) -> None:
    """Persist the authenticated browser cookies + UA so later runs can reuse
    the session through requests instead of logging in again."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SESSION_DIR, 0o700)
        ua = ""
        try:
            if ctx.pages:
                ua = ctx.pages[0].evaluate("() => navigator.userAgent")
        except Exception:
            pass
        data = {
            "saved_at":   now_utc().isoformat(),
            "user_agent": ua,
            "cookies":    ctx.cookies(),
        }
        f = _session_file(cfg)
        f.write_text(json.dumps(data))
        os.chmod(f, 0o600)
        logger.info(f"[{cfg.code}] Saved session for reuse ({len(data['cookies'])} cookies)")
    except Exception as e:
        logger.info(f"[{cfg.code}] Could not save session: {e}")


def _build_requests_session(cfg: PmsConfig) -> "requests.Session | None":
    """Return a single requests.Session seeded with the saved auth cookies, or
    None if there is no saved session or it is too old to trust."""
    f = _session_file(cfg)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except Exception:
        return None
    try:
        age = (now_utc() - datetime.fromisoformat(data["saved_at"])).total_seconds()
    except Exception:
        age = SESSION_MAX_AGE_S + 1
    if age > SESSION_MAX_AGE_S:
        logger.info(f"[{cfg.code}] Saved session is {int(age)}s old — re-login needed")
        return None

    s = requests.Session()
    s.headers.update({
        "User-Agent":       data.get("user_agent") or _USER_AGENTS[0],
        "X-Requested-With": "XMLHttpRequest",
        "Accept":           "application/json, text/plain, */*",
        "Referer":          BASE_URL.rstrip("/") + "/",
    })
    for c in data.get("cookies", []):
        try:
            s.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", "").lstrip("."),
                path=c.get("path", "/"),
            )
        except Exception:
            pass
    return s


def _session_alive(sess: "requests.Session", cfg: PmsConfig) -> bool:
    """Cheap probe: hit the app root and confirm the saved cookies still
    authenticate (i.e. we are NOT bounced back to the login page)."""
    try:
        r = sess.get(BASE_URL.rstrip("/") + "/", timeout=20, allow_redirects=True)
    except Exception as e:
        logger.info(f"[{cfg.code}] Session probe failed: {e}")
        return False
    blob = (r.url + " " + r.text[:3000]).lower()
    if ("login" in r.url.lower()
            or "login to your account" in blob
            or "invalid login as per license" in blob):
        logger.info(f"[{cfg.code}] Saved session no longer authenticated")
        return False
    return True


def _rows_from_portfolio_json(payload) -> list[dict]:
    """Best-effort map of a captured portfolio JSON payload to holding rows.

    The portal's exact JSON shape can only be confirmed from a live capture, so
    this walks the payload for the first list of holding-like dicts and maps by
    fuzzy field names. It logs the keys it sees so the mapping can be finalised
    against the real response. Returns [] (→ caller falls back to the browser
    DOM parse) if it cannot confidently map."""
    def _find_records(node):
        if isinstance(node, list):
            if node and isinstance(node[0], dict):
                return node
            for it in node:
                r = _find_records(it)
                if r:
                    return r
        elif isinstance(node, dict):
            for v in node.values():
                r = _find_records(v)
                if r:
                    return r
        return []

    records = _find_records(payload)
    if not records:
        return []

    keys = sorted(records[0].keys())
    logger.info(f"Portfolio JSON record keys: {keys}")

    def pick(d, *cands):
        low = {k.lower(): k for k in d}
        for c in cands:
            for lk, orig in low.items():
                if c in lk:
                    return d[orig]
        return None

    rows: list[dict] = []
    for d in records:
        name = pick(d, "securityname", "scripname", "instrument", "description", "scrip", "name")
        mval = pick(d, "marketvalue", "mktval", "marketval", "currentvalue", "closingvalue", "value")
        if not name or mval is None:
            continue
        nm = str(name).strip().lower()
        if nm in _PF_SKIP_LABELS or nm in _PF_EQUITY_LABELS or nm in _PF_CASH_LABELS:
            continue
        is_cash = pick(d, "assetclass", "category", "type")
        bucket = "cash" if (is_cash and "cash" in str(is_cash).lower()) else "equity"
        units = _num(pick(d, "units", "quantity", "qty", "holding"))
        cost  = _num(pick(d, "totalcost", "cost", "investedvalue", "purchasevalue"))
        rows.append({
            "holding_type":  bucket,
            "security_name": str(name).strip(),
            "isin":          pick(d, "isin"),
            "quantity":      units,
            "avg_cost":      (cost / units) if (cost and units) else None,
            "cost":          cost,
            "current_price": _num(pick(d, "marketprice", "mktprice", "nav", "rate", "price")),
            "market_value":  _num(mval),
            "weight_pct":    _num(pick(d, "weight", "assets", "allocation")),
        })
    return rows


def _http_portfolio(sess: "requests.Session", cfg: PmsConfig) -> list[dict] | None:
    """Fetch holdings via the saved requests.Session using the JSON data
    endpoint(s) captured during a previous browser run. Returns rows, or None
    when no endpoint is known yet / nothing parseable (→ browser fallback)."""
    f = _endpoints_file(cfg)
    if not f.exists():
        logger.info(f"[{cfg.code}] No captured portfolio endpoint yet — cannot fetch via requests")
        return None
    try:
        endpoints = json.loads(f.read_text())
    except Exception:
        return None
    for url in endpoints:
        try:
            r = sess.get(url, timeout=30)
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                rows = _rows_from_portfolio_json(r.json())
                if rows:
                    logger.info(f"[{cfg.code}] Fetched {len(rows)} holdings via requests "
                                f"({url.split('?')[0]})")
                    return rows
        except Exception as e:
            logger.info(f"[{cfg.code}] requests fetch failed for {url.split('?')[0]}: {e}")
    return None


def _capture_portfolio_endpoints(page, cfg: PmsConfig) -> None:
    """Record the XHR/JSON responses the Portfolio page fires, so subsequent
    runs can pull the same data through requests. Saves candidate endpoint URLs
    and a sample body for finalising the JSON mapping. Best-effort; attach
    BEFORE navigating to Portfolio."""
    captured: list[str] = []
    samples: list[dict] = []

    def _on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            url = resp.url
            if not any(k in url.lower() for k in ("portfolio", "holding", "valuation", "asset", "scheme")):
                return
            body = resp.json()
            if url not in captured:
                captured.append(url)
                samples.append({"url": url, "sample": body})
        except Exception:
            pass

    page.on("response", _on_response)

    def _persist():
        if not captured:
            return
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(SESSION_DIR, 0o700)
            _endpoints_file(cfg).write_text(json.dumps(captured, indent=2))
            os.chmod(_endpoints_file(cfg), 0o600)
            sf = SESSION_DIR / f"{cfg.prefix}.endpoint-sample.json"
            sf.write_text(json.dumps(samples, indent=2)[:200_000])
            os.chmod(sf, 0o600)
            logger.info(f"[{cfg.code}] Captured {len(captured)} portfolio endpoint(s) for requests reuse")
        except Exception as e:
            logger.info(f"[{cfg.code}] Could not persist captured endpoints: {e}")

    page._iws_persist_endpoints = _persist  # called by _parse_portfolio after load


def download_report(cfg: PmsConfig, as_on: date) -> tuple[Path | None, list[dict]]:
    """Log in, parse the Portfolio holdings, (optionally) download the report.

    Returns (report_path | None, holdings_rows). Holdings (equity + cash) come
    from the Portfolio page; the Account Statement file is downloaded only when
    DOWNLOAD_REPORT_FILE is set. Always logs out at the end to free the seat.

    Reuse-first: if a recent authenticated session is saved on disk and its data
    endpoint is known, the holdings are fetched through a single requests.Session
    with NO browser and NO login. Only when that is unavailable/expired do we
    spin up the browser, log in once, parse the DOM, and (re)save the session."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from playwright_stealth import Stealth
    from pyvirtualdisplay import Display

    # --- Reuse the saved session via requests (no login) when possible --------
    if not DOWNLOAD_REPORT_FILE:   # report download still needs the browser
        sess = _build_requests_session(cfg)
        if sess is not None and _session_alive(sess, cfg):
            rows = _http_portfolio(sess, cfg)
            if rows:
                logger.info(f"[{cfg.code}] Reused saved session — skipped browser login")
                return None, rows
            logger.info(f"[{cfg.code}] Saved session live but no usable data endpoint yet "
                        f"— falling back to browser this run")

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DOWNLOAD_DIR, 0o700)
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = BROWSER_PROFILE_DIR / stale
        if p.exists() or p.is_symlink():
            p.unlink()

    display = Display(visible=False, size=(1440, 900))
    display.start()
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,
                accept_downloads=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1440, "height": 900},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()
            page.on("dialog", lambda d: d.accept())  # auto-OK any Execute alerts
            Stealth().apply_stealth_sync(page)
            try:
                _login(page, cfg)
                # Capture the Portfolio data endpoint during this one good login
                # so future runs can fetch via requests (reuse-first above).
                _capture_portfolio_endpoints(page, cfg)
                rows = _parse_portfolio(page, cfg)
                # Persist this authenticated session into one reusable bundle.
                _save_browser_session(cfg, ctx)
                path = None
                if DOWNLOAD_REPORT_FILE:
                    _open_reports(page)
                    _fill_report_form(page, cfg, as_on)
                    path = _execute_and_download(page, cfg)
                    logger.info(f"[{cfg.code}] Report downloaded: {path.name}")
                return path, rows
            except PWTimeout as e:
                _shot(page, f"pms_timeout_{cfg.prefix}.png")
                raise RuntimeError(f"[{cfg.code}] Portal timeout: {e}") from e
            finally:
                try:
                    _logout(page)
                except Exception:
                    pass
                ctx.close()
    finally:
        display.stop()


def _login(page, cfg: PmsConfig):
    logger.info(f"[{cfg.code}] Logging in to WealthSpectrum")
    page.goto(LOGIN_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(1200, 2200))
    _shot(page, f"pms_login_{cfg.prefix}.png")

    _fill(page, [
        'input[name="username"]', 'input[name="userId"]', 'input[name="loginId"]',
        'input[id*="user" i]', 'input[placeholder*="User" i]',
        'input[type="text"]',
    ], cfg.username, "username")
    _fill(page, [
        'input[name="password"]', 'input[id*="pass" i]',
        'input[placeholder*="Password" i]', 'input[type="password"]',
    ], cfg.password, "password")
    page.wait_for_timeout(random.randint(300, 700))

    _click(page, [
        'button[type="submit"]', 'button:has-text("Login")', 'button:has-text("Sign In")',
        'input[type="submit"]', 'a:has-text("Login")',
    ], "login button")

    # Fast-fail: a rejected login re-renders the login page with the red
    # "Invalid login as per license." banner almost immediately. Catch it now
    # instead of waiting out the full Dashboard timeout (~2 min) — that hang
    # wastes time and keeps poking the single-session license seat.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT)
    except Exception:
        pass
    page.wait_for_timeout(1000)
    if _has_text(page, "invalid login as per license"):
        _shot(page, f"pms_postlogin_{cfg.prefix}.png")
        raise RuntimeError(
            f"[{cfg.code}] Login blocked: 'Invalid login as per license'. "
            f"WealthSpectrum allows only one live session — wait for the existing "
            f"session to time out, or ensure no one else is logged in to this account."
        )

    # Logged in when the left-nav chrome appears (login form gone).
    try:
        page.get_by_text("Dashboard", exact=False).first.wait_for(
            state="visible", timeout=NAV_TIMEOUT
        )
    except Exception:
        page.wait_for_timeout(4000)
    _shot(page, f"pms_postlogin_{cfg.prefix}.png")
    if "login" in page.url.lower():
        if _has_text(page, "license"):
            raise RuntimeError(
                f"[{cfg.code}] Login blocked: 'Invalid login as per license'. "
                f"WealthSpectrum allows only one live session — wait for the existing "
                f"session to time out, or ensure no one else is logged in to this account."
            )
        if _has_text(page, "invalid", "incorrect", "failed"):
            raise RuntimeError(f"[{cfg.code}] Login appears to have failed — check credentials")


def _logout(page):
    """Log out to release the WealthSpectrum license seat (single live session)."""
    try:
        # Open the top-right account menu, then click Logout.
        try:
            page.locator('[data-toggle="dropdown"], .dropdown-toggle').last.click(timeout=3000)
            page.wait_for_timeout(500)
        except Exception:
            pass
        page.locator(
            'a:has-text("Logout"), a:has-text("Sign Out"), [href*="logout" i]'
        ).first.click(timeout=5000, force=True)
        page.wait_for_timeout(1500)
        logger.info("Logged out (freed the license session)")
    except Exception as e:
        logger.info(f"Logout skipped: {e}")


def _open_reports(page):
    _click(page, [
        'a:has-text("Reports")', 'span:has-text("Reports")',
        '[href*="report" i]', 'li:has-text("Reports")',
    ], "Reports nav")
    page.wait_for_timeout(random.randint(1500, 2500))
    _shot(page, "pms_reports_page.png")


def _fill_report_form(page, cfg: PmsConfig, as_on: date):
    # Report — a custom dropdown (caret + favourite star) backed by a hidden
    # native <select>. The dedicated picker exact-matches "Account Statement"
    # (not "Account Statement - Non unitized").
    _select_report(page, REPORT_NAME)
    # Selecting the report re-renders the form with that report's own criteria
    # (Account Statement adds "DrawDown Summary Required" etc.); let it settle.
    page.wait_for_timeout(1500)
    # As on Date — left at the portal default, which is the latest valuation
    # date (what a daily sync wants). The visible field is #toDateUI if a
    # specific date is ever required.
    # Scope → Owner auto-defaults to the logged-in account holder — left as-is.
    # Output format — XLSX for reliable parsing. Criteria field ids are numbered
    # dynamically per report, so match the <select> by its options instead.
    _select_output_format(page, OUTPUT_FORMAT)
    page.wait_for_timeout(random.randint(400, 800))
    _shot(page, f"pms_form_{cfg.prefix}.png")


def _execute_and_download(page, cfg: PmsConfig) -> Path:
    """Execute the report, then download it from the Report Executions tab.

    The portal queues the report and lists it in the Report Executions grid
    (columns: Report Name | Scope | Time | Status). The newest run is the TOP
    data row, and its "Status" cell holds a clickable download icon — there is
    no "Download" label or "complete/ready" status text. We poll the top row,
    clicking its status icon until it yields a file.
    """
    _click(page, [
        'button:has-text("Execute")', 'input[type="submit"][value*="Execute" i]',
        'button[type="submit"]',
    ], "Execute")
    # Execute does a full server-side form POST that reloads the Reports page;
    # wait for the reload to finish before the tabs reappear in the DOM.
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
    except Exception:
        page.wait_for_timeout(3000)
    try:
        page.wait_for_selector('text="Report Executions"', timeout=NAV_TIMEOUT)
    except Exception:
        page.wait_for_timeout(2000)

    _click(page, [
        'a:has-text("Report Executions")', 'li:has-text("Report Executions")',
        'text="Report Executions"',
    ], "Report Executions tab")
    page.wait_for_timeout(2000)

    waited = 0
    poll = 5000
    while waited < EXECUTE_TIMEOUT:
        _shot(page, f"pms_executions_{cfg.prefix}.png")
        # Newest execution = first data row. "tr:has(td)" skips the <th> header
        # row; filtering by owner keeps us on this account's rows if present.
        rows = page.locator('table tr:has(td)')
        if cfg.owner and rows.filter(has_text=cfg.owner).count() > 0:
            row = rows.filter(has_text=cfg.owner).first
        else:
            row = rows.first
        if row.count() > 0:
            icon = row.locator('td').last.locator(
                'a, button, i, img, span[role="button"], [class*="download" i]'
            ).first
            if icon.count() > 0:
                try:
                    with page.expect_download(timeout=ACTION_TIMEOUT) as dl_info:
                        icon.click(force=True)
                    return _save_download(dl_info.value, cfg)
                except Exception as e:
                    logger.info(f"[{cfg.code}] Top row not ready to download yet: {e}")
        page.wait_for_timeout(poll)
        # No explicit refresh control — re-open the tab to refresh the grid.
        try:
            _click(page, [
                'a:has-text("Report Executions")', 'text="Report Executions"',
            ], "refresh executions")
            page.wait_for_timeout(1000)
        except Exception:
            pass
        waited += poll
    raise RuntimeError(f"[{cfg.code}] Report did not finish/download within timeout")


def _save_download(download, cfg: PmsConfig) -> Path:
    suggested = download.suggested_filename or f"pms_{cfg.prefix}.xlsx"
    dest = DOWNLOAD_DIR / f"{cfg.prefix}_{date.today():%Y%m%d}_{suggested}"
    download.save_as(str(dest))
    os.chmod(dest, 0o600)
    return dest


def _num(s) -> Decimal | None:
    """Parse a portfolio number cell ('1,234.56', '40.78%', '', '-') to Decimal."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("%", "").replace("₹", "")
    if t in ("", "-", "--", "NA", "N/A"):
        return None
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


# Portfolio table rows that are section headers / subtotals, not holdings.
_PF_EQUITY_LABELS = {"equity"}
_PF_CASH_LABELS   = {"cash and equivalent", "cash & equivalent"}
_PF_SKIP_LABELS   = {
    "total", "shares", "mutual funds", "cash / bank", "cash/bank",
    "fixed income", "bonds", "debt", "etf", "exchange traded funds",
    "alternates", "alternatives", "description",
}


def _rows_from_grid(grid: list[list[str]]) -> list[dict]:
    """Pure parser: turn the Portfolio table cell-grid into holding rows.

    Kept separate from the DOM fetch so it is unit-testable. See _parse_portfolio
    for the column contract. Handles holdings whose name renders on its own row
    above the numbers row (pending_name)."""
    rows: list[dict] = []
    bucket = None
    pending_name = None
    for cells in grid:
        nonempty = [c for c in cells if c]
        if not nonempty:
            continue
        first = (cells[0] if cells else "").strip()
        label = first.lower()

        # A lone text cell = a section label or a holding name on its own row.
        if len(nonempty) == 1 and _num(nonempty[0]) is None:
            tl = nonempty[0].strip().lower()
            if tl in _PF_EQUITY_LABELS:
                bucket = "equity"
            elif tl in _PF_CASH_LABELS:
                bucket = "cash"
            elif tl in _PF_SKIP_LABELS:
                pass
            else:
                pending_name = nonempty[0].strip()
            continue

        # Section/subtotal rows that also carry aggregate numbers.
        if label in _PF_EQUITY_LABELS:
            bucket = "equity"; continue
        if label in _PF_CASH_LABELS:
            bucket = "cash"; continue
        if label in _PF_SKIP_LABELS:
            continue

        # Data row — align to the 9-column header.
        vals = list(cells) + [""] * (9 - len(cells))
        market_value = _num(vals[4])
        if market_value is None or bucket is None:
            continue
        name = first or pending_name
        pending_name = None
        if not name:
            continue
        units = _num(vals[1])
        cost  = _num(vals[2])
        price = _num(vals[3])
        rows.append({
            "holding_type":  bucket,
            "security_name": name,
            "isin":          None,
            "quantity":      units,
            "avg_cost":      (cost / units) if (cost and units) else None,
            "cost":          cost,
            "current_price": price,
            "market_value":  market_value,
            "weight_pct":    _num(vals[8]),
        })
    return rows


def _parse_portfolio(page, cfg: PmsConfig) -> list[dict]:
    """Open the Portfolio page and parse the full holdings table.

    The table has one row per holding plus section/subtotal rows. Top-level
    sections set the bucket: "Equity" -> equity, "Cash and Equivalent" -> cash
    (covering its Mutual Funds + Cash/Bank sub-rows). Columns are:
      [Description, Units, Total Cost, Mkt Price, Mkt Val+AI, Income,
       Total G/L, % G/L, % Assets]
    Some holdings render the name on its own row above the numbers row, so a
    lone text row is held as a pending name for the following numbers row.
    """
    _click(page, [
        'a:has-text("Portfolio")', 'span:has-text("Portfolio")',
        'li:has-text("Portfolio")', '[href*="portfolio" i]',
    ], "Portfolio nav")
    page.wait_for_timeout(3000)
    _shot(page, f"pms_portfolio_{cfg.prefix}.png")
    # Persist any XHR data endpoints seen while Portfolio loaded (for requests reuse).
    persist = getattr(page, "_iws_persist_endpoints", None)
    if persist:
        persist()

    grid = page.evaluate("""() => {
      const tables = Array.from(document.querySelectorAll('table'));
      let target = null;
      for (const t of tables) {
        const h = t.innerText || '';
        if (h.includes('Description') && h.includes('Mkt Val')) { target = t; break; }
      }
      if (!target) return [];
      return Array.from(target.querySelectorAll('tr')).map(tr =>
        Array.from(tr.querySelectorAll('td,th'))
          .map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim()));
    }""")

    rows = _rows_from_grid(grid)

    eq = sum(r["market_value"] for r in rows if r["holding_type"] == "equity")
    ca = sum(r["market_value"] for r in rows if r["holding_type"] == "cash")
    logger.info(
        f"[{cfg.code}] Parsed {len(rows)} portfolio holdings "
        f"(equity={sum(1 for r in rows if r['holding_type']=='equity')} = {eq:,.0f}, "
        f"cash={sum(1 for r in rows if r['holding_type']=='cash')} = {ca:,.0f})"
    )
    if not rows:
        grid_dump = "\n".join(f"  {i}: {r}" for i, r in enumerate(grid[:60]))
        logger.warning(f"[{cfg.code}] Parsed 0 holdings — raw grid:\n{grid_dump}")
    return rows


# ---------------------------------------------------------------------------
# Parsing — schema-capture first
# ---------------------------------------------------------------------------
# Flip to True once the column mapping below is implemented against a real file.
PMS_SCHEMA_READY = False


def parse_report(path: Path, cfg: PmsConfig) -> list[dict]:
    """Parse the downloaded report into normalised line-items.

    On first run (PMS_SCHEMA_READY == False) this logs the actual workbook
    sheets + header rows and raises PmsSchemaUnknown, so the real layout is
    captured in the run log instead of guessed. Once the columns are known,
    implement the mapping below and set PMS_SCHEMA_READY = True.
    """
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        schema = _dump_xlsx_schema(path)
    elif suffix == ".pdf":
        schema = _dump_pdf_schema(path)
    else:
        schema = f"(unrecognised file type: {suffix})"

    if not PMS_SCHEMA_READY:
        logger.warning(
            f"[{cfg.code}] PMS report schema not yet mapped. Captured layout below "
            f"— implement the mapping in parse_report() and set PMS_SCHEMA_READY=True.\n{schema}"
        )
        raise PmsSchemaUnknown(f"schema captured for {path.name}; mapping not implemented")

    # TODO(once schema known): map each report row to the contract below and
    # return the list. upsert() persists it into pms_holding.
    #   {
    #     "holding_type":  "equity" | "cash",   # required
    #     "security_name": str,                  # required
    #     "isin":          str | None,
    #     "quantity":      Decimal | None,
    #     "avg_cost":      Decimal | None,
    #     "cost":          Decimal | None,
    #     "current_price": Decimal | None,
    #     "market_value":  Decimal,              # required (equity value or cash balance)
    #     "weight_pct":    Decimal | None,
    #   }
    raise NotImplementedError("parse_report mapping pending real schema")


def _dump_xlsx_schema(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"  sheet '{ws.title}':")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 30:
                out.append("    ...")
                break
            cells = [str(c) for c in row if c is not None][:14]
            out.append(f"    r{i}: {cells}")
    wb.close()
    return "\n".join(out)


def _dump_pdf_schema(path: Path) -> str:
    import fitz
    doc = fitz.open(path)
    text = doc[0].get_text()[:1500] if doc.page_count else ""
    doc.close()
    return f"  PDF page 1 text (first 1500 chars):\n{text}"


def _prev_snapshot_total(conn, entity_id: int) -> Decimal | None:
    """Total market value of this entity's most recent stored PMS snapshot, or
    None when there is no prior snapshot to compare a new scrape against."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(SUM(market_value), 0) AS total
          FROM pms_holding
         WHERE entity_id = %s
           AND as_on_date = (
               SELECT MAX(as_on_date) FROM pms_holding WHERE entity_id = %s
           )
        """,
        (entity_id, entity_id),
    )
    row = cur.fetchone()
    cur.close()
    # Connection uses RealDictCursor, so rows are dicts — read by column name.
    total_raw = row["total"] if row else None
    if total_raw is None:
        return None
    total = Decimal(total_raw)
    return total if total > 0 else None


def _validate_scrape(conn, entity_id: int, rows: list[dict], cfg: PmsConfig) -> None:
    """Reject a partial/truncated Portfolio scrape before it overwrites good data.

    Raises PmsPartialScrape when the parsed set is missing a structural section
    or its total value has collapsed against the last stored snapshot; the caller
    then keeps the previous holdings. Run this BEFORE upsert (delete-then-insert)
    so the comparison still sees the prior snapshot."""
    n_cash   = sum(1 for r in rows if r["holding_type"] == "cash")
    n_equity = sum(1 for r in rows if r["holding_type"] == "equity")

    # Structural: the Portfolio page always carries a Cash & Equivalent line
    # (even at ~0), so its absence means that section did not render this run.
    if PMS_REQUIRE_CASH and n_cash == 0:
        raise PmsPartialScrape(
            f"no cash row parsed (equity rows={n_equity}) — Cash & Equivalent "
            f"section likely did not load; keeping previous snapshot"
        )

    # Value collapse vs the last good snapshot catches a missing equity section
    # (cash present, securities dropped) that the structural check cannot see.
    if PMS_MAX_VALUE_DROP > 0:
        prev = _prev_snapshot_total(conn, entity_id)
        if prev is not None:
            new_total = sum((r.get("market_value") or Decimal(0)) for r in rows)
            floor = prev * (Decimal(1) - PMS_MAX_VALUE_DROP)
            if new_total < floor:
                drop = (Decimal(1) - new_total / prev) * 100
                raise PmsPartialScrape(
                    f"total value {new_total:,.0f} is {drop:.0f}% below the last "
                    f"snapshot {prev:,.0f} (limit {PMS_MAX_VALUE_DROP * 100:.0f}%) "
                    f"— likely a partial scrape; keeping previous snapshot"
                )


def upsert(conn, entity_id: int, rows: list[dict], cfg: PmsConfig,
           as_on: date | None = None) -> int:
    """Full-replace this entity's PMS holdings with the freshly parsed set.

    Delete-then-insert (in one transaction) so positions sold out of the PMS
    drop off instead of lingering. Each row must satisfy the parse_report()
    contract; holding_type tags equity vs cash for the per-bucket totals.
    """
    as_on = as_on or date.today()
    cur = conn.cursor()
    cur.execute("DELETE FROM pms_holding WHERE entity_id = %s", (entity_id,))
    count = 0
    for r in rows:
        cur.execute(
            """
            INSERT INTO pms_holding
                (entity_id, as_on_date, holding_type, security_name, isin,
                 quantity, avg_cost, cost, current_price, market_value,
                 weight_pct, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'nuvama_pms', %s)
            """,
            (
                entity_id, as_on,
                r["holding_type"], r["security_name"], r.get("isin"),
                r.get("quantity"), r.get("avg_cost"), r.get("cost"),
                r.get("current_price"), r["market_value"], r.get("weight_pct"),
                now_utc(),
            ),
        )
        count += 1
    cur.close()
    return count


# ---------------------------------------------------------------------------
# Resilient Playwright helpers
# ---------------------------------------------------------------------------
def _fill(page, selectors, value, label, clear=False, optional=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=ACTION_TIMEOUT)
            loc.click(force=True)
            if clear:
                page.keyboard.press("Control+a")
                page.keyboard.press("Delete")
            page.wait_for_timeout(random.randint(120, 300))
            loc.type(value, delay=random.randint(35, 80))
            logger.debug(f"Filled {label} via {sel}")
            return
        except Exception:
            continue
    _dump_inputs(page, label)
    msg = f"Could not find '{label}' field. Tried: {selectors}"
    if optional:
        logger.warning(msg + " — leaving portal default")
        return
    raise RuntimeError(msg)


def _click(page, selectors, label):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=ACTION_TIMEOUT)
            loc.click(force=True)
            logger.debug(f"Clicked {label} via {sel}")
            return
        except Exception:
            continue
    raise RuntimeError(f"Could not click '{label}'. Tried: {selectors}")


def _select(page, selectors, value, label, optional=False):
    """Select a value from a native <select> or a custom dropdown."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            tag = (loc.evaluate("el => el.tagName") or "").lower()
            if tag == "select":
                try:
                    loc.select_option(label=value, timeout=ACTION_TIMEOUT)
                except Exception:
                    loc.select_option(value=value, timeout=ACTION_TIMEOUT)
            else:
                loc.click(force=True)
                page.wait_for_timeout(400)
                # If the dropdown exposes a search box, type to filter options.
                try:
                    search = page.locator(
                        '.select2-search__field, input.ng-input, '
                        'input[type="search"]:visible'
                    ).first
                    if search.count() > 0:
                        search.fill(value, timeout=2000)
                        page.wait_for_timeout(400)
                except Exception:
                    pass
                # Quoted text = exact match → avoids partial-name collisions.
                page.locator(f'text="{value}"').first.click(timeout=ACTION_TIMEOUT, force=True)
            logger.debug(f"Selected {label}={value} via {sel}")
            return
        except Exception:
            continue
    msg = f"Could not select '{label}'={value}. Tried: {selectors}"
    if optional:
        logger.warning(msg + " — leaving portal default")
        return
    raise RuntimeError(msg)


def _select_report(page, report_name: str):
    """Select a report from the WealthSpectrum 'Report' picker.

    The picker is a custom dropdown (caret + favourite star), not a native
    <select>: open it (several strategies), filter via a search box if present,
    then click the EXACT option (:text-is avoids the "Account Statement - Non
    unitized" collision). Dumps the dropdown DOM on failure for selector tuning.
    """
    # 1) Native <select> fallback, in case a hidden one backs the widget.
    try:
        sel = page.locator('select').filter(
            has=page.locator(f'option:text-is("{report_name}")')
        ).first
        if sel.count() > 0:
            sel.select_option(label=report_name, timeout=ACTION_TIMEOUT)
            logger.info("Selected Report via native <select>")
            return
    except Exception:
        pass

    # 2) Open the dropdown — try the field next to the "Report" label, then
    #    common widget containers.
    open_selectors = [
        'xpath=//label[normalize-space()="Report"]/following::*'
        '[self::div or self::span or self::button][1]',
        'button.dropdown-toggle', '.bootstrap-select', '[role="combobox"]',
        '.ng-select-container', '.select2-selection',
    ]
    opened = False
    for sel in open_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.click(force=True, timeout=ACTION_TIMEOUT)
            opened = True
            logger.debug(f"Opened Report dropdown via {sel}")
            break
        except Exception:
            continue
    if not opened:
        _dump_dropdowns(page, "Report")
        raise RuntimeError(f"Could not open the Report dropdown for '{report_name}'")

    page.wait_for_timeout(500)
    # 3) Filter via a search box if the widget has one.
    try:
        search = page.locator(
            '.bootstrap-select .bs-searchbox input, .select2-search__field, '
            'input.ng-input, input[type="search"]:visible'
        ).first
        if search.count() > 0:
            search.fill(report_name, timeout=2000)
            page.wait_for_timeout(400)
    except Exception:
        pass

    # 4) Click the exact option.
    option_selectors = [
        f'.dropdown-menu li a:text-is("{report_name}")',
        f'li:text-is("{report_name}")',
        f'[role="option"]:text-is("{report_name}")',
        f'.dropdown-item:text-is("{report_name}")',
        f':text-is("{report_name}")',
    ]
    for sel in option_selectors:
        try:
            opt = page.locator(sel).first
            if opt.count() == 0:
                continue
            opt.click(force=True, timeout=ACTION_TIMEOUT)
            logger.info(f"Selected Report='{report_name}' via {sel}")
            return
        except Exception:
            continue
    _dump_dropdowns(page, "Report")
    raise RuntimeError(f"Opened dropdown but could not click option '{report_name}'")


def _select_output_format(page, fmt: str):
    """Pick the output format by locating the <select> that offers it.

    Field ids differ per report (criteria are numbered dynamically), so we match
    on option text (PDF/XLS/CSV/RTF/PPTX/DOCX/XLSX) rather than an id."""
    try:
        sel = page.locator('select').filter(
            has=page.locator(f'option:text-is("{fmt}")')
        ).first
        if sel.count() > 0:
            sel.select_option(label=fmt, timeout=ACTION_TIMEOUT)
            logger.info(f"Output Format set to {fmt}")
            return
    except Exception as e:
        logger.warning(f"Output Format '{fmt}' select failed: {e}")
    logger.warning(f"Output Format '{fmt}' not available — leaving portal default")


def _dump_dropdowns(page, label):
    try:
        dump = page.evaluate("""() => {
          const pick = el => ({tag: el.tagName, cls: el.className, id: el.id,
            role: el.getAttribute('role'), name: el.getAttribute('name'),
            fcn: el.getAttribute('formcontrolname'),
            text: (el.innerText||'').trim().slice(0,40)});
          const selects = Array.from(document.querySelectorAll('select')).map(s => ({
            ...pick(s), options: Array.from(s.options).slice(0,12).map(o=>o.text)}));
          const widgets = Array.from(document.querySelectorAll(
            'button.dropdown-toggle,[role=combobox],[role=listbox],.bootstrap-select,'
            +'.ng-select,.select2,.dropdown-toggle')).slice(0,25).map(pick);
          return {selects, widgets};
        }""")
        logger.warning(f"Dropdown DOM dump for '{label}': {dump}")
    except Exception as e:
        logger.warning(f"DOM dump failed for '{label}': {e}")


def _has_text(page, *needles) -> bool:
    try:
        content = page.content().lower()
        return any(n.lower() in content for n in needles)
    except Exception:
        return False


def _dump_inputs(page, label):
    try:
        dump = page.evaluate("""() => Array.from(document.querySelectorAll('input,select,button'))
            .map(el => ({tag: el.tagName, type: el.type, name: el.name, id: el.id,
                         placeholder: el.placeholder, text: (el.innerText||'').slice(0,30),
                         visible: el.offsetParent !== null})).slice(0, 60)""")
        logger.warning(f"DOM control dump for '{label}': {dump}")
    except Exception:
        pass


def _shot(page, filename):
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        os.chmod(_SCREENSHOT_DIR, 0o700)
        path = f"{_SCREENSHOT_DIR}/{filename}"
        page.screenshot(path=path, timeout=15_000)
        os.chmod(path, 0o600)
        logger.info(f"Screenshot saved: {filename}")
    except Exception as e:
        logger.warning(f"Screenshot failed ({filename}): {e}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
LOCK_FILE = Path("/tmp/nuvama_pms.lock")


def _acquire_lock() -> bool:
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip())
            try:
                os.kill(pid, 0)
                logger.error(f"Another Nuvama PMS run is active (PID {pid}). Exiting.")
                return False
            except (ProcessLookupError, PermissionError):
                logger.warning("Stale lock — removing and continuing.")
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except Exception as e:
        logger.warning(f"Lock error: {e} — proceeding")
        return True


def run():
    started = now_utc()
    configs = load_configs()
    if not configs:
        logger.warning("No Nuvama PMS entities configured in .env — nothing to do.")
        return

    as_on = date.today()
    conn = get_db()
    processed = failed = 0
    errors = []

    # Anti-fingerprint pacing: randomise the order, jitter the start, and (below)
    # space the entities out by a randomised delay — the same cadence the CAMS
    # CAS pipeline uses so the portal never sees a fixed order/time pattern.
    if PMS_RANDOMIZE and len(configs) > 1:
        random.shuffle(configs)
    logger.info(f"PMS sync order: {' → '.join(c.code for c in configs)}")
    if PMS_RANDOMIZE and PMS_START_JITTER_S > 0:
        jitter = random.uniform(0, PMS_START_JITTER_S)
        fires = datetime.now(IST) + timedelta(seconds=jitter)
        logger.info(f"Start jitter: waiting {jitter / 60:.0f}m "
                    f"(first entity ~{fires:%H:%M IST})")
        time.sleep(jitter)

    try:
        for idx, cfg in enumerate(configs):
            if idx > 0 and PMS_RANDOMIZE:
                # Ceiling drawn 45–75 min, then the delay drawn 30 min..ceiling.
                ceil_s  = random.uniform(PMS_DELAY_CEIL_MIN_S, PMS_DELAY_CEIL_MAX_S)
                delay_s = random.uniform(PMS_DELAY_FLOOR_S, ceil_s)
                fires   = datetime.now(IST) + timedelta(seconds=delay_s)
                logger.info(
                    f"Next sync [{cfg.code}] in {delay_s / 60:.0f}m "
                    f"(ceil {ceil_s / 60:.0f}m, fires ~{fires:%H:%M IST})"
                )
                time.sleep(delay_s)

            logger.info(f"=== [{cfg.code}] Nuvama PMS sync ===")
            path = None
            try:
                entity_id = load_entity_id(conn, cfg.code)
                if entity_id is None:
                    raise RuntimeError(f"Entity '{cfg.code}' not found in DB")
                path, rows = download_report(cfg, as_on)
                if not rows:
                    raise RuntimeError("no holdings parsed from Portfolio page")
                _validate_scrape(conn, entity_id, rows, cfg)
                n = upsert(conn, entity_id, rows, cfg, as_on)
                conn.commit()
                processed += n
                logger.info(f"[{cfg.code}] Upserted {n} PMS line-item(s)")
            except Exception as e:
                conn.rollback()
                failed += 1
                errors.append(f"{cfg.code}: {e}")
                logger.error(f"[{cfg.code}] Failed: {e}")
            finally:
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        status = "success" if failed == 0 else ("partial" if processed else "failed")
        log_run(conn, status, processed, failed, started,
                error="; ".join(errors) if errors else None)
        logger.info(f"=== Nuvama PMS done: {processed} upserted, {failed} failed ===")
    finally:
        conn.close()


def main():
    if not _acquire_lock():
        sys.exit(1)
    try:
        run()
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
