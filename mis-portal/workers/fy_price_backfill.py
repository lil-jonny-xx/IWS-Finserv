#!/usr/bin/env python3
"""
Backfill 31-March boundary closes for Indian equity, from Yahoo Finance.

These are the anchors the FY growth columns measure between: FY2025-26 is the
move from the 31-Mar-2025 close to the 31-Mar-2026 close. Our own
equity_holding_history only starts 2026-04-01 (and covers 119 of 225 holdings),
so it can anchor the current FY and nothing earlier — hence Yahoo, which covers
every boundary uniformly and needs no key.

Symbol resolution: broker series suffixes are not part of the exchange ticker
(GOLDBEES-EQ from Angel One is GOLDBEES on the NSE), so they're stripped and the
result probed as .NS / .BO (BSE holdings try .BO first). Resolutions — including
failures — are cached in security_symbol_map so re-runs don't re-probe.

31-March is usually a holiday or weekend, so the anchor is the last close on or
before it, within a short lookback window. One fetch per symbol covers every
boundary (Yahoo returns the whole daily series).

Known-unresolvable (report NULL rather than guess): SME-board listings Yahoo
doesn't carry, a Sovereign Gold Bond, and one row with an ISIN in the symbol
column — together ~2.55% of Indian equity value.

Dry-run by default; pass --commit to write.
  python -m workers.fy_price_backfill --commit
"""
import argparse
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
UA = {"User-Agent": "Mozilla/5.0"}

# Broker series suffixes (Angel One's -EQ, SME boards' -ST/-SM, …) are not part
# of the NSE/BSE ticker.
SERIES_SUFFIX = re.compile(r"-(EQ|ST|SM|BE|BZ|INDIA)$")

# 31-Mar is frequently a weekend/holiday — accept the last close within this many
# days before it.
ANCHOR_LOOKBACK_DAYS = 10

# 3 completed FYs need ~3 years of history; 5y gives headroom for --years 4.
RANGE = "5y"

# Below this many closes in 5 years a ticker is a delisted/illiquid husk (LT.BO
# returns exactly 1), not the live listing we want to anchor against.
MIN_POINTS = 100


def fy_boundaries(n_years: int = 3, today: date | None = None) -> list[date]:
    """The 31-Mar anchors needed for `n_years` completed FYs.

    3 completed FYs (FY23-24, FY24-25, FY25-26) need 4 boundaries: the start of
    the oldest through the end of the newest.
    """
    today = today or date.today()
    # FY starting year of the CURRENT (incomplete) FY: Apr-Dec -> this year.
    cur_fy_start = today.year if today.month >= 4 else today.year - 1
    # Newest completed FY ends on 31-Mar of cur_fy_start; oldest starts n back.
    return [date(cur_fy_start - i, 3, 31) for i in range(n_years, -1, -1)]


def fy_label(start_year: int) -> str:
    return f"{start_year}-{str(start_year + 1)[2:]}"


def _get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def candidates(symbol: str, exchange: str | None) -> list[str]:
    base = SERIES_SUFFIX.sub("", symbol)
    return [f"{base}.BO", f"{base}.NS"] if exchange == "BSE" else [f"{base}.NS", f"{base}.BO"]


