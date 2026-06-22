#!/usr/bin/env python3
"""
Historical FX backfill — IWS MIS Portal

The daily fx_rates_worker only stores the *latest* rate. Foreign-equity realised
gains value each trade leg at its own trade-date rate (see report_generator._fx_rate_on),
so we need historical currency→INR rates going back to the oldest foreign trade.

This worker backfills fx_rate for every currency present in equity_trade_ledger,
from the earliest trade_date (or --start) up to today, using frankfurter.app's
time-series endpoint (no API key, business-day rates). Weekend/holiday trade dates
are covered by the nearest-prior business day at read time.

  python -m workers.fx_backfill_worker                 # backfill from ledger min date
  python -m workers.fx_backfill_worker --start 2018-01-01
  python -m workers.fx_backfill_worker --currencies USD,SGD --dry-run

Idempotent: upserts on (from_currency, to_currency, rate_date).
"""
import os
import sys
import argparse
import logging
from datetime import datetime, date, timezone
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
FRANKFURTER = "https://api.frankfurter.app"


def get_conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def ledger_scope(conn, currencies_override):
    """(currencies, earliest_trade_date) from equity_trade_ledger."""
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT currency FROM equity_trade_ledger
                   WHERE currency IS NOT NULL AND upper(currency) <> 'INR'""")
    currencies = currencies_override or sorted(r["currency"] for r in cur.fetchall())
    cur.execute("SELECT min(trade_date) AS d FROM equity_trade_ledger")
    row = cur.fetchone()
    cur.close()
    return currencies, (row["d"] if row else None)


def fetch_series(currency, start, end):
    """{date_obj: rate(currency→INR)} from frankfurter time series (USD base)."""
    to = "INR" if currency == "USD" else f"{currency},INR"
    url = f"{FRANKFURTER}/{start}..{end}?from=USD&to={to}"
    logger.info(f"GET {url}")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    rates = resp.json().get("rates", {})
    out = {}
    for ds, vals in rates.items():
        inr_per_usd = vals.get("INR")
        if not inr_per_usd:
            continue
        if currency == "USD":
            rate = inr_per_usd
        else:
            cur_per_usd = vals.get(currency)
            if not cur_per_usd:
                continue
            rate = inr_per_usd / cur_per_usd
        out[datetime.strptime(ds, "%Y-%m-%d").date()] = round(rate, 6)
    return out


def upsert(conn, currency, series):
    cur = conn.cursor()
    n = 0
    for d, rate in series.items():
        cur.execute("""
            INSERT INTO fx_rate (from_currency, to_currency, rate_date, rate, created_at)
            VALUES (%s, 'INR', %s, %s, %s)
            ON CONFLICT (from_currency, to_currency, rate_date)
            DO UPDATE SET rate = EXCLUDED.rate, created_at = EXCLUDED.created_at
        """, (currency, d, rate, datetime.now(timezone.utc)))
        n += 1
    cur.close()
    return n


def main():
    ap = argparse.ArgumentParser(description="Backfill historical currency→INR rates for foreign trades.")
    ap.add_argument("--start", help="ISO date floor (default: earliest ledger trade)")
    ap.add_argument("--currencies", help="comma list, e.g. USD,SGD (default: all in ledger)")
    ap.add_argument("--dry-run", action="store_true", help="fetch & report; write nothing")
    args = ap.parse_args()

    conn = get_conn()
    override = [c.strip().upper() for c in args.currencies.split(",")] if args.currencies else None
    currencies, ledger_min = ledger_scope(conn, override)

    if not currencies:
        logger.info("No foreign currencies in equity_trade_ledger — nothing to backfill.")
        conn.close()
        return

    start = args.start or (ledger_min.isoformat() if ledger_min else None)
    if not start:
        logger.info("No trades and no --start given — nothing to backfill.")
        conn.close()
        return
    end = date.today().isoformat()
    logger.info(f"Backfilling {currencies} from {start} to {end}")

    total = 0
    for cur_code in currencies:
        try:
            series = fetch_series(cur_code, start, end)
        except Exception as e:
            logger.error(f"{cur_code}: fetch failed — {e}")
            continue
        if args.dry_run:
            logger.info(f"{cur_code}: would upsert {len(series)} rows "
                        f"({min(series) if series else '-'}..{max(series) if series else '-'})")
            total += len(series)
            continue
        total += upsert(conn, cur_code, series)

    if not args.dry_run:
        conn.commit()
    conn.close()
    verb = "would upsert" if args.dry_run else "upserted"
    logger.info(f"Done: {verb} {total} fx_rate rows across {len(currencies)} currencies.")


if __name__ == "__main__":
    main()
