#!/usr/bin/env python3
"""
US lending & deposit rates from FRED into market_benchmark.

Covers two of the rates asked for on the Overview rail:
  * US home-loan rate  — MORTGAGE30US, Freddie Mac's 30-year fixed average.
                         Weekly, published Thursdays, series runs back to 1971.
  * US FD rate         — NDR12MCD, the FDIC's national average rate on a 12-month
                         CD under $100k. Monthly. A US "CD" is the instrument an
                         Indian FD corresponds to; there is no US series literally
                         called a fixed deposit.

Their INDIAN counterparts are NOT here, and not by omission: no free API publishes
average Indian FD or home-loan rates. The IMF's MFS_IR interest-rate dataflow has
zero series for India, RBI puts them only behind an undocumented gateway, and
data.gov.in's rate resources are long stale. Those two stay manual — the admin
rates panel is the plan for them.

Distinct from benchmark_worker's FRED fallback: that one stands in for Yahoo on
codes both quote. These are codes Yahoo has no equivalent for at all, so FRED is
the primary and only source. Weekly/monthly data, so this runs on a weekly cron
rather than the minute loop.

  # cron — Fridays 19:00 IST (13:30 UTC), after Freddie Mac's Thursday publication:
  30 13 * * 5 /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py workers/fred_rates_worker.py --commit >> /var/log/mis-portal-fred-rates.log 2>&1

Run via cron_wrapper.py (which emails on a non-zero exit) and /var/www/.venv, as
every other worker here does — mis-portal/venv is the backend's, not cron's.

Dry-run by default; pass --commit to write.
"""
import argparse
import logging
import os
import time
from datetime import date

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# How much history to pull. Enough to backfill the series on first run so the
# rail has something behind it; idempotent on every run after.
LIMIT = 260

# code -> (FRED series id, label, unit)
SERIES = {
    "US_MORTGAGE30": ("MORTGAGE30US", "US 30-yr mortgage",  "pct_raw"),
    "US_FD_12M":     ("NDR12MCD",     "US 12-mo CD (FD)",   "pct_raw"),
}


def fetch_series(series_id: str) -> list[tuple[date, float]]:
    """[(date, value)] oldest first. FRED marks a missing print with "." — those
    are dropped rather than coerced to zero, which would read as a real 0% rate."""
    key = os.getenv("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY not set in /var/www/mis-portal/.env")
    r = requests.get(FRED_URL, timeout=30, params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "sort_order": "desc", "limit": LIMIT,
    })
    r.raise_for_status()
    out = [(date.fromisoformat(o["date"]), float(o["value"]))
           for o in r.json().get("observations", [])
           if o.get("value") not in (".", "", None)]
    return sorted(out)


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
    ok = total = 0

    for code, (sid, label, unit) in SERIES.items():
        try:
            pts = fetch_series(sid)
        except Exception as e:
            logger.warning(f"  {code:<15} {sid:<14} FAILED — {str(e)[:60]}")
            continue
        if not pts:
            logger.warning(f"  {code:<15} {sid:<14} no observations")
            continue
        if args.commit:
            for d_, v in pts:
                cur.execute("""
                    INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_at)
                    VALUES (%s,%s,%s,%s,%s,'fred',NOW())
                    ON CONFLICT (code, as_of_date) DO UPDATE
                      SET value = EXCLUDED.value, label = EXCLUDED.label,
                          unit  = EXCLUDED.unit,  source = 'fred', updated_at = NOW()
                """, (code, label, d_, v, unit))
            conn.commit()
        total += len(pts)
        ok += 1
        logger.info(f"  {code:<15} {sid:<14} {len(pts):>4} obs  {pts[0][0]} → {pts[-1][0]}  "
                    f"latest={pts[-1][1]:.2f}%")
        time.sleep(0.2)

    logger.info(f"\n{ok}/{len(SERIES)} series, {total} observations "
                f"{'written' if args.commit else 'found (dry-run)'}")
    if not args.commit:
        logger.info("\nDRY RUN — nothing written. Re-run with --commit.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
