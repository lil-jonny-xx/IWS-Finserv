#!/usr/bin/env python3
"""
India WPI (headline, "All commodities") from the Office of the Economic Adviser.

Why not the IMF, which already serves CPI in imf_inflation_worker: the IMF's India
WPI series (PPI dataflow, IND.WPI.IX.M) stops at Oct-2025 and has for months, while
the portal showed that stamp as though it were current. The Office of the Economic
Adviser is the body that PUBLISHES India's WPI — the IMF is republishing it with a
lag — and it is ~6 months ahead of the IMF's copy.

Same-basis check (the bar every source in benchmark_worker has to clear before it may
share a code): year-on-year computed from this file was compared against the IMF's own
YoY series on the SAME months. Jan-2025..Aug-2025 agree to 0.00pp; Sep/Oct-2025 differ
by 0.06/0.19pp, which is this source carrying REVISED figures where the IMF snapshot
still holds the provisional print. Same quantity, same basis, fresher and more correct.

But NOT the same index base: this file is 2011-12=100 (Oct-2025 = 155.1) where the
IMF's copy is rebased (Oct-2025 = 190.41). market_benchmark is one series per code and
the API derives week%/YTD% by walking it, so appending these rows onto the IMF's would
inject a permanent ~23% cliff at the join. The whole series is therefore REPLACED, not
appended — this file carries full history back to Apr-2012, so nothing is lost.
IN_WPI/IN_WPI_YOY were removed from imf_inflation_worker at the same time; if they are
ever put back, the two workers will fight over the code every week.

YoY is derived here from the index rather than downloaded separately: it is a ratio, so
it is base-independent, and it saves depending on a second file that lags differently.

The published file lags its own filename — monthly_index_202606.xls carried data to
Apr-2026. The download link is scraped rather than constructed from today's date for
that reason.

  # cron — Fridays 19:20 IST (13:50 UTC), just after the IMF CPI worker:
  50 13 * * 5 /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py workers/wpi_worker.py --commit >> /var/log/mis-portal-imf-inflation.log 2>&1

Logging deliberately shares the IMF worker's existing log file rather than opening a
mis-portal-wpi.log of its own: /var/log is root-owned, so cron redirecting to a file
that does not yet exist dies SILENTLY at the shell redirect — the command never runs.
That trap has already killed three workers here. A new log would need
deploy/tmpfiles/mis-portal-logs.conf reinstalled as root before this worker would run
at all; reusing the inflation log (same subject, runs 5 minutes apart) needs no sudo
and cannot fail that way.

Dry-run by default; pass --commit to write.
"""
import argparse
import io
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date

import pandas as pd
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

BASE     = "https://eaindustry.nic.in"
INDEX_PAGE = f"{BASE}/download_data_1112.asp"
UA       = {"User-Agent": "Mozilla/5.0"}

# The headline "All commodities" line. Matched on COMM_CODE, not COMM_NAME: the code is
# a stable identifier while the name is free text that has carried stray whitespace.
HEADLINE_CODE = 1000000000

# Column header form: INDX + MM + YYYY, e.g. INDX042026 = April 2026.
COL_RE = re.compile(r"^INDX(\d{2})(\d{4})$")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

SOURCE = "eaindustry"
CODE_INDEX = ("IN_WPI",     "India WPI (index)",   "index")
CODE_YOY   = ("IN_WPI_YOY", "India WPI inflation", "pct_raw")


def latest_workbook_url() -> str:
    """URL of the newest monthly WPI workbook, scraped from the downloads page.

    Constructing the name from today's date would 404 for most of every month: the
    file is published on the Office's own schedule and its data trails its filename.
    """
    req = urllib.request.Request(INDEX_PAGE, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        html = r.read().decode("utf-8", "replace")

    hrefs = re.findall(r'href="([^"]*monthly_index_(\d{6})\.xls[x]?)"', html, re.I)
    if not hrefs:
        raise RuntimeError(f"No monthly_index_YYYYMM.xls link found on {INDEX_PAGE}")
    # Several vintages are listed; take the newest by the YYYYMM in the name.
    href, stamp = max(hrefs, key=lambda h: h[1])
    logger.info("Latest workbook: %s (published %s)", href, stamp)
    return urllib.parse.urljoin(BASE + "/", href)


def fetch_headline() -> list[tuple[date, float]]:
    """[(month_start, index_value)] for headline WPI, oldest first."""
    url = latest_workbook_url()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        blob = r.read()
    df = pd.read_excel(io.BytesIO(blob))

    match = df[df["COMM_CODE"] == HEADLINE_CODE]
    if len(match) != 1:
        raise RuntimeError(f"Expected exactly 1 headline row (COMM_CODE={HEADLINE_CODE}), got {len(match)}")
    row = match.iloc[0]

    out: list[tuple[date, float]] = []
    for col in df.columns:
        m = COL_RE.match(str(col))
        if not m:
            continue
        val = row[col]
        # Trailing months exist as columns before they are published, and arrive
        # blank. Skip rather than write a NULL that would read as a gap.
        if pd.isna(val):
            continue
        mm, yyyy = int(m.group(1)), int(m.group(2))
        out.append((date(yyyy, mm, 1), float(val)))
    out.sort()
    if not out:
        raise RuntimeError("Headline row parsed but held no observations")
    return out


def to_yoy(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Year-on-year % per month, for months whose year-earlier month exists.

    Base-independent (it is a ratio of two readings on the same base), which is why
    rebasing the source does not change this series.
    """
    by_month = {d: v for d, v in series}
    out = []
    for d, v in series:
        prior = by_month.get(date(d.year - 1, d.month, 1))
        if prior:
            out.append((d, (v / prior - 1.0) * 100.0))
    return out


def replace_series(cur, code: str, label: str, unit: str, series: list[tuple[date, float]]) -> int:
    """Delete this code's rows and re-insert, so a rebase can never leave a mixed series.

    Scoped to rows this worker and the IMF own; a hand-entered override (source=
    'manual') is left alone, matching benchmark_worker's rule of only touching codes
    it owns.
    """
    cur.execute(
        "DELETE FROM market_benchmark WHERE code = %s AND source IN (%s, 'imf')",
        (code, SOURCE),
    )
    removed = cur.rowcount
    cur.executemany(
        """
        INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (code, as_of_date)
        DO UPDATE SET value = EXCLUDED.value, label = EXCLUDED.label,
                      unit = EXCLUDED.unit, source = EXCLUDED.source, updated_at = NOW()
        """,
        [(code, label, d, v, unit, SOURCE) for d, v in series],
    )
    logger.info("  %-11s %3d obs  %s → %s  latest=%.2f  (replaced %d)",
                code, len(series), series[0][0], series[-1][0], series[-1][1], removed)
    return len(series)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="write to the DB (default: dry run)")
    args = ap.parse_args()

    try:
        index = fetch_headline()
    except Exception as e:
        logger.error("WPI fetch failed: %s", e)
        return 1
    yoy = to_yoy(index)

    logger.info("Headline WPI: %d obs, %s → %s (latest %.2f, YoY %.2f%%)",
                len(index), index[0][0], index[-1][0], index[-1][1],
                yoy[-1][1] if yoy else float("nan"))

    if not args.commit:
        logger.info("Dry run — pass --commit to write.")
        return 0

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    try:
        n = replace_series(cur, *CODE_INDEX, index)
        n += replace_series(cur, *CODE_YOY, yoy)
        conn.commit()
        logger.info("2/2 series, %d observations written", n)
        return 0
    except Exception as e:
        conn.rollback()
        logger.error("Write failed, rolled back: %s", e)
        return 1
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
