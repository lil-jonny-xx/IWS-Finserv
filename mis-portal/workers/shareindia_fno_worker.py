#!/usr/bin/env python3
"""
Share India uTrade FnO worker — IWS MIS Portal.

Share India's uTrade portal (https://www.utrade.shareindia.com) carries HHR's
(Harsh's) FnO book. There is no investor API, so — like Vested / DBS / Nuvama —
we drive a browser via the shared _portal_scraper toolkit. The second FnO
portal, Orbis (DHR / Dhruv), gets its own worker later on the same skeleton.

Login is user id + password, then an emailed OTP. The OTP mail auto-forwards
into the central collector inbox (address in .env) and is read with the
shared Gmail token, exactly like the ICICI PMS worker (in:anywhere — forwarded
mail often lands in Spam).

RECON-FIRST: the portal's selectors, page layout and API shapes are unknown
until we see a real logged-in session. Until SHAREINDIA_SCHEMA_READY=1 the
worker therefore performs exactly ONE login attempt (no retries — each login
consumes an OTP and repeated attempts risk a lockout) and then walks the likely
FnO pages capturing, per page:
  * a screenshot            → /home/SAdmin/.shareindia-screenshots/
  * the rendered HTML       → .shareindia_downloads/recon/
  * every JSON XHR response → .shareindia_downloads/recon/json/
The position/margin parsers get built from that capture, after which
parse-and-upsert into fno_position / fno_account is switched on.

The browser profile and cookies persist between runs (save_browser_session),
so later runs reuse the session instead of logging in again whenever possible.

Credentials (.env):
  SHAREINDIA_HHR_USERNAME / SHAREINDIA_HHR_PASSWORD
Optional:
  SHAREINDIA_LOGIN_URL, SHAREINDIA_OTP_FROM, SHAREINDIA_OTP_REGEX,
  SHAREINDIA_OTP_WAIT_S, SHAREINDIA_SCHEMA_READY, SHAREINDIA_LOGIN_DISABLED=1

Run:  /var/www/.venv/bin/python workers/shareindia_fno_worker.py
"""
import base64
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # workers/ — for gmail_worker
load_dotenv("/var/www/mis-portal/.env", override=True)

import _portal_scraper as ps   # noqa: E402
import gmail_worker            # noqa: E402  central-inbox reader (CAS pipeline)