def fetch_chart(yahoo_symbol: str, range_: str = RANGE) -> tuple[dict[date, float], list[tuple[date, float]]]:
    """(daily closes, split events) for a ticker. ({}, []) when Yahoo has nothing.

    Uses ?range= rather than ?period1/?period2: the epoch-range form 400s on a
    number of perfectly valid Indian tickers (LT.BO, VOGL.NS, …) for no stated
    reason, while ?range= returns the same series reliably. The symbol is
    URL-quoted because tickers like M&M.NS carry an '&' that would otherwise
    terminate the query string.

    Splits come from the same call (&events=split) — they're the gate that keeps
    split-distorted years out of the output.
    """
    url = (f"{YF_CHART.format(sym=urllib.parse.quote(yahoo_symbol))}"
           f"?range={range_}&interval=1d&events=split")
    d = _get_json(url)
    res = (d.get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return {}, []
    closes = res["indicators"]["quote"][0]["close"]
    out: dict[date, float] = {}
    for ts, cl in zip(res["timestamp"], closes):
        if cl is None:
            continue
        out[date.fromtimestamp(ts)] = float(cl)
    ev = (res.get("events") or {}).get("splits") or {}
    sp = []
    for v in ev.values():
        den = v.get("denominator") or 1
        sp.append((date.fromtimestamp(v["date"]), float(v.get("numerator") or 1) / float(den)))
    return out, sorted(sp)


def fetch_closes(yahoo_symbol: str, range_: str = RANGE) -> dict[date, float]:
    return fetch_chart(yahoo_symbol, range_)[0]


def resolve_and_fetch(symbol: str, exchange: str | None):
    """Pick the candidate ticker with the MOST history, and return its series.

    Density, not mere existence, decides. Yahoo answers for dead listings too —
    LT.BO returns a chart with a single data point across five years, so
    "first candidate that responds" silently anchored real holdings to a stale
    husk. Whichever of .NS/.BO actually carries the stock wins.
    """
    best: tuple[str | None, dict[date, float], list] = (None, {}, [])
    for cand in candidates(symbol, exchange):
        try:
            series, sp = fetch_chart(cand)
        except Exception:
            series, sp = {}, []
        if len(series) > len(best[1]):
            best = (cand, series, sp)
        time.sleep(0.05)
    # A handful of points over 5y is a delisted/illiquid husk, not a usable series.
    if len(best[1]) < MIN_POINTS:
        return None, {}, []
    return best


def anchor_close(series: dict[date, float], anchor: date) -> tuple[date, float] | None:
    """Last close on or before `anchor` (31-Mar lands on a holiday most years)."""
    for back in range(ANCHOR_LOOKBACK_DAYS + 1):
        d = anchor - timedelta(days=back)
        if d in series:
            return d, series[d]
    return None


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write to DB (default: dry-run)")
    ap.add_argument("--years", type=int, default=3, help="completed FYs to cover (default 3)")
    ap.add_argument("--refresh-map", action="store_true",
                    help="re-probe Yahoo for symbols already in security_symbol_map")
    args = ap.parse_args()

    bounds = fy_boundaries(args.years)
    logger.info(f"FY anchors: {', '.join(str(b) for b in bounds)}")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT symbol, exchange FROM equity_holding
                   WHERE (currency = 'INR' OR currency IS NULL)
                     AND symbol IS NOT NULL AND symbol <> ''
                   ORDER BY symbol""")
    holdings = cur.fetchall()

    cur.execute("SELECT symbol, resolved_symbol FROM security_symbol_map")
    known = {r["symbol"]: r["resolved_symbol"] for r in cur.fetchall()}

    resolved, unresolved, price_rows, partial = 0, [], 0, []
    splits_seen: dict[str, list] = {}

    for h in holdings:
        sym, exch = h["symbol"], h["exchange"]
        cached = known.get(sym) if (sym in known and not args.refresh_map) else None

        if cached:
            ysym = cached
            try:
                series, sp = fetch_chart(ysym)
            except Exception as e:
                logger.warning(f"  {sym} ({ysym}): fetch failed — {e}")
                continue
        else:
            ysym, series, sp = resolve_and_fetch(sym, exch)
            if args.commit:
                cur.execute("""INSERT INTO security_symbol_map (symbol, exchange, resolved_symbol, checked_at)
                               VALUES (%s,%s,%s,NOW())
                               ON CONFLICT (symbol) DO UPDATE
                                 SET resolved_symbol = EXCLUDED.resolved_symbol,
                                     exchange        = EXCLUDED.exchange,
                                     checked_at      = NOW()""",
                            (sym, exch, ysym))
                conn.commit()

        if not ysym or not series:
            unresolved.append(sym)
            continue
        resolved += 1

        if args.commit and sp:
            for sd, ratio in sp:
                cur.execute("""INSERT INTO security_split (yahoo_symbol, split_date, ratio)
                               VALUES (%s,%s,%s)
                               ON CONFLICT (yahoo_symbol, split_date) DO UPDATE
                                 SET ratio = EXCLUDED.ratio""",
                            (ysym, sd, ratio))
            splits_seen[ysym] = sp

        hits = 0
        for b in bounds:
            hit = anchor_close(series, b)
            if not hit:
                continue
            d, close = hit
            if args.commit:
                # Store under the ANCHOR date, not the actual trading day, so a
                # lookup by boundary is a plain equality match.
                cur.execute("""INSERT INTO security_price_history (yahoo_symbol, price_date, close, source)
                               VALUES (%s,%s,%s,'yahoo')
                               ON CONFLICT (yahoo_symbol, price_date) DO UPDATE
                                 SET close = EXCLUDED.close, created_at = NOW()""",
                            (ysym, b, close))
            price_rows += 1
            hits += 1
        # Fewer anchors than boundaries = listed part-way through the window; its
        # earlier FYs will correctly report NULL rather than a fabricated number.
        if hits < len(bounds):
            partial.append(f"{sym}({hits}/{len(bounds)})")
        if args.commit:
            conn.commit()
        time.sleep(0.12)   # be gentle: unofficial endpoint, no documented limit

    logger.info(f"\nresolved {resolved}/{len(holdings)} symbols; "
                f"{price_rows} boundary closes {'written' if args.commit else 'found (dry-run)'}")
    in_window = {s: [x for x in sp if x[0] >= bounds[0]] for s, sp in splits_seen.items()}
    in_window = {s: v for s, v in in_window.items() if v}
    if in_window:
        logger.info(f"splits inside the FY window ({len(in_window)} tickers) — years with an in-year "
                    f"lot predating one of these report NULL: "
                    f"{', '.join(sorted(in_window))}")
    if partial:
        logger.info(f"partial anchors ({len(partial)}) — listed mid-window, older FYs stay NULL: "
                    f"{', '.join(partial)}")
    if unresolved:
        logger.info(f"unresolved ({len(unresolved)}) — these report NULL FY growth: "
                    f"{', '.join(unresolved)}")
    if not args.commit:
        logger.info("\nDRY RUN — nothing written. Re-run with --commit.")
    conn.close()


if __name__ == "__main__":
    main()
