#!/usr/bin/env python3
"""
Shared portal-scraper toolkit — IWS MIS Portal.

Common machinery for the login-gated broker portals that have no investor API
and must be driven through a browser, the same way the Nuvama PMS worker is:
Playwright (headed via Xvfb + stealth) logs in, the data is read off the pages
or a downloaded statement, and a lightweight requests.Session is reused on later
runs so we are not logging in (and claiming a license seat) every night.

Used by workers/vested_worker.py and workers/dbs_worker.py. Portal-specific
flow (selectors, navigation, parsing) lives in those files; everything generic
— display/browser launch, stealth, resilient fill/click/select, screenshots,
session persistence + reuse, the run lock, DB helpers — lives here.

Nothing in here is broker-specific; a new portal scraper only needs a PortalCfg
and its own login/scrape functions.
"""
import json
import logging
import os
import random
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

from equity import finmath

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

NAV_TIMEOUT    = 60_000
ACTION_TIMEOUT = 15_000


@dataclass
class PortalCfg:
    """Per-portal configuration shared by all of its entities."""
    name:        str            # short id, e.g. "vested" / "dbs" (also run_type)
    base_url:    str
    download_dir: Path
    profile_dir: Path
    session_dir: Path
    shots_dir:   str
    session_max_age_s: int = 30 * 60
    user_agents: list = field(default_factory=lambda: list(USER_AGENTS))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_entity_id(conn, entity_name: str):
    cur = conn.cursor()
    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (entity_name,))
    row = cur.fetchone()
    cur.close()
    return row["id"] if row else None


def log_run(conn, run_type, status, processed, failed, started, error=None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ingestion_run
            (run_type, run_date, status, records_processed,
             records_failed, error_message, started_at, completed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_type, date.today(), status, processed, failed, error, started, now_utc()),
    )
    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------
def num(s) -> Decimal | None:
    """Parse a money/number cell ('1,234.56', 'USD 1,234.56', '40.78%', '-')."""
    if s is None:
        return None
    t = (str(s).strip()
         .replace(",", "").replace("%", "").replace("$", "")
         .replace("S$", "").replace("US$", "").replace("₹", "")
         .replace("USD", "").replace("SGD", "").strip())
    if t in ("", "-", "--", "NA", "N/A", "None"):
        return None
    if t.startswith("(") and t.endswith(")"):   # accounting negatives
        t = "-" + t[1:-1]
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


# ---------------------------------------------------------------------------
# Run lock — one live scrape per portal (each login claims a session seat)
# ---------------------------------------------------------------------------
class Lock:
    def __init__(self, name: str):
        self.path = Path(f"/tmp/{name}.lock")

    def acquire(self) -> bool:
        try:
            if self.path.exists():
                pid = int(self.path.read_text().strip() or "0")
                try:
                    os.kill(pid, 0)
                    logger.error(f"Another {self.path.stem} run is active (PID {pid}). Exiting.")
                    return False
                except (ProcessLookupError, PermissionError, ValueError):
                    logger.warning("Stale lock — removing and continuing.")
            self.path.write_text(str(os.getpid()))
            return True
        except Exception as e:
            logger.warning(f"Lock error: {e} — proceeding")
            return True

    def release(self):
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Browser launch (Xvfb + persistent stealth context)
# ---------------------------------------------------------------------------
@contextmanager
def browser(cfg: PortalCfg):
    """Yield a Playwright `page` inside a headed-but-virtual stealth context.

    Mirrors the Nuvama worker's launch: a persistent profile, anti-automation
    flags, a randomised UA and the webdriver flag scrubbed. Cleans up the
    virtual display and context on exit.
    """
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    from pyvirtualdisplay import Display

    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.download_dir, 0o700)
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = cfg.profile_dir / stale
        if p.exists() or p.is_symlink():
            p.unlink()

    display = Display(visible=False, size=(1440, 900))
    display.start()
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(cfg.profile_dir),
                headless=False,
                accept_downloads=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
                user_agent=random.choice(cfg.user_agents),
                viewport={"width": 1440, "height": 900},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()
            page.on("dialog", lambda d: d.accept())
            Stealth().apply_stealth_sync(page)
            try:
                yield ctx, page
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    finally:
        display.stop()


