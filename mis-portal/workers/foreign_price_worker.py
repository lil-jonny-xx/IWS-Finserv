#!/usr/bin/env python3
"""
Foreign Equity Price Worker — IWS MIS Portal

Refreshes the live native-currency price for every row in foreign_equity_holding
from an INDEPENDENT market-data feed, then recomputes the INR-denominated metrics
in-place — exactly the AMFI-for-mutual-funds pattern, but for US equities.

Why a separate feed: foreign holdings are otherwise only priced when the broker
scraper runs (Vested ~once/day, IBKR Flex EOD). This worker decouples price from
the scrape so values stay fresh and survive a broker outage.

Price source — first hit wins, per symbol:
  1. Finnhub      (FINNHUB_API_KEY)      — real-time US quotes
  2. Twelve Data  (TWELVEDATA_API_KEY)   — real-time-ish, global-capable
  3. yfinance     (no key)               — always-available fallback

Provider tickers can differ from the broker symbol (e.g. an LSE listing needs the
".L" suffix); set foreign_equity_holding.symbol_override to the provider ticker for
those. Plain US symbols (NASDAQ/NYSE) need no override.

Metrics follow the same conventions as equity_sync_worker (INR-based; CAGR only
once held ≥1 year — sub-year holdings show the absolute return instead). pnl_ytd /
returns_ytd_pct are left untouched (owned by equity_txn_metrics_worker).

pnl_daily (today vs prior close, INR) is computed here from the feed's previous
close for the NON-ibkr foreign brokers (Vested/DBS). ibkr rows are skipped — their
daily P&L is owned by the realtime stream (equity/ibkr_stream_sink) — so the two
writers never touch the same row.

Dry-run by default; --commit to write. --snapshot also writes today's EOD row to
foreign_equity_holding_history (auto-written anyway in the last 5 min before US close).
--force runs even when the US market is closed.

Scheduling: driven by mis-portal-foreign-price.timer (fires every minute). The worker
self-guards US regular hours, so it only spends API quota 09:30–16:00 ET. With Finnhub
free (60 calls/min) and ~45 symbols = 45 calls/run, a 1-minute cadence stays under the
limit while being as close to real-time as the quota allows.
"""
import os
import sys
import argparse
import logging
import zoneinfo
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity import fx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

TWO  = Decimal("0.01")
FOUR = Decimal("0.0001")
TODAY = date.today()
ANNUALISE_MIN_DAYS = 365   # CAGR only once held ≥1 year (matches equity_sync_worker)

# US regular session (DST-safe via the America/New_York zone). The timer fires every
# minute but the worker exits instantly outside this window, so no API quota is spent
# when US prices aren't moving.
US_TZ = zoneinfo.ZoneInfo("America/New_York")
US_OPEN_MIN  = 9 * 60 + 30   # 09:30 ET
US_CLOSE_MIN = 16 * 60       # 16:00 ET
EOD_SNAPSHOT_WINDOW_MINUTES = 5   # auto-write the daily history row in the last 5 min


