#!/usr/bin/env python3
"""
DBS Worker — IWS MIS Portal.

DBS Wealth Singapore (DBS iWealth / Treasures) has no retail investor API, so —
like the Nuvama PMS worker — we drive a browser: log in, read the current
holdings, and read the transaction history. Only HHR holds a DBS account.

Two data products per run (same model as the Vested worker):
  * HOLDINGS — the current snapshot (downloaded statement, parsed; DOM table as
    fallback) → equity_holding via the `dbs` adapter + equity sync (SGD→INR).
  * TRANSACTIONS — date-filtered activity/transactions, NOT stored in a table;
    they feed a per-symbol cash-flow ledger in the feed cache from which we
    derive each holding's first_invested_date (since-held → CAGR) and the
    money-weighted XIRR (native SGD). First run = full history; daily = previous
    business day, appended.

DBS iWealth typically enforces a digital-token 2FA at login. If that wall
appears we screenshot and fail loudly — it needs interactive approval and cannot
run unattended; fall back to the manual DBS_HHR_HOLDINGS feed in that case.

Credentials (.env):
  DBS_HHR_USERNAME / DBS_HHR_PASSWORD
Optional:
  DBS_BASE_URL, DBS_LOGIN_URL, DBS_DOWNLOAD_FILE=0, DBS_RANDOMIZE=0,
  DBS_SCHEMA_READY=1  (once the holdings-statement column mapping is finalised)

NOTE: DBS selectors / statement & transaction layouts can only be confirmed
against a live login; everything is best-effort with fallbacks + screenshots
(_SHOTS_DIR), mirroring Nuvama.

Run:  /var/www/.venv/bin/python workers/dbs_worker.py
Cron: via workers/cron_wrapper.py, daily before market open.
"""
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

import _portal_scraper as ps          # noqa: E402
from equity.brokers import _feed       # noqa: E402

ps.basic_logging()
logger = logging.getLogger("dbs_worker")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL  = os.environ.get("DBS_BASE_URL", "https://internet-banking.dbs.com.sg")
LOGIN_URL = os.environ.get("DBS_LOGIN_URL", f"{BASE_URL.rstrip('/')}/")
CURRENCY  = "SGD"
BROKER    = "dbs"
_SHOTS_DIR = "/home/SAdmin/.dbs-screenshots"

DOWNLOAD_FILE   = os.environ.get("DBS_DOWNLOAD_FILE", "1").strip().lower() in ("1", "true", "yes")
SCHEMA_READY    = os.environ.get("DBS_SCHEMA_READY", "0").strip().lower() in ("1", "true", "yes")

ENTITIES = [("HHR", "HHR")]

RANDOMIZE      = os.environ.get("DBS_RANDOMIZE", "1").strip().lower() in ("1", "true", "yes")
START_JITTER_S = int(os.getenv("DBS_START_JITTER_S", str(5 * 60)))
EXECUTE_TIMEOUT = 120_000

CFG = ps.PortalCfg(
    name="dbs",
    base_url=BASE_URL,
    download_dir=Path("/var/www/mis-portal/.dbs_downloads"),
    profile_dir=Path("/var/www/mis-portal/.dbs_browser_profile"),
    session_dir=Path("/var/www/mis-portal/.dbs_sessions"),
    shots_dir=_SHOTS_DIR,
)


class DbsSchemaUnknown(Exception):
    """Raised when the downloaded statement layout has not yet been mapped."""


@dataclass
class DbsConfig:
    code:     str
    prefix:   str
    username: str
    password: str


def _env(prefix: str, key: str, required: bool = False) -> str:
    val = os.environ.get(f"DBS_{prefix}_{key}", "")
    if required and not val:
        raise KeyError(f"DBS_{prefix}_{key} not set in .env")
    return val


def load_configs() -> list[DbsConfig]:
    configs = []
    for code, prefix in ENTITIES:
        if not _env(prefix, "USERNAME"):
            continue
        configs.append(DbsConfig(
            code=code, prefix=prefix,
            username=_env(prefix, "USERNAME", required=True),
            password=_env(prefix, "PASSWORD", required=True),
        ))
    return configs


# ---------------------------------------------------------------------------
# Signed cash flow (shared convention with the Vested worker)
# ---------------------------------------------------------------------------
_SELL_WORDS   = ("sell", "sld", "sale", "redeem", "redemption", "disposal")
_BUY_WORDS    = ("buy", "bot", "purchase", "subscription", "subscribe")
_INFLOW_WORDS = ("dividend", "interest", "coupon", "div")
_SKIP_WORDS   = ("deposit", "withdraw", "transfer", "fee", "charge", "tax", "fx", "conversion")