# ---------------------------------------------------------------------------
# Session persistence — log in once (browser), reuse via one requests.Session
# ---------------------------------------------------------------------------
def _session_file(cfg: PortalCfg, prefix: str) -> Path:
    return cfg.session_dir / f"{prefix}.session.json"


def save_browser_session(cfg: PortalCfg, prefix: str, ctx) -> None:
    """Persist the authenticated cookies + UA so a later run can reuse them via
    requests instead of logging in again."""
    try:
        cfg.session_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(cfg.session_dir, 0o700)
        ua = ""
        try:
            if ctx.pages:
                ua = ctx.pages[0].evaluate("() => navigator.userAgent")
        except Exception:
            pass
        data = {"saved_at": now_utc().isoformat(), "user_agent": ua, "cookies": ctx.cookies()}
        f = _session_file(cfg, prefix)
        f.write_text(json.dumps(data))
        os.chmod(f, 0o600)
        logger.info(f"[{prefix}] saved session for reuse ({len(data['cookies'])} cookies)")
    except Exception as e:
        logger.info(f"[{prefix}] could not save session: {e}")


def build_requests_session(cfg: PortalCfg, prefix: str) -> "requests.Session | None":
    """A requests.Session seeded with the saved auth cookies, or None when there
    is no saved session or it is too old to trust."""
    f = _session_file(cfg, prefix)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except Exception:
        return None
    try:
        age = (now_utc() - datetime.fromisoformat(data["saved_at"])).total_seconds()
    except Exception:
        age = cfg.session_max_age_s + 1
    if age > cfg.session_max_age_s:
        logger.info(f"[{prefix}] saved session is {int(age)}s old — re-login needed")
        return None

    s = requests.Session()
    s.headers.update({
        "User-Agent":       data.get("user_agent") or cfg.user_agents[0],
        "X-Requested-With": "XMLHttpRequest",
        "Accept":           "application/json, text/plain, */*",
        "Referer":          cfg.base_url.rstrip("/") + "/",
    })
    for c in data.get("cookies", []):
        try:
            s.cookies.set(c["name"], c["value"],
                          domain=c.get("domain", "").lstrip("."), path=c.get("path", "/"))
        except Exception:
            pass
    return s


def session_alive(sess: "requests.Session", cfg: PortalCfg, prefix: str,
                  dead_markers: tuple = ("login", "sign in", "session expired")) -> bool:
    """Cheap probe: hit the app root and confirm the cookies still authenticate."""
    try:
        r = sess.get(cfg.base_url.rstrip("/") + "/", timeout=20, allow_redirects=True)
    except Exception as e:
        logger.info(f"[{prefix}] session probe failed: {e}")
        return False
    blob = (r.url + " " + r.text[:3000]).lower()
    if "login" in r.url.lower() or any(m in blob for m in dead_markers):
        logger.info(f"[{prefix}] saved session no longer authenticated")
        return False
    return True


# ---------------------------------------------------------------------------
# Resilient Playwright helpers (selector lists with fallbacks + DOM dumps)
# ---------------------------------------------------------------------------
def fill(page, selectors, value, label, clear=False, optional=False):
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
            return
        except Exception:
            continue
    dump_inputs(page, label)
    msg = f"Could not find '{label}' field. Tried: {selectors}"
    if optional:
        logger.warning(msg + " — leaving portal default")
        return
    raise RuntimeError(msg)


def click(page, selectors, label, optional=False):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=ACTION_TIMEOUT)
            loc.click(force=True)
            return True
        except Exception:
            continue
    if optional:
        logger.warning(f"Could not click '{label}' — skipping. Tried: {selectors}")
        return False
    raise RuntimeError(f"Could not click '{label}'. Tried: {selectors}")


def select(page, selectors, value, label, optional=False):
    """Select from a native <select> or a custom dropdown widget."""
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
                try:
                    search = page.locator(
                        '.select2-search__field, input.ng-input, input[type="search"]:visible'
                    ).first
                    if search.count() > 0:
                        search.fill(value, timeout=2000)
                        page.wait_for_timeout(400)
                except Exception:
                    pass
                page.locator(f'text="{value}"').first.click(timeout=ACTION_TIMEOUT, force=True)
            return
        except Exception:
            continue
    msg = f"Could not select '{label}'={value}. Tried: {selectors}"
    if optional:
        logger.warning(msg + " — leaving portal default")
        return
    raise RuntimeError(msg)


