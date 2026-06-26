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

# Market-hours guards — the worker runs every minute and fetches each index only
# while ITS OWN exchange is open: Indian indices on NSE hours (IST), US indices on
# NYSE/NASDAQ hours (US Eastern, DST-aware via zoneinfo). So Nifty/Sensex track the
# Indian session and Dow/NASDAQ track the live US session.
IST = zoneinfo.ZoneInfo("Asia/Kolkata")
NY  = zoneinfo.ZoneInfo("America/New_York")


def _within(now, open_hm, close_hm) -> bool:
    if now.weekday() >= 5:            # Sat / Sun
        return False
    return open_hm <= (now.hour, now.minute) < close_hm


def is_nse_open() -> bool:
    return _within(datetime.now(IST), (9, 15), (15, 30))   # 09:15–15:30 IST


def is_us_open() -> bool:
    return _within(datetime.now(NY), (9, 30), (16, 0))     # 09:30–16:00 ET

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# benchmark code -> (Yahoo symbol, market). Each index is fetched only while its
# own market is open ('IN' = NSE/IST, 'US' = NYSE-NASDAQ/ET).
INDEX_SYMBOLS = {
    "NIFTY":    ("^NSEI",  "IN"),
    "SENSEX":   ("^BSESN", "IN"),
    "DOWJONES": ("^DJI",   "US"),
    "NASDAQ":   ("^IXIC",  "US"),
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
    nse_open, us_open = is_nse_open(), is_us_open()
    if not force and not (nse_open or us_open):
        logger.info("Both markets closed — skipping benchmark fetch (use --force to override).")
        return

    market_open = {"IN": nse_open, "US": us_open}
    today = date.today()
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    ok = attempted = 0
    for code, (symbol, market) in INDEX_SYMBOLS.items():
        if not force and not market_open.get(market, False):
            continue                       # this index's exchange is shut right now
        attempted += 1
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
    logger.info("Benchmark worker done (%d/%d fetched; NSE %s, US %s).",
                ok, attempted, "open" if nse_open else "closed", "open" if us_open else "closed")


if __name__ == "__main__":
    main()
