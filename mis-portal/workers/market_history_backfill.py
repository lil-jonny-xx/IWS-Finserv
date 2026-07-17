#!/usr/bin/env python3
"""
Backfill daily history into market_benchmark from Yahoo, for every code in
benchmark_worker.INDEX_SYMBOLS.

Why this is not optional: /api/v1/benchmarks derives week% and YTD% from the
market_benchmark HISTORY (report_generator._fetch_benchmarks walks the series and
takes the value at-or-before 7 days ago / 31-Mar). Without history a brand-new
code shows a price and two blanks, and only starts reading properly a week later.
Before this ran, the whole table held 78 rows across 22 days — enough for the four
original indices to show a week%, and nothing else.

Backfilling a year gives every code a real week% AND YTD% from the first render,
and leaves a series long enough to answer "how did gold do this FY" later.

The live benchmark_worker keeps writing today's row every minute; this only fills
in the past, and is safe to re-run (upsert on (code, as_of_date)).

Codes marked source='manual' (the hand-entered GS bond yields) are never touched.

Dry-run by default; pass --commit to write.
  python -m workers.market_history_backfill --commit
"""
import argparse
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import date

import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

from workers.benchmark_worker import INDEX_SYMBOLS  # noqa: E402  (single source of truth)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
UA = {"User-Agent": "Mozilla/5.0"}

# ?range= rather than ?period1/?period2 — the epoch form 400s on a number of
# perfectly valid tickers while range returns the same series (see fy_price_backfill).
RANGE = "1y"


def fetch_series(symbol: str) -> dict:
    url = f"{YF_CHART.format(sym=urllib.parse.quote(symbol))}?range={RANGE}&interval=1d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    res = (d.get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return {}
    closes = res["indicators"]["quote"][0]["close"]
    out = {}
    for ts, cl in zip(res["timestamp"], closes):
        if cl is None:
            continue
        out[date.fromtimestamp(ts)] = float(cl)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    cur = conn.cursor()
    today = date.today()
    total = ok = 0
    failed = []

    for code, (symbol, _market, label, unit) in INDEX_SYMBOLS.items():
        try:
            series = fetch_series(symbol)
        except Exception as e:
            logger.warning(f"  {code:<13} ({symbol}): {str(e)[:60]}")
            failed.append(code)
            continue
        if not series:
            logger.warning(f"  {code:<13} ({symbol}): no data")
            failed.append(code)
            continue
        n = 0
        for d, v in series.items():
            if d > today:
                continue
            if args.commit:
                cur.execute("""
                    INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_at)
                    VALUES (%s,%s,%s,%s,%s,'yahoo',NOW())
                    ON CONFLICT (code, as_of_date) DO UPDATE
                      SET value = EXCLUDED.value, label = EXCLUDED.label,
                          unit  = EXCLUDED.unit,  source = 'yahoo', updated_at = NOW()
                """, (code, label, d, v, unit))
            n += 1
        if args.commit:
            conn.commit()
        total += n
        ok += 1
        logger.info(f"  {code:<13} {symbol:<12} {n:>4} days  {min(series)} → {max(series)}")
        time.sleep(0.12)   # unofficial endpoint, no documented limit — be gentle

    logger.info(f"\n{ok}/{len(INDEX_SYMBOLS)} codes, {total} rows "
                f"{'written' if args.commit else 'found (dry-run)'}")
    if failed:
        logger.info(f"no data ({len(failed)}): {', '.join(failed)}")
    if not args.commit:
        logger.info("\nDRY RUN — nothing written. Re-run with --commit.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