def _txn_sign_amount(ttype: str, amount, qty, price) -> Decimal | None:
    t = (ttype or "").strip().lower()
    amt = amount
    if amt is None and qty is not None and price is not None:
        amt = qty * price
    if amt is None:
        return None
    amt = abs(amt)
    if any(w in t for w in _SELL_WORDS) or any(w in t for w in _INFLOW_WORDS):
        return amt
    if any(w in t for w in _BUY_WORDS):
        return -amt
    if any(w in t for w in _SKIP_WORDS):
        return None
    if amount is not None and amount < 0:
        return amount
    return None


# ---------------------------------------------------------------------------
# Login (+ 2FA wall handling)
# ---------------------------------------------------------------------------
def login(page, cfg: DbsConfig):
    logger.info(f"[{cfg.code}] logging in to DBS iWealth")
    page.goto(LOGIN_URL, timeout=ps.NAV_TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(1500, 2500))
    ps.shot(page, CFG, f"dbs_login_{cfg.prefix}.png")

    ps.fill(page, [
        'input[name="UID"]', 'input[name="userId"]', 'input[id*="user" i]',
        'input[placeholder*="user" i]', 'input[type="text"]',
    ], cfg.username, "username")
    ps.fill(page, [
        'input[name="PIN"]', 'input[name="password"]', 'input[type="password"]',
        'input[id*="pin" i]', 'input[id*="pass" i]',
    ], cfg.password, "password")
    page.wait_for_timeout(random.randint(300, 700))
    ps.click(page, [
        'button:has-text("Login")', 'button[type="submit"]', 'input[type="submit"]',
        'a:has-text("Login")', 'button:has-text("Log in")',
    ], "login button")

    try:
        page.wait_for_load_state("domcontentloaded", timeout=ps.ACTION_TIMEOUT)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    ps.shot(page, CFG, f"dbs_postlogin_{cfg.prefix}.png")

    if ps.has_text(page, "digital token", "approve", "one-time password",
                   "otp", "authenticate", "verify your identity"):
        raise RuntimeError(
            f"[{cfg.code}] DBS 2FA wall reached (digital token / OTP). Needs an "
            f"interactive approval — cannot run unattended (see screenshot). Use a "
            f"maintained session or DBS_{cfg.prefix}_HOLDINGS as a fallback."
        )
    if ps.has_text(page, "invalid", "incorrect", "not match", "try again"):
        raise RuntimeError(f"[{cfg.code}] login appears to have failed — check credentials")


def logout(page):
    try:
        ps.click(page, ['a:has-text("Logout")', 'a:has-text("Log out")', 'a:has-text("Sign Out")',
                        '[href*="logout" i]', 'button:has-text("Logout")'], "logout", optional=True)
        page.wait_for_timeout(1000)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Holdings — open Investments, download statement / read the table
# ---------------------------------------------------------------------------
def open_holdings(page, cfg: DbsConfig):
    ps.click(page, [
        'a:has-text("Investments")', 'a:has-text("Wealth")', 'a:has-text("Portfolio")',
        'a:has-text("Holdings")', 'span:has-text("Investments")', '[href*="invest" i]',
        '[href*="wealth" i]',
    ], "Investments nav", optional=True)
    page.wait_for_timeout(3000)
    ps.click(page, [
        'a:has-text("Holdings")', 'a:has-text("Portfolio")', 'a:has-text("Your Holdings")',
        'span:has-text("Holdings")', '[href*="holding" i]',
    ], "Holdings tab", optional=True)
    page.wait_for_timeout(3000)
    ps.shot(page, CFG, f"dbs_holdings_{cfg.prefix}.png")


def download_statement(page, cfg: DbsConfig, tag: str = "holdings") -> Path | None:
    for sel in ('a:has-text("Download")', 'button:has-text("Download")',
                'a:has-text("Export")', 'button:has-text("Export")',
                'a:has-text("Statement")', '[aria-label*="download" i]',
                '[class*="download" i]', 'i[class*="download" i]'):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            with page.expect_download(timeout=EXECUTE_TIMEOUT) as dl:
                loc.click(force=True)
            path = ps.save_download(dl.value, CFG, f"{cfg.prefix}_{tag}")
            logger.info(f"[{cfg.code}] {tag} downloaded: {path.name}")
            return path
        except Exception:
            continue
    logger.warning(f"[{cfg.code}] no downloadable {tag} found — will parse the DOM")
    return None