ps.basic_logging()
logger = logging.getLogger("shareindia_fno_worker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL  = os.environ.get("SHAREINDIA_BASE_URL", "https://www.utrade.shareindia.com")
LOGIN_URL = os.environ.get(
    "SHAREINDIA_LOGIN_URL",
    f"{BASE_URL.rstrip('/')}/login?next=%2Fdashboard%2Ftrading")
HOME_URL  = os.environ.get("SHAREINDIA_HOME_URL", f"{BASE_URL.rstrip('/')}/dashboard/trading")

SOURCE = "shareindia"

# Pages worth capturing in recon, tried in order; 404s/redirects are harmless
# (we screenshot whatever renders). Tune this list after the first capture.
RECON_PATHS = [
    "/dashboard/trading",
    "/dashboard",
    "/dashboard/positions",
    "/positions",
    "/dashboard/holdings",
    "/holdings",
    "/dashboard/funds",
    "/funds",
    "/dashboard/orders",
    "/portfolio",
]

# Only HHR trades FnO through Share India: (entity_code, env_prefix).
ENTITIES = [("HHR", "HHR")]

SCHEMA_READY = os.environ.get(
    "SHAREINDIA_SCHEMA_READY", "0").strip().lower() in ("1", "true", "yes")
LOGIN_DISABLED = os.environ.get(
    "SHAREINDIA_LOGIN_DISABLED", "0").strip().lower() in ("1", "true", "yes")

CFG = ps.PortalCfg(
    name="shareindia",
    base_url=BASE_URL,
    download_dir=Path("/var/www/mis-portal/.shareindia_downloads"),
    profile_dir=Path("/var/www/mis-portal/.shareindia_browser_profile"),
    session_dir=Path("/var/www/mis-portal/.shareindia_sessions"),
    shots_dir="/home/SAdmin/.shareindia-screenshots",
)
RECON_DIR = CFG.download_dir / "recon"

# OTP retrieval from the central collector inbox (address in .env).
WORKERS_DIR = Path(__file__).parent
GMAIL_TOKEN_CENTRAL = str(WORKERS_DIR / os.environ.get(
    "GMAIL_TOKEN_CENTRAL", "gmail_token_central.json"))
OTP_FROM_FILTER = os.environ.get("SHAREINDIA_OTP_FROM", "shareindia")
OTP_CODE_REGEX  = os.environ.get("SHAREINDIA_OTP_REGEX", r"\b(\d{4,8})\b")
OTP_WAIT_S      = int(os.environ.get("SHAREINDIA_OTP_WAIT_S", "300"))
OTP_POLL_S      = int(os.environ.get("SHAREINDIA_OTP_POLL_S", "10"))


class ShareIndiaSchemaUnknown(Exception):
    """Raised when parsing is attempted before the portal capture has been
    mapped (SHAREINDIA_SCHEMA_READY not set)."""


@dataclass
class SIConfig:
    code:     str   # entity_name in DB, e.g. "HHR"
    prefix:   str   # env prefix
    username: str
    password: str


def _env(prefix: str, key: str, required: bool = False) -> str:
    val = os.environ.get(f"SHAREINDIA_{prefix}_{key}", "").strip()
    if required and not val:
        raise KeyError(f"SHAREINDIA_{prefix}_{key} not set in .env")
    return val


def load_configs() -> list[SIConfig]:
    configs = []
    for code, prefix in ENTITIES:
        if not _env(prefix, "USERNAME"):
            continue
        configs.append(SIConfig(
            code=code, prefix=prefix,
            username=_env(prefix, "USERNAME", required=True),
            password=_env(prefix, "PASSWORD", required=True),
        ))
    return configs


# ---------------------------------------------------------------------------
# OTP via the central Gmail inbox (same machinery as the ICICI PMS worker)
# ---------------------------------------------------------------------------
def _message_body_text(msg: dict) -> str:
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
    if msg.get("snippet"):
        out.append(msg["snippet"])
    return "\n".join(out)


def fetch_otp(after_ts: int) -> str | None:
    """Poll the central inbox for a Share India OTP mail newer than `after_ts`
    and return the extracted code, or None on timeout.

    Two phases, because the exact sender is unknown until the first real mail:
    first half of the wait searches by SHAREINDIA_OTP_FROM; the second half
    broadens to ALL fresh mail and accepts any message that mentions
    share india / utrade and carries a code. A wrong sender guess must not
    waste the (single) login attempt."""
    service = gmail_worker._get_service(GMAIL_TOKEN_CENTRAL)
    # in:anywhere is required — the mail is auto-forwarded into the collector
    # (possibly two hops) and Gmail may file it under Spam, which the default
    # search scope excludes.
    narrow_q = f"in:anywhere from:{OTP_FROM_FILTER} after:{after_ts}"
    broad_q  = f"in:anywhere after:{after_ts}"
    deadline = time.time() + OTP_WAIT_S
    broaden_at = time.time() + OTP_WAIT_S / 2
    pat = re.compile(OTP_CODE_REGEX)
    logger.info(f"Waiting for Share India OTP mail (q={narrow_q!r}, timeout {OTP_WAIT_S}s)...")
    while time.time() < deadline:
        broad = time.time() >= broaden_at
        query = broad_q if broad else narrow_q
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
            if broad:
                blob = body.lower()
                if not any(k in blob for k in ("share india", "shareindia", "utrade")):
                    continue
                if "otp" not in blob and "one time" not in blob and "one-time" not in blob:
                    continue
            mm = pat.search(body)
            if mm:
                code = mm.group(1)
                logger.info(f"OTP received ({len(code)} digits, "
                            f"{'broad' if broad else 'sender'} match)")
                return code
        remaining = int(deadline - time.time())
        logger.info(f"No OTP yet ({'broad' if broad else 'sender'} search) — "
                    f"retry in {OTP_POLL_S}s (~{remaining}s left)")
        time.sleep(OTP_POLL_S)
    logger.error("Timed out waiting for Share India OTP mail")
    return None


# ---------------------------------------------------------------------------
# Login — user id + password, then emailed OTP. ONE attempt, ever, per run.
# ---------------------------------------------------------------------------
def _is_logged_in(page) -> bool:
    """Heuristic: off the login page and no login form present."""
    try:
        if "/login" in (page.url or "").lower():
            return False
        return page.locator('input[type="password"]').count() == 0
    except Exception:
        return False


def _enter_otp(page, code: str) -> None:
    """Enter the OTP into either a single field or the split per-digit boxes."""
    single = page.locator(
        'input[autocomplete="one-time-code"], input[name*="otp" i], '
        'input[placeholder*="otp" i], input[id*="otp" i]')
    if single.count() > 0:
        ps.fill(page, ['input[autocomplete="one-time-code"]', 'input[name*="otp" i]',
                       'input[placeholder*="otp" i]', 'input[id*="otp" i]'],
                code, "OTP")
        return
    # Split boxes: several 1-char inputs — click the first and type the digits.
    boxes = page.locator('input[maxlength="1"]')
    if boxes.count() >= 4:
        boxes.first.click(force=True)
        page.keyboard.type(code, delay=120)
        return
    raise RuntimeError("No OTP input found on the page")


_FATAL_MARKERS = ("invalid password", "invalid user", "invalid ucc", "invalid credentials",
                  "user not found", "account locked", "account blocked",
                  "invalid otp", "otp expired", "incorrect otp")


def _visible(page, selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible()
    except Exception:
        return False


def _wait_turnstile(page, tag: str, timeout_s: int = 90) -> bool:
    """The login form is gated on a Cloudflare Turnstile (first capture showed
    it stuck on 'Verifying…' — a submit before its token exists is a no-op).
    Wait for the token; if the widget escalates to an interactive checkbox,
    click it once inside its iframe. True when a token is present (or there is
    no Turnstile on this step)."""
    deadline = time.time() + timeout_s
    poked = False
    while time.time() < deadline:
        try:
            val = page.evaluate(
                "() => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]');"
                " return el ? el.value : null; }")
        except Exception:
            val = None
        if val is None or (val and len(val) > 10):
            return True
        if not poked and time.time() - (deadline - timeout_s) > 20:
            try:
                frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
                frame.locator('input[type="checkbox"], .cb-lb, body').first.click(
                    timeout=3000, force=True)
                logger.info(f"[{tag}] poked the Turnstile checkbox")
            except Exception:
                pass
            poked = True
        page.wait_for_timeout(2000)
    ps.shot(page, CFG, f"si_turnstile_stuck_{tag}.png")
    return False


def login(page, cfg: SIConfig) -> None:
    """One login attempt: UCC → whatever the portal asks next (password / OTP /
    a send-OTP choice) → dashboard, as a step machine driven by what is actually
    on screen (the uTrade SPA reveals one step at a time — first capture
    2026-07-09 showed only the UCC field, formcontrolname=IdControl, plus a
    Cloudflare Turnstile). Screenshots + HTML dumps at every step so a failure
    still produces the material needed to tune the flow. NEVER retries — a
    second attempt means a second OTP and (on repeated failures) a lockout."""
    logger.info(f"[{cfg.prefix}] navigating to login: {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=ps.NAV_TIMEOUT)
    page.wait_for_timeout(5000)   # SPA settle (+ invisible Turnstile)
    ps.shot(page, CFG, f"si_login_{cfg.prefix}.png")
    _dump_html(page, f"login_{cfg.prefix}")

    # Any of the submits below may trigger the OTP mail — timestamp before all.
    otp_request_ts = int(time.time())
    password_done = False
    otp_done = False

    ps.fill(page, [
        'input[formcontrolname="IdControl"]', 'input[placeholder*="UCC" i]',
        'input[placeholder*="Registered Mobile" i]', '#mat-input-0',
    ], cfg.username, "UCC")
    ps.shot(page, CFG, f"si_ucc_filled_{cfg.prefix}.png")
    if not _wait_turnstile(page, cfg.prefix):
        raise RuntimeError("Cloudflare Turnstile never verified — nothing was "
                           "submitted, so the attempt is not spent; see "
                           "si_turnstile_stuck screenshot")
    ps.click(page, [
        'button[type="submit"]', 'button:has-text("Log In")', 'button:has-text("Login")',
        'button:has-text("Continue")', 'button:has-text("Proceed")',
    ], "UCC submit")

    for step in range(1, 6):
        page.wait_for_timeout(7000)
        ps.shot(page, CFG, f"si_step{step}_{cfg.prefix}.png")
        _dump_html(page, f"step{step}_{cfg.prefix}")

        if _is_logged_in(page):
            logger.info(f"[{cfg.prefix}] login complete (after step {step})")
            return
        hit = next((m for m in _FATAL_MARKERS if ps.has_text(page, m)), None)
        if hit:
            raise RuntimeError(f"Portal error at step {step}: {hit!r} — NOT retrying; "
                               f"see si_step{step} screenshot")

        if not password_done and _visible(page, 'input[type="password"]'):
            ps.fill(page, ['input[type="password"]'], cfg.password, "password")
            ps.shot(page, CFG, f"si_password_filled_{cfg.prefix}.png")
            _wait_turnstile(page, f"{cfg.prefix}_pw")   # a fresh widget may gate this step too
            ps.click(page, [
                'button[type="submit"]', 'button:has-text("Log In")',
                'button:has-text("Login")', 'button:has-text("Continue")',
                'button:has-text("Proceed")', 'button:has-text("Verify")',
            ], "password submit")
            password_done = True
            continue

        otp_visible = (
            _visible(page, 'input[autocomplete="one-time-code"]')
            or _visible(page, 'input[name*="otp" i]')
            or _visible(page, 'input[placeholder*="otp" i]')
            or _visible(page, 'input[formcontrolname*="otp" i]')
            or page.locator('input[maxlength="1"]').count() >= 4
        )
        if not otp_done and otp_visible:
            code = fetch_otp(otp_request_ts)
            if not code:
                ps.shot(page, CFG, f"si_otp_timeout_{cfg.prefix}.png")
                raise RuntimeError("No OTP mail arrived — check forwarding into the "
                                   "collector inbox and the SHAREINDIA_OTP_FROM filter")
            _enter_otp(page, code)
            ps.shot(page, CFG, f"si_otp_entered_{cfg.prefix}.png")
            ps.click(page, [
                'button[type="submit"]', 'button:has-text("Verify")',
                'button:has-text("Submit")', 'button:has-text("Continue")',
                'button:has-text("Log In")', 'button:has-text("Login")',
            ], "OTP submit", optional=True)
            otp_done = True
            continue

        # No known field — maybe a choose-your-2FA screen or a plain continue.
        if ps.click(page, [
            'button:has-text("Email")', 'button:has-text("Send OTP")',
            'button:has-text("Get OTP")',
            'button:has-text("Continue")', 'button:has-text("Proceed")',
        ], f"step {step} advance", optional=True):
            continue
        ps.dump_inputs(page, f"step {step} (unrecognised)")
        raise RuntimeError(f"Unrecognised login step {step} — see si_step{step} "
                           "screenshot + HTML dump; NOT retrying")

    raise RuntimeError("Login did not reach the dashboard within 5 steps — NOT retrying")


# ---------------------------------------------------------------------------
# Recon — capture everything needed to build the parser offline
# ---------------------------------------------------------------------------
def _dump_html(page, tag: str) -> None:
    try:
        RECON_DIR.mkdir(parents=True, exist_ok=True)
        f = RECON_DIR / f"{tag}_{date.today():%Y%m%d}.html"
        f.write_text(page.content())
        os.chmod(f, 0o600)
        logger.info(f"HTML dumped: {f.name}")
    except Exception as e:
        logger.warning(f"HTML dump failed ({tag}): {e}")


def _attach_json_capture(page) -> None:
    """Save every JSON XHR body the app loads — the portal's own API responses
    are the best parser source (and often carry the positions verbatim)."""
    json_dir = RECON_DIR / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()

    def on_response(resp):
        try:
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", resp.url.split("?")[0][-90:]).strip("_")
            if slug in seen:      # first capture per endpoint is enough
                return
            body = resp.text()
            if not body or len(body) > 2_000_000:
                return
            seen.add(slug)
            f = json_dir / f"{slug}.json"
            f.write_text(body)
            os.chmod(f, 0o600)
            logger.info(f"JSON captured: {slug} ({len(body)} bytes)")
        except Exception:
            pass

    page.on("response", on_response)


def recon(page, cfg: SIConfig) -> int:
    """Walk the candidate FnO pages, screenshotting and dumping each. Returns
    the number of pages that rendered."""
    ok = 0
    for path in RECON_PATHS:
        url = BASE_URL.rstrip("/") + path
        tag = re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") or "root"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=ps.NAV_TIMEOUT)
            page.wait_for_timeout(6000)   # SPA data loads
            if "/login" in (page.url or "").lower():
                logger.warning(f"[{cfg.prefix}] {path} bounced to login — session dropped, "
                               "stopping recon walk")
                ps.shot(page, CFG, f"si_recon_{tag}_bounced_{cfg.prefix}.png")
                break
            ps.shot(page, CFG, f"si_recon_{tag}_{cfg.prefix}.png")
            _dump_html(page, f"recon_{tag}_{cfg.prefix}")
            ok += 1
        except Exception as e:
            logger.warning(f"[{cfg.prefix}] recon {path} failed: {e}")
    return ok


# ---------------------------------------------------------------------------
# Parse + upsert — switched on once the capture has been mapped
# ---------------------------------------------------------------------------
def parse_positions(cfg: SIConfig) -> tuple[list[dict], dict]:
    """Map the captured portal data to fno_position / fno_account rows.
    Built AFTER the first recon capture — until then this raises."""
    raise ShareIndiaSchemaUnknown(
        "Share India page/API mapping not built yet — run recon, inspect "
        f"{CFG.shots_dir} and {RECON_DIR}, implement parse_positions(), then "
        "set SHAREINDIA_SCHEMA_READY=1")


def upsert_positions(conn, entity_id: int, positions: list[dict],
                     account: dict | None) -> int:
    """Replace the entity's open Share India positions and refresh the account
    summary. `positions` rows use fno_position column names."""
    cur = conn.cursor()
    cur.execute("DELETE FROM fno_position WHERE entity_id = %s AND source = %s",
                (entity_id, SOURCE))
    n = 0
    for p in positions:
        cur.execute(
            """
            INSERT INTO fno_position
                (entity_id, source, symbol, underlying, instrument, expiry, strike,
                 product, quantity, lot_size, avg_price, ltp, mtm_pnl, realized_pnl,
                 as_of_date, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, source, symbol, product) DO UPDATE SET
                underlying = EXCLUDED.underlying, instrument = EXCLUDED.instrument,
                expiry = EXCLUDED.expiry, strike = EXCLUDED.strike,
                quantity = EXCLUDED.quantity, lot_size = EXCLUDED.lot_size,
                avg_price = EXCLUDED.avg_price, ltp = EXCLUDED.ltp,
                mtm_pnl = EXCLUDED.mtm_pnl, realized_pnl = EXCLUDED.realized_pnl,
                as_of_date = EXCLUDED.as_of_date, updated_at = NOW()
            """,
            (entity_id, SOURCE, p["symbol"], p.get("underlying"), p.get("instrument"),
             p.get("expiry"), p.get("strike"), p.get("product") or "",
             p.get("quantity") or 0, p.get("lot_size"), p.get("avg_price"),
             p.get("ltp"), p.get("mtm_pnl"), p.get("realized_pnl"), date.today()),
        )
        n += 1
    if account is not None:
        cur.execute(
            """
            INSERT INTO fno_account
                (entity_id, source, margin_available, margin_used, ledger_balance,
                 day_realized_pnl, total_mtm_pnl, as_of_date, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, source) DO UPDATE SET
                margin_available = EXCLUDED.margin_available,
                margin_used = EXCLUDED.margin_used,
                ledger_balance = EXCLUDED.ledger_balance,
                day_realized_pnl = EXCLUDED.day_realized_pnl,
                total_mtm_pnl = EXCLUDED.total_mtm_pnl,
                as_of_date = EXCLUDED.as_of_date, updated_at = NOW()
            """,
            (entity_id, SOURCE, account.get("margin_available"),
             account.get("margin_used"), account.get("ledger_balance"),
             account.get("day_realized_pnl"), account.get("total_mtm_pnl"),
             date.today()),
        )
    conn.commit()
    cur.close()
    return n


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def process_entity(cfg: SIConfig) -> None:
    with ps.browser(CFG) as (ctx, page):
        _attach_json_capture(page)

        # Reuse a still-authenticated persistent profile before spending a login.
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=ps.NAV_TIMEOUT)
        page.wait_for_timeout(4000)
        if _is_logged_in(page):
            logger.info(f"[{cfg.prefix}] existing browser session still authenticated")
        else:
            if LOGIN_DISABLED:
                raise RuntimeError("Session dead and SHAREINDIA_LOGIN_DISABLED=1 — "
                                   "not attempting a login")
            login(page, cfg)
        ps.save_browser_session(CFG, cfg.prefix, ctx)

        pages_ok = recon(page, cfg)
        logger.info(f"[{cfg.prefix}] recon captured {pages_ok} page(s) — see "
                    f"{CFG.shots_dir} and {RECON_DIR}")

        if SCHEMA_READY:
            positions, account = parse_positions(cfg)
            conn = ps.get_db()
            try:
                eid = ps.load_entity_id(conn, cfg.code)
                if eid is None:
                    raise RuntimeError(f"Entity {cfg.code!r} not found in DB")
                n = upsert_positions(conn, eid, positions, account)
                logger.info(f"[{cfg.prefix}] upserted {n} FnO positions")
            finally:
                conn.close()
        else:
            logger.info(f"[{cfg.prefix}] SHAREINDIA_SCHEMA_READY not set — recon only, "
                        "no DB writes")


def run() -> int:
    lock = ps.Lock("shareindia_fno_worker")
    if not lock.acquire():
        return 1
    started = ps.now_utc()
    conn = None
    try:
        configs = load_configs()
        if not configs:
            logger.info("No Share India credentials configured — nothing to do")
            return 0
        failed = 0
        for cfg in configs:
            try:
                process_entity(cfg)
            except Exception as e:
                logger.error(f"[{cfg.prefix}] failed: {e}")
                failed += 1
        conn = ps.get_db()
        ps.log_run(conn, "shareindia_fno", "success" if failed == 0 else "failed",
                   len(configs) - failed, failed, started,
                   error=None if failed == 0 else "see worker log")
        return 0 if failed == 0 else 1
    finally:
        if conn is not None:
            conn.close()
        lock.release()


if __name__ == "__main__":
    sys.exit(run())