def has_text(page, *needles) -> bool:
    try:
        content = page.content().lower()
        return any(n.lower() in content for n in needles)
    except Exception:
        return False


def dump_inputs(page, label):
    try:
        dump = page.evaluate("""() => Array.from(document.querySelectorAll('input,select,button'))
            .map(el => ({tag: el.tagName, type: el.type, name: el.name, id: el.id,
                         placeholder: el.placeholder, text: (el.innerText||'').slice(0,30),
                         visible: el.offsetParent !== null})).slice(0, 60)""")
        logger.warning(f"DOM control dump for '{label}': {dump}")
    except Exception:
        pass


def shot(page, cfg: PortalCfg, filename):
    try:
        os.makedirs(cfg.shots_dir, exist_ok=True)
        os.chmod(cfg.shots_dir, 0o700)
        path = f"{cfg.shots_dir}/{filename}"
        page.screenshot(path=path, timeout=15_000)
        os.chmod(path, 0o600)
        logger.info(f"Screenshot saved: {filename}")
    except Exception as e:
        logger.warning(f"Screenshot failed ({filename}): {e}")


def save_download(download, cfg: PortalCfg, prefix: str) -> Path:
    suggested = download.suggested_filename or f"{cfg.name}_{prefix}.xlsx"
    dest = cfg.download_dir / f"{prefix}_{date.today():%Y%m%d}_{suggested}"
    download.save_as(str(dest))
    os.chmod(dest, 0o600)
    return dest


def basic_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ---------------------------------------------------------------------------
# Transaction ledger → since-held + XIRR
# ---------------------------------------------------------------------------
def prev_business_day(d: date) -> date:
    """Most recent weekday strictly before `d` (skips Sat/Sun). Holidays are not
    modelled — a no-trade day simply yields no transactions, which is harmless."""
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:        # 5=Sat, 6=Sun
        cur -= timedelta(days=1)
    return cur


def incremental_range(cache: dict | None, today: date,
                      epoch: date = date(2000, 1, 1)) -> tuple[date, date, bool]:
    """The (from, to, is_first_run) transaction window to fetch this run.

    First run (no ledger cursor): epoch → previous business day, full history.
    Daily run: day after the stored cursor → previous business day. Returns a
    zero-width window (from > to) when there is nothing new to pull.
    """
    to_date = prev_business_day(today)
    through = (cache or {}).get("ledger_through")
    if not through:
        return epoch, to_date, True
    try:
        last = date.fromisoformat(str(through)[:10])
    except Exception:
        return epoch, to_date, True
    return last + timedelta(days=1), to_date, False


def merge_and_metric(holdings: list[dict], ledger: dict | None,
                     transactions: list[dict], today: date) -> dict:
    """Append new dated cash flows to the per-symbol ledger, then derive
    `first_invested_date` and `xirr_pct` onto each holding from its accumulated
    flows plus its current native market value.

    `transactions`: list of {"date","symbol","amount"} (buys negative, sells /
    dividends positive). `ledger`: symbol → list of {"date","amount"}. Returns
    the updated ledger (to persist in the feed cache for the next run).
    """
    ledger = {k: list(v) for k, v in (ledger or {}).items()}
    for t in transactions:
        sym = (t.get("symbol") or "").strip().upper()
        amt = t.get("amount")
        d   = t.get("date")
        if not sym or amt is None or not d:
            continue
        ledger.setdefault(sym, []).append({"date": str(d)[:10], "amount": float(amt)})

    for h in holdings:
        sym = (h.get("symbol") or "").strip().upper()
        raw = ledger.get(sym, [])
        flows: list[tuple[date, float]] = []
        for f in raw:
            try:
                flows.append((date.fromisoformat(str(f["date"])[:10]), float(f["amount"])))
            except Exception:
                continue
        if not flows:
            continue
        buys = [d for d, a in flows if a < 0]
        h["first_invested_date"] = (min(buys) if buys else min(d for d, _ in flows)).isoformat()
        try:
            cur_val = float(h["quantity"]) * float(h["current_price"])
        except Exception:
            cur_val = 0.0
        x = finmath.xirr(flows + [(today, cur_val)]) if cur_val > 0 else None
        h["xirr_pct"] = round(x * 100, 4) if x is not None else None
    return ledger
