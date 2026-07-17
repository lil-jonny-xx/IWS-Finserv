#!/usr/bin/env python3
"""
Benchmark Worker — IWS MIS Portal

Fetches daily levels for the equity-index benchmarks (Nifty, Sensex) and stores
them in market_benchmark.  GS-bond YTM/price have no free live feed and are
entered manually via the portal, so they are not touched here.

Source: Yahoo Finance chart API (no key required), which is the only free feed that
covers all of it — indices, commodities, FX, yields and crypto alike.

FRED and Finnhub sit behind it as fallbacks, but only for the codes they quote on a
verified-identical basis (10 and 2 respectively — see FRED_FALLBACK). Neither has
free commodities or world indices, so a Yahoo outage still stops those advancing.

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

# benchmark code -> (Yahoo symbol, market, label, unit).
#
# `market` gates when we bother fetching: 'IN' = NSE/IST hours, 'US' = NYSE hours,
# 'GLOBAL' = anything that trades outside a single session (FX, commodities,
# crypto, and the non-India/US indices whose own exchange is shut while we run).
# GLOBAL rows are fetched on any tick, so their value is simply the last print
# Yahoo has — which for a closed exchange is that day's close. That's exactly what
# a "markets" strip should show.
#
# `unit` drives formatting downstream: index | price | pct | fx.
INDEX_SYMBOLS = {
    # ── India ────────────────────────────────────────────────────────────────
    "NIFTY":        ("^NSEI",      "IN",     "NSE - Nifty 50",        "index"),
    "SENSEX":       ("^BSESN",     "IN",     "BSE - Sensex",          "index"),
    "NIFTYBANK":    ("^NSEBANK",   "IN",     "Nifty Bank",            "index"),
    # ── US ───────────────────────────────────────────────────────────────────
    "DOWJONES":     ("^DJI",       "US",     "Dow Jones",             "index"),
    "NASDAQ":       ("^IXIC",      "US",     "Nasdaq Composite",      "index"),
    "SP500":        ("^GSPC",      "US",     "S&P 500",               "index"),
    "RUSSELL2000":  ("^RUT",       "US",     "Russell 2000",          "index"),
    "VIX":          ("^VIX",       "US",     "CBOE Volatility (VIX)", "index"),
    # ── Rest of world (their exchanges are shut while we run → GLOBAL) ────────
    "FTSE100":      ("^FTSE",      "GLOBAL", "FTSE 100",              "index"),
    "DAX":          ("^GDAXI",     "GLOBAL", "DAX (Germany)",         "index"),
    "CAC40":        ("^FCHI",      "GLOBAL", "CAC 40 (France)",       "index"),
    "STOXX50":      ("^STOXX50E",  "GLOBAL", "Euro Stoxx 50",         "index"),
    "NIKKEI":       ("^N225",      "GLOBAL", "Nikkei 225 (Japan)",    "index"),
    "HANGSENG":     ("^HSI",       "GLOBAL", "Hang Seng (HK)",        "index"),
    "SHANGHAI":     ("000001.SS",  "GLOBAL", "Shanghai Composite",    "index"),
    "KOSPI":        ("^KS11",      "GLOBAL", "KOSPI (Korea)",         "index"),
    "ASX200":       ("^AXJO",      "GLOBAL", "ASX 200 (Australia)",   "index"),
    "TSX":          ("^GSPTSE",    "GLOBAL", "TSX (Canada)",          "index"),
    "BOVESPA":      ("^BVSP",      "GLOBAL", "Bovespa (Brazil)",      "index"),
    # ── Commodities ──────────────────────────────────────────────────────────
    "GOLD":         ("GC=F",       "GLOBAL", "Gold (COMEX, $/oz)",    "price"),
    "SILVER":       ("SI=F",       "GLOBAL", "Silver (COMEX, $/oz)",  "price"),
    "COPPER":       ("HG=F",       "GLOBAL", "Copper ($/lb)",         "price"),
    "PLATINUM":     ("PL=F",       "GLOBAL", "Platinum ($/oz)",       "price"),
    "CRUDE_WTI":    ("CL=F",       "GLOBAL", "Crude Oil WTI ($/bbl)", "price"),
    "CRUDE_BRENT":  ("BZ=F",       "GLOBAL", "Crude Oil Brent ($/bbl)", "price"),
    "NATGAS":       ("NG=F",       "GLOBAL", "Natural Gas ($/MMBtu)", "price"),
    # ── US yields (Yahoo quotes these already as percentages) ─────────────────
    "US13W":        ("^IRX",       "GLOBAL", "US 13-Week T-Bill",     "pct_raw"),
    "US5Y":         ("^FVX",       "GLOBAL", "US 5-Year Treasury",    "pct_raw"),
    "US10Y":        ("^TNX",       "GLOBAL", "US 10-Year Treasury",   "pct_raw"),
    "US30Y":        ("^TYX",       "GLOBAL", "US 30-Year Treasury",   "pct_raw"),
    # ── FX (vs INR, plus the majors) ──────────────────────────────────────────
    "USDINR":       ("INR=X",      "GLOBAL", "USD / INR",             "fx"),
    "EURINR":       ("EURINR=X",   "GLOBAL", "EUR / INR",             "fx"),
    "GBPINR":       ("GBPINR=X",   "GLOBAL", "GBP / INR",             "fx"),
    "JPYINR":       ("JPYINR=X",   "GLOBAL", "JPY / INR",             "fx"),
    "AEDINR":       ("AEDINR=X",   "GLOBAL", "AED / INR",             "fx"),
    "SGDINR":       ("SGDINR=X",   "GLOBAL", "SGD / INR",             "fx"),
    "CHFINR":       ("CHFINR=X",   "GLOBAL", "CHF / INR",             "fx"),
    "EURUSD":       ("EURUSD=X",   "GLOBAL", "EUR / USD",             "fx"),
    "DXY":          ("DX-Y.NYB",   "GLOBAL", "US Dollar Index",       "index"),
    # ── Crypto (24/7) ────────────────────────────────────────────────────────
    "BTCINR":       ("BTC-INR",    "GLOBAL", "Bitcoin (INR)",         "price"),
    "ETHINR":       ("ETH-INR",    "GLOBAL", "Ethereum (INR)",        "price"),
    "BTCUSD":       ("BTC-USD",    "GLOBAL", "Bitcoin (USD)",         "price"),
    "ETHUSD":       ("ETH-USD",    "GLOBAL", "Ethereum (USD)",        "price"),
}
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Minutes between refreshes of the GLOBAL set (commodities / FX / crypto / world
# indices). The India + US indices still refresh every run.
GLOBAL_REFRESH_MINUTES = 15

# ── Fallbacks, for when Yahoo is unreachable ─────────────────────────────────
#
# A fallback may only be used where the other source quotes THE SAME QUANTITY ON
# THE SAME BASIS. Each mapping below was verified by comparing the two sources on
# the SAME date (not just "latest vs latest", which conflates basis with lag) over
# ~15 sessions. Anything that disagreed was left out rather than approximated:
#
#   DXY    <- DTWEXBGS      16.3% off. Fed's broad trade-weighted index is a
#                           different index from ICE's DXY, not a lagged one.
#   crude  <- DCOILWTICO    2.1% mean, 5.2% max. FRED quotes SPOT, our series is
#          <- DCOILBRENTEU  4.8% max. front-month FUTURES. Related, not equal.
#   natgas <- DHHNGSP       7.7% max. Henry Hub spot vs NG=F futures.
#   any    <- an ETF proxy  Finnhub's free tier will happily quote GLD ($365) for
#                           gold ($4k/oz). Different instrument entirely.
#
# The reason for the strictness: market_benchmark is ONE series per code, and the
# API derives week% and YTD% by walking it. A row on a foreign basis doesn't just
# render one wrong number — it injects a permanent fake move into every comparison
# that later spans it. A missing row costs far less than a wrong one.
#
# FRED (daily/weekly closes, published with a lag). Verified same-basis:
FRED_FALLBACK = {
    "SP500":    "SP500",    "DOWJONES": "DJIA",   "NASDAQ": "NASDAQCOM",
    "VIX":      "VIXCLS",   "US13W":    "DTB3",   "US5Y":   "DGS5",
    "US10Y":    "DGS10",    "US30Y":    "DGS30",
    "USDINR":   "DEXINUS",  "EURUSD":   "DEXUSEU",
}
# Finnhub (live). Free tier gates indices ("CFD indices"), forex and commodities,
# leaving US equities and crypto. Yahoo sources BTC-USD/ETH-USD from Coinbase, so
# these two agree to the cent — the only genuinely same-basis mappings it offers.
FINNHUB_FALLBACK = {
    "BTCUSD": "COINBASE:BTC-USD",
    "ETHUSD": "COINBASE:ETH-USD",
}
FRED_URL    = "https://api.stlouisfed.org/fred/series/observations"
FINNHUB_URL = "https://finnhub.io/api/v1/quote"


def fetch_index(symbol: str) -> float:
    r = requests.get(YF_URL.format(sym=symbol), timeout=10, headers=HEADERS)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    if price is None:
        raise ValueError(f"No price in Yahoo response for {symbol}")
    return float(price)


def fetch_fred(series_id: str) -> tuple[date, float]:
    """Latest FRED observation as (its own date, value).

    The date is returned rather than assumed to be today because FRED publishes
    with a lag — the FX series run about a week behind. See `_store` for why that
    date is honoured instead of being stamped as today's.
    """
    key = os.getenv("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY not set")
    r = requests.get(FRED_URL, timeout=15, params={
        "series_id": series_id, "api_key": key, "file_type": "json",
        "sort_order": "desc", "limit": 5,
    })
    r.raise_for_status()
    for o in r.json().get("observations", []):
        if o.get("value") not in (".", "", None):    # "." is FRED's no-print marker
            return date.fromisoformat(o["date"]), float(o["value"])
    raise ValueError(f"No usable observation for {series_id}")


def fetch_finnhub(symbol: str) -> tuple[date, float]:
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        raise RuntimeError("FINNHUB_API_KEY not set")
    r = requests.get(FINNHUB_URL, timeout=15, params={"symbol": symbol, "token": key})
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise RuntimeError(d["error"])
    price = d.get("c")
    # Finnhub answers an unknown symbol with 200 and an all-zero quote rather than
    # an error, so zero has to be treated as "no data" instead of a price.
    if not price:
        raise ValueError(f"No price in Finnhub response for {symbol}")
    ts = d.get("t") or 0
    return (datetime.fromtimestamp(ts, tz=NY).date() if ts else date.today()), float(price)


def _fallback(code: str):
    """(source, as_of_date, value) from the first fallback that answers, else None.

    FRED before Finnhub: FRED covers ten of our codes on a verified-identical
    basis, Finnhub only two. Neither covers commodities or the world indices, so
    those simply stop advancing if Yahoo is down — which is the honest outcome.
    """
    for src, table, fetch in (("fred",    FRED_FALLBACK,    fetch_fred),
                              ("finnhub", FINNHUB_FALLBACK, fetch_finnhub)):
        sym = table.get(code)
        if not sym:
            continue
        try:
            as_of, value = fetch(sym)
            return src, as_of, value
        except Exception as e:
            logger.warning("  fallback %s failed for %s (%s): %s", src, code, sym, e)
    return None


def main():
    force = "--force" in sys.argv or os.getenv("BENCHMARK_FORCE") == "1"
    nse_open, us_open = is_nse_open(), is_us_open()
    if not force and not (nse_open or us_open):
        logger.info("Both markets closed — skipping benchmark fetch (use --force to override).")
        return

    # GLOBAL = trades outside a single session (FX, commodities, crypto) or on an
    # exchange that's shut while we run (Nikkei, FTSE, …). For a closed exchange the
    # value is simply its last close, which is what a markets strip should show.
    #
    # This worker runs every minute. The IN/US indices are what people actually watch
    # tick, so they refresh every run; the GLOBAL set refreshes on a slower cadence —
    # otherwise 43 symbols x 60 runs/hour is ~2,600 calls/hour at an unofficial
    # endpoint with no documented limit, to move a gold price by a few paise.
    slow_tick = datetime.now(IST).minute % GLOBAL_REFRESH_MINUTES == 0
    market_open = {
        "IN":     nse_open,
        "US":     us_open,
        "GLOBAL": (nse_open or us_open) and slow_tick,
    }
    today = date.today()
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    ok = attempted = fell_back = 0
    for code, (symbol, market, label, unit) in INDEX_SYMBOLS.items():
        if not force and not market_open.get(market, False):
            continue                       # this symbol's market is shut right now
        attempted += 1
        try:
            value = fetch_index(symbol)
        except Exception as e:
            logger.warning("Yahoo failed for %s (%s): %s", code, symbol, e)
            fb = _fallback(code)
            if fb is None:
                logger.error("No usable fallback for %s — leaving the series alone", code)
                continue
            src, as_of, value = fb
            # Written at the observation's OWN date, never stamped as today's. The
            # fallbacks publish closes with a lag, so calling a week-old FX rate
            # "today's" would be inventing a print that never happened. It also
            # isn't needed: /api/v1/benchmarks reads "value at-or-before", so the
            # newest row is current automatically, whatever date it carries.
            #
            # DO NOTHING, not DO UPDATE: a fallback fills gaps and must never
            # rewrite a row Yahoo already got right. FRED's noon FX rate differs
            # from Yahoo's spot by ~0.2% — close enough to stand in for a missing
            # day, not close enough to justify overwriting a real one.
            cur.execute("""
                INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (code, as_of_date) DO NOTHING
            """, (code, label, as_of, value, unit, src))
            logger.info("%s = %s (via %s, as of %s)%s", code, value, src, as_of,
                        "" if cur.rowcount else " — already had that day, kept")
            fell_back += 1
            ok += 1
            continue
        # Label/unit come from the table above and are refreshed on every write, so
        # renaming one here propagates instead of being pinned by the first row ever
        # inserted. A manual override (source='manual', e.g. the GS bonds) is left
        # alone — this worker only ever touches codes it owns.
        cur.execute("""
            INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'yahoo', NOW())
            ON CONFLICT (code, as_of_date)
            DO UPDATE SET value = EXCLUDED.value, label = EXCLUDED.label,
                          unit = EXCLUDED.unit, source = 'yahoo', updated_at = NOW()
        """, (code, label, today, value, unit))
        logger.info("%s = %s", code, value)
        ok += 1
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Benchmark worker done (%d/%d fetched%s; NSE %s, US %s).",
                ok, attempted, f", {fell_back} via fallback" if fell_back else "",
                "open" if nse_open else "closed", "open" if us_open else "closed")


if __name__ == "__main__":
    main()
