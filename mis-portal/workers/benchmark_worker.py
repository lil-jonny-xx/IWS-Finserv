#!/usr/bin/env python3
"""
Benchmark Worker — IWS MIS Portal

Fetches daily levels for the equity-index benchmarks (Nifty, Sensex) and stores
them in market_benchmark.  GS-bond YTM/price have no free live feed and are
entered manually via the portal, so they are not touched here.

Source: Yahoo Finance chart API (no key required) — ^NSEI, ^BSESN.
Runs every minute during market hours (exits immediately off-hours), updating today's
row in place so "current" stays live while one row/day feeds prev-week & 31-Mar history.

  # cron (every minute; the worker self-guards market hours):
  * * * * * /var/www/mis-portal/venv/bin/python -m workers.benchmark_worker >> /var/log/mis-portal-benchmark.log 2>&1
"""
import os
import sys
import logging
import zoneinfo
import requests
import psycopg2
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

# Market-hours guard — lets this run every minute (mirrors equity_price_worker).
IST          = zoneinfo.ZoneInfo("Asia/Kolkata")
MARKET_OPEN  = (9, 15)    # 09:15 IST
MARKET_CLOSE = (15, 30)   # 15:30 IST


def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:            # Sat / Sun
        return False
    return MARKET_OPEN <= (now.hour, now.minute) < MARKET_CLOSE

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# benchmark code -> Yahoo symbol
INDEX_SYMBOLS = {
    "NIFTY":  "^NSEI",
    "SENSEX": "^BSESN",
}
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_index(symbol: str) -> float:
    r = requests.get(YF_URL.format(sym=symbol), timeout=10, headers=HEADERS)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    if price is None:
        raise ValueError(f"No price in Yahoo response for {symbol}")
    return float(price)


def main():
    force = "--force" in sys.argv or os.getenv("BENCHMARK_FORCE") == "1"
    if not force and not is_market_open():
        logger.info("Market closed — skipping benchmark fetch (use --force to override).")
        return

    today = date.today()
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    ok = 0
    for code, symbol in INDEX_SYMBOLS.items():
        try:
            value = fetch_index(symbol)
        except Exception as e:
            logger.error("Failed to fetch %s (%s): %s", code, symbol, e)
            continue
        cur.execute("""
            INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_at)
            VALUES (%s,
                    COALESCE((SELECT label FROM market_benchmark WHERE code = %s ORDER BY as_of_date LIMIT 1), %s),
                    %s, %s, 'index', 'yahoo', NOW())
            ON CONFLICT (code, as_of_date)
            DO UPDATE SET value = EXCLUDED.value, source = 'yahoo', updated_at = NOW()
        """, (code, code, code, today, value))
        logger.info("%s = %s", code, value)
        ok += 1
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Benchmark worker done (%d/%d indices updated).", ok, len(INDEX_SYMBOLS))


if __name__ == "__main__":
    main()