def parse_statement(path: Path, cfg: DbsConfig) -> tuple[list[dict], Decimal | None]:
    """Parse a downloaded DBS holdings statement → (holdings, cash).

    Schema-capture-first: logs the real layout and raises DbsSchemaUnknown until
    DBS_SCHEMA_READY=1, exactly like the Nuvama report-schema capture."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        schema = _dump_xlsx_schema(path)
    elif suffix == ".csv":
        schema = _dump_csv_schema(path)
    elif suffix == ".pdf":
        schema = _dump_pdf_schema(path)
    else:
        schema = f"(unrecognised file type: {suffix})"

    if not SCHEMA_READY:
        logger.warning(
            f"[{cfg.code}] DBS statement schema not mapped — layout below; implement "
            f"parse_statement() and set DBS_SCHEMA_READY=1.\n{schema}"
        )
        raise DbsSchemaUnknown(path.name)
    # TODO(once schema known): map rows → holding dicts + cash.
    raise NotImplementedError("parse_statement mapping pending real DBS schema")


def _dump_xlsx_schema(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"  sheet '{ws.title}':")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 30:
                out.append("    ..."); break
            out.append(f"    r{i}: {[str(c) for c in row if c is not None][:14]}")
    wb.close()
    return "\n".join(out)


def _dump_csv_schema(path: Path) -> str:
    import csv
    out = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for i, row in enumerate(csv.reader(f)):
            if i >= 30:
                out.append("    ..."); break
            out.append(f"    r{i}: {row[:14]}")
    return "  CSV rows:\n" + "\n".join(out)


def _dump_pdf_schema(path: Path) -> str:
    import fitz
    doc = fitz.open(path)
    text = doc[0].get_text()[:1500] if doc.page_count else ""
    doc.close()
    return f"  PDF page 1 text (first 1500 chars):\n{text}"


def holdings_from_dom(page) -> tuple[list[dict], Decimal | None]:
    grid = page.evaluate("""() => {
      const tables = Array.from(document.querySelectorAll('table'));
      let target = null;
      for (const t of tables) {
        const h = (t.innerText || '').toLowerCase();
        if ((h.includes('holding') || h.includes('security') || h.includes('name')
             || h.includes('stock')) && (h.includes('quantity') || h.includes('units')
             || h.includes('market value') || h.includes('value'))) { target = t; break; }
      }
      if (!target) return [];
      return Array.from(target.querySelectorAll('tr')).map(tr =>
        Array.from(tr.querySelectorAll('td,th'))
          .map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim()));
    }""")
    holdings: list[dict] = []
    for cells in grid:
        if len(cells) < 3:
            continue
        name = (cells[0] or "").strip()
        if not name or name.lower() in ("name", "security", "holding", "stock", "symbol",
                                        "total", "subtotal"):
            continue
        nums = [n for n in (ps.num(c) for c in cells[1:]) if n is not None]
        if not nums:
            continue
        qty   = nums[0]
        avg   = nums[1] if len(nums) >= 2 else None
        price = nums[2] if len(nums) >= 3 else None
        mval  = nums[-1] if len(nums) >= 2 else None
        if qty is None or qty <= 0:
            continue
        if price is None and mval is not None and qty:
            price = mval / qty
        holdings.append({
            "symbol":        name, "isin": "", "exchange": "SGX",
            "quantity":      str(qty),
            "avg_cost":      str(avg if avg is not None else (price or 0)),
            "current_price": str(price if price is not None else (avg or 0)),
        })
    cash = _cash_from_dom(page)
    return holdings, cash


def _cash_from_dom(page) -> Decimal | None:
    try:
        cash_txt = page.evaluate("""() => {
          const labels = ['cash balance','available balance','cash','settlement'];
          const els = Array.from(document.querySelectorAll('*'));
          for (const lbl of labels) {
            for (const el of els) {
              const t = (el.innerText || '').toLowerCase();
              if (t.includes(lbl) && t.length < 60)
                return (el.parentElement && el.parentElement.innerText) || el.innerText;
            }
          }
          return '';
        }""")
        if cash_txt:
            m = re.search(r'[-+]?[\d,]+\.?\d*', cash_txt.replace("S$", " ").replace("$", " "))
            if m:
                return ps.num(m.group(0))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Transactions — date-filtered activity → signed flows
# ---------------------------------------------------------------------------
def open_transactions(page, cfg: DbsConfig):
    ps.click(page, [
        'a:has-text("Transactions")', 'a:has-text("Transaction History")',
        'a:has-text("Activity")', 'a:has-text("History")', 'span:has-text("Transactions")',
        '[href*="transaction" i]', '[href*="activity" i]',
    ], "Transactions nav", optional=True)
    page.wait_for_timeout(3000)
    ps.shot(page, CFG, f"dbs_transactions_{cfg.prefix}.png")


def _apply_date_filter(page, from_date: date, to_date: date):
    for iso, names in ((from_date.isoformat(), ("from", "start", "fromdate", "startdate")),
                       (to_date.isoformat(),   ("to", "end", "todate", "enddate"))):
        sels = [f'input[name*="{n}" i]' for n in names] + \
               [f'input[id*="{n}" i]' for n in names] + \
               [f'input[placeholder*="{n}" i]' for n in names]
        ps.fill(page, sels + ['input[type="date"]'], iso, f"date {iso}",
                clear=True, optional=True)
    ps.click(page, ['button:has-text("Apply")', 'button:has-text("Filter")',
                    'button:has-text("Search")', 'button:has-text("Go")', 'button:has-text("View")'],
             "apply date filter", optional=True)


def scrape_transactions(page, cfg: DbsConfig, from_date: date, to_date: date) -> list[dict]:
    open_transactions(page, cfg)
    _apply_date_filter(page, from_date, to_date)
    page.wait_for_timeout(3000)
    ps.shot(page, CFG, f"dbs_transactions_filtered_{cfg.prefix}.png")

    txns = _txns_from_dom(page)
    out = []
    for t in txns:
        try:
            d = date.fromisoformat(t["date"])
        except Exception:
            continue
        if from_date <= d <= to_date:
            out.append(t)
    logger.info(f"[{cfg.code}] {len(out)} transactions in {from_date}..{to_date} "
                f"(parsed {len(txns)} total)")
    return out


def _txns_from_dom(page) -> list[dict]:
    grid = page.evaluate("""() => {
      const tables = Array.from(document.querySelectorAll('table'));
      let target = null;
      for (const t of tables) {
        const h = (t.innerText || '').toLowerCase();
        if (h.includes('date') && (h.includes('description') || h.includes('security')
            || h.includes('type') || h.includes('amount') || h.includes('quantity'))) {
          target = t; break; }
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
    i_sym   = col("security", "symbol", "ticker", "stock", "description", "name")
    i_type  = col("type", "side", "action", "transaction", "description")
    i_amt   = col("amount", "value", "total")
    i_qty   = col("quantity", "units", "shares", "qty")
    i_price = col("price")

    out = []
    for cells in rows:
        def at(i):
            return cells[i] if (i is not None and i < len(cells)) else None
        dm = re.search(r'\d{4}-\d{2}-\d{2}', str(at(i_date)) or "")
        sym = at(i_sym)
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
def process_entity(cfg: DbsConfig) -> dict:
    today = date.today()
    cache = _feed.load_cache_obj(BROKER.upper(), cfg.code)
    from_d, to_d, first_run = ps.incremental_range(cache, today)
    path = None

    with ps.browser(CFG) as (ctx, page):
        try:
            login(page, cfg)
            open_holdings(page, cfg)

            holdings: list[dict] = []
            cash: Decimal | None = None
            if DOWNLOAD_FILE:
                path = download_statement(page, cfg, "holdings")
                if path is not None:
                    try:
                        holdings, cash = parse_statement(path, cfg)
                    except (DbsSchemaUnknown, NotImplementedError):
                        logger.info(f"[{cfg.code}] statement mapping pending — DOM parse fallback")
            if not holdings:
                holdings, dom_cash = holdings_from_dom(page)
                if cash is None:
                    cash = dom_cash
            if not holdings:
                ps.shot(page, CFG, f"dbs_empty_{cfg.prefix}.png")
                raise RuntimeError("no holdings parsed from DBS")

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
            if path:
                try:
                    os.remove(path)
                except OSError:
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
        logger.warning("No DBS entities configured in .env — nothing to do.")
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
            logger.info(f"=== [{cfg.code}] DBS sync ===")
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
        ps.log_run(conn, "dbs", status, processed, failed, started,
                   error="; ".join(errors) if errors else None)
        logger.info(f"=== DBS done: {processed} holdings, {failed} failed ===")
    finally:
        conn.close()


def main():
    lock = ps.Lock("dbs_worker")
    if not lock.acquire():
        sys.exit(1)
    try:
        run()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
