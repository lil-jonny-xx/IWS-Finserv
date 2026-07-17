#!/usr/bin/env python3
"""
Inflation (CPI / WPI, India + US) from the IMF into market_benchmark.

Source: IMF SDMX 3.0 at https://api.imf.org/external/sdmx/3.0 — no API key.
The published OpenAPI advertises an `Ocp-Apim-Subscription-Key`, but these
endpoints answer unauthenticated, so nothing needs provisioning.

Why the IMF and not the obvious candidates (all checked, all rejected):
  * FRED           — its India CPI series stop at Mar-2025; OECD is retiring them.
                     (Still the right source for US mortgage rates — different worker.)
  * data.gov.in    — the headline "All India CPI" resource was last updated in 2014.
  * API Ninjas     — /v1/inflation is premium-only; the free tier cannot serve it.
  * RBI DBIE       — has the data behind an undocumented, unversioned gateway.
The IMF is current to within ~2 months, is a single source for both countries, and
is a documented, versioned API.

Series (dataflow / key):
  CPI / {C}.CPI._T.IX.M             headline CPI index, _T = all-items total
                                    (CP01..CP12 are COICOP sub-baskets — not the headline)
  CPI / {C}.CPI._T.YOY_PCH_PA_PT.M  CPI inflation %, year-on-year, computed by the IMF
  PPI / IND.WPI.IX.M                India's actual WPI. The IMF files it inside the PPI
                                    dataflow under a WPI indicator — it is not the PPI.
  PPI / IND.WPI.YOY_PCH_PT.M        WPI inflation %, year-on-year

The two dataflows spell the year-on-year transformation DIFFERENTLY — CPI uses
YOY_PCH_PA_PT, PPI uses YOY_PCH_PT. Using one code for both silently returns an
empty series rather than an error, so the codes are per-series here on purpose.

Monthly data, but polled WEEKLY rather than monthly: the IMF republishes on its own
schedule and already runs ~2 months behind, so a fixed monthly run that lands just
before a print waits another full month to notice it. Weekly catches it within 7
days, and the run is idempotent and costs 6 requests. Not part of the minute-by-
minute benchmark loop either way.

  # cron — Fridays 19:15 IST (13:45 UTC):
  45 13 * * 5 /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py workers/imf_inflation_worker.py --commit >> /var/log/mis-portal-imf-inflation.log 2>&1

Values land in market_benchmark under the same (code, as_of_date) shape as
everything else, so /api/v1/benchmarks picks them up with no API change.

Note on as_of_date: a monthly observation (2026-M05) is stored on the FIRST of that
month. _fetch_benchmarks walks the series with "value at-or-before" semantics, so a
May reading correctly remains "current" through June and July until June publishes.

Dry-run by default; pass --commit to write.
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

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

IMF_BASE = "https://api.imf.org/external/sdmx/3.0"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# How many recent observations to pull. Generous: it backfills history on the first
# run so week/YTD comparisons have something to walk, and is idempotent after.
LAST_N = 120

# code -> (dataflow, sdmx key, label, unit)
SERIES = {
    "IN_CPI":       ("CPI", "IND.CPI._T.IX.M",             "India CPI (index)",   "index"),
    "IN_CPI_YOY":   ("CPI", "IND.CPI._T.YOY_PCH_PA_PT.M", "India CPI inflation", "pct_raw"),
    "IN_WPI":       ("PPI", "IND.WPI.IX.M",                "India WPI (index)",   "index"),
    "IN_WPI_YOY":   ("PPI", "IND.WPI.YOY_PCH_PT.M",        "India WPI inflation", "pct_raw"),
    "US_CPI":       ("CPI", "USA.CPI._T.IX.M",             "US CPI (index)",      "index"),
    "US_CPI_YOY":   ("CPI", "USA.CPI._T.YOY_PCH_PA_PT.M", "US CPI inflation",    "pct_raw"),
}


def fetch_series(dataflow: str, key: str) -> list[tuple[date, float]]:
    """[(period_start_date, value)] for one SDMX key, oldest first.

    Returns [] rather than raising when the IMF has the series but no observations,
    so one empty series can't abort the run.
    """
    url = (f"{IMF_BASE}/data/dataflow/IMF.STA/{dataflow}/+/{urllib.parse.quote(key)}"
           f"?lastNObservations={LAST_N}&dimensionAtObservation=TIME_PERIOD")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)

    data = d.get("data") or {}
    if not data.get("structures") or not data.get("dataSets"):
        return []
    st = data["structures"][0]
    periods = [v.get("value") or v.get("id") for v in st["dimensions"]["observation"][0]["values"]]
    out: list[tuple[date, float]] = []
    for s in (data["dataSets"][0].get("series") or {}).values():
        for oi, ov in (s.get("observations") or {}).items():
            # observation array is [OBS_VALUE, PRECISION, DERIVATION_TYPE, REFERENCE_PERIOD, STATUS]
            val = ov[0] if ov else None
            if val is None:
                continue
            p = periods[int(oi)]
            d_ = _period_to_date(p)
            if d_:
                out.append((d_, float(val)))
    return sorted(set(out))


def _period_to_date(p: str) -> date | None:
    """'2026-M05' -> 2026-05-01. Monthly only; anything else is ignored."""
    if not p or "-M" not in p:
        return None
    try:
        y, m = p.split("-M")
        return date(int(y), int(m), 1)
    except (ValueError, TypeError):
        return None


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
    total = ok = 0
    empty = []

    for code, (flow, key, label, unit) in SERIES.items():
        try:
            pts = fetch_series(flow, key)
        except Exception as e:
            logger.warning(f"  {code:<12} {key:<28} FAILED — {str(e)[:60]}")
            continue
        if not pts:
            logger.warning(f"  {code:<12} {key:<28} no observations")
            empty.append(code)
            continue
        if args.commit:
            for d_, v in pts:
                cur.execute("""
                    INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_at)
                    VALUES (%s,%s,%s,%s,%s,'imf',NOW())
                    ON CONFLICT (code, as_of_date) DO UPDATE
                      SET value = EXCLUDED.value, label = EXCLUDED.label,
                          unit  = EXCLUDED.unit,  source = 'imf', updated_at = NOW()
                """, (code, label, d_, v, unit))
            conn.commit()
        total += len(pts)
        ok += 1
        logger.info(f"  {code:<12} {key:<28} {len(pts):>4} obs  {pts[0][0]} → {pts[-1][0]}  "
                    f"latest={pts[-1][1]:.2f}")
        time.sleep(0.2)

    logger.info(f"\n{ok}/{len(SERIES)} series, {total} observations "
                f"{'written' if args.commit else 'found (dry-run)'}")
    if empty:
        logger.info(f"no data: {', '.join(empty)}")
    if not args.commit:
        logger.info("\nDRY RUN — nothing written. Re-run with --commit.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