def us_market_state():
    """Return (is_open, in_eod_window) for the US regular session right now."""
    now = datetime.now(US_TZ)
    if now.weekday() >= 5:
        return False, False
    mins = now.hour * 60 + now.minute
    is_open = US_OPEN_MIN <= mins < US_CLOSE_MIN
    in_eod  = (US_CLOSE_MIN - EOD_SNAPSHOT_WINDOW_MINUTES) <= mins < US_CLOSE_MIN
    return is_open, in_eod


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ---------------------------------------------------------------------------
# Price sources — each returns a positive float price or None
# ---------------------------------------------------------------------------
# Each source returns (price, prev_close) — prev_close is the previous session's
# close, used for daily P&L, or None when the source doesn't carry it (Twelve Data's
# /price endpoint). Both come from the SAME request, so daily P&L costs no extra quota.
def _finnhub(symbol: str, key: str, session: requests.Session):
    # Don't use raise_for_status(): its message echoes the full URL incl. the token
    # into the worker log. Raise a clean status-only error instead.
    r = session.get("https://finnhub.io/api/v1/quote",
                    params={"symbol": symbol, "token": key}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    j = r.json()
    c  = j.get("c")                # current price; 0 == no data for the symbol
    pc = j.get("pc")              # previous close
    return (float(c) if c else None, float(pc) if pc else None)


def _twelvedata(symbol: str, key: str, session: requests.Session):
    r = session.get("https://api.twelvedata.com/price",
                    params={"symbol": symbol, "apikey": key}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    p = r.json().get("price")
    return (float(p) if p else None, None)   # /price has no prev-close (daily P&L via others)


def _yfinance(symbol: str):
    import yfinance as yf
    fi = yf.Ticker(symbol).fast_info
    p  = fi.get("lastPrice") or fi.get("last_price")
    pc = fi.get("previousClose") or fi.get("previous_close")
    return (float(p) if p else None, float(pc) if pc else None)


def fetch_price(symbol: str, finnhub_key, td_key, session):
    """Try each source in priority order; return (price, prev_close, source_name).
    prev_close may be None even on a price hit (source doesn't carry it)."""
    sources = []
    if finnhub_key:
        sources.append(("finnhub",    lambda: _finnhub(symbol, finnhub_key, session)))
    if td_key:
        sources.append(("twelvedata", lambda: _twelvedata(symbol, td_key, session)))
    sources.append(("yfinance",       lambda: _yfinance(symbol)))

    for name, fn in sources:
        try:
            p, pc = fn()
            if p and p > 0:
                return p, pc, name
        except Exception as e:
            # Scrub keys: a network-level exception embeds the full request URL (incl.
            # the token) in its message, which would otherwise land in the worker log.
            msg = str(e)
            for k in (finnhub_key, td_key):
                if k:
                    msg = msg.replace(k, "***")
            logger.warning(f"  [{symbol}] {name} failed: {msg}")
    return None, None, None


# ---------------------------------------------------------------------------
# Metric recompute (INR-based, mirrors equity_sync_worker)
# ---------------------------------------------------------------------------
def recompute(h: dict, price_native: Decimal, fx_rate: Decimal,
              prev_close_native: Decimal | None = None) -> dict:
    qty  = Decimal(str(h["quantity"]))
    cost = Decimal(str(h["cost"] or 0))                 # cost is stored in INR
    cmv_native = (qty * price_native).quantize(TWO, ROUND_HALF_UP)
    price_inr  = (price_native * fx_rate).quantize(FOUR, ROUND_HALF_UP)
    cmv_inr    = (cmv_native * fx_rate).quantize(TWO, ROUND_HALF_UP)
    pnl_inc    = (cmv_inr - cost).quantize(TWO, ROUND_HALF_UP)

    # Daily P&L (today vs prior close), in INR. None when no prev-close is available
    # this run — the caller COALESCEs so the last good value is preserved, not wiped.
    pnl_daily = None
    if prev_close_native is not None and prev_close_native > 0:
        pnl_daily = (qty * (price_native - prev_close_native) * fx_rate).quantize(TWO, ROUND_HALF_UP)

    out = {
        "current_price_native":        price_native,
        "current_market_value_native": cmv_native,
        "current_price":               price_inr,
        "current_market_value":        cmv_inr,
        "pnl_inception":               pnl_inc,
        "pnl_daily":                   pnl_daily,
        "fx_rate":                     fx_rate,
        "returns_inception_pct":       None,
        "cagr_inception_pct":          None,
    }
    if cost > 0:
        out["returns_inception_pct"] = (pnl_inc / cost * 100).quantize(FOUR, ROUND_HALF_UP)
        fdt = h.get("first_invested_date")
        if fdt:
            days = (TODAY - fdt).days
            if days >= ANNUALISE_MIN_DAYS:
                ratio = float(cmv_inr / cost)
                if ratio > 0:
                    cagr = (ratio ** (365.0 / days) - 1.0) * 100
                    if -99.99 <= cagr <= 1000.0:
                        out["cagr_inception_pct"] = Decimal(str(round(cagr, 4)))
    return out


def write_snapshot(cur, h, m):
    cur.execute("""
        INSERT INTO foreign_equity_holding_history
            (entity_id, broker, symbol, snapshot_date, quantity,
             close_price, market_value, cost, pnl, currency, fx_rate,
             close_price_native, market_value_native)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING
    """, (h["entity_id"], h["broker"], h["symbol"], TODAY, h["quantity"],
          m["current_price"], m["current_market_value"], h["cost"], m["pnl_inception"],
          h.get("currency") or "USD", m["fx_rate"],
          m["current_price_native"], m["current_market_value_native"]))


def main():
    ap = argparse.ArgumentParser(description="Refresh foreign equity prices from an independent feed.")
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--snapshot", action="store_true", help="also write today's history row")
    ap.add_argument("--force", action="store_true", help="run even when the US market is closed")
    args = ap.parse_args()

    is_open, in_eod = us_market_state()
    if not is_open and not args.force:
        logger.info("US market closed — skipping (use --force to override)")
        return
    do_snapshot = args.snapshot or in_eod   # auto-snapshot in the last 5 min before close

    finnhub_key = os.getenv("FINNHUB_API_KEY", "").strip()
    td_key      = os.getenv("TWELVEDATA_API_KEY", "").strip()
    active = [n for n, k in (("finnhub", finnhub_key), ("twelvedata", td_key)) if k] + ["yfinance"]
    logger.info(f"Price sources (in order): {active}")

    conn = connect(); cur = conn.cursor()
    cur.execute("""SELECT feh.*, e.entity_name FROM foreign_equity_holding feh
                   JOIN entity e ON e.id = feh.entity_id ORDER BY e.entity_name, symbol""")
    rows = cur.fetchall()

    session = requests.Session()
    fx_cache: dict[str, Decimal] = {}
    updated = sources_used = 0
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — {len(rows)} foreign holding(s)\n")
    print(f"{'ENT':6}{'SYMBOL':10}{'SRC':11}{'PX(nat)':>11}{'VALUE(INR)':>15}"
          f"{'DAY(INR)':>13}{'INC%':>9}{'CAGR%':>9}")

    for h in rows:
        ticker = (h.get("symbol_override") or h["symbol"]).strip()
        price, prev_close, src = fetch_price(ticker, finnhub_key, td_key, session)
        if price is None:
            logger.warning(f"  [{h['symbol']}] no price from any source — keeping last value")
            continue
        sources_used += 1

        ccy = (h.get("currency") or "USD").upper()
        if ccy not in fx_cache:
            r = fx.get_rate(conn, ccy, TODAY) if ccy != "INR" else Decimal("1")
            fx_cache[ccy] = r if r is not None else Decimal(str(h["fx_rate"] or 1))
        fx_rate = fx_cache[ccy]

        m = recompute(h, Decimal(str(price)), fx_rate,
                      Decimal(str(prev_close)) if prev_close else None)
        # Daily P&L for ibkr rows is owned by the realtime stream (ibkr_stream_sink);
        # leave it to the stream so the two writers never fight (COALESCE keeps it).
        if h["broker"] == "ibkr":
            m["pnl_daily"] = None
        if args.commit:
            cur.execute("""UPDATE foreign_equity_holding SET
                             current_price_native=%(current_price_native)s,
                             current_market_value_native=%(current_market_value_native)s,
                             current_price=%(current_price)s,
                             current_market_value=%(current_market_value)s,
                             pnl_inception=%(pnl_inception)s,
                             pnl_daily=COALESCE(%(pnl_daily)s, pnl_daily),
                             returns_inception_pct=%(returns_inception_pct)s,
                             cagr_inception_pct=%(cagr_inception_pct)s,
                             fx_rate=%(fx_rate)s, updated_at=NOW()
                           WHERE id=%(id)s""", {**m, "id": h["id"]})
            if do_snapshot:
                write_snapshot(cur, h, m)
        updated += 1
        day = m["pnl_daily"]
        print(f"{h['entity_name'][:5]:6}{h['symbol'][:9]:10}{src:11}{price:>11,.2f}"
              f"{float(m['current_market_value']):>15,.0f}"
              f"{(float(day) if day is not None else 0):>13,.0f}"
              f"{float(m['returns_inception_pct'] or 0):>9.1f}"
              f"{float(m['cagr_inception_pct'] or 0):>9.1f}")

    if args.commit:
        conn.commit()
    print(f"\n{updated}/{len(rows)} priced ({sources_used} fetched). "
          f"{'committed.' if args.commit else 'dry-run — nothing written.'}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
