#!/usr/bin/env python3
"""
Dividend worker — derive Indian equity dividends from market data x ledger replay.

Indian dividends never reach the broker: the company credits the shareholder's bank
account directly (DDT abolished 2020), so an 8-year Zerodha ledger holds zero dividend
rows and Kite Connect has no dividend endpoint. The only way to get them automatically
is to derive them:

    ex-date + rate/share   (Yahoo, via yfinance — 30-60 events per large-cap)
  x quantity held on that date  (replay stock_transaction, the same ledger FIFO uses)
  = dividend for that event

Runs over every security the entity has EVER traded, not just what it holds now, so
dividends on since-sold positions still count toward past years.

WHAT THIS IS NOT
----------------
It is a GROSS estimate, not cash received. Dividends above Rs 5,000/yr attract 10% TDS,
so the bank credit is smaller. Accuracy also tracks ledger completeness — years where
trade history is missing (HDR pre-2024, the NRI off-market transfers) will understate.
Both are why the monthly broker-report validation exists: `--validate` compares an
imported source='broker' row against the computed one and writes variance_pct, so
drift is measured rather than assumed away.

Quantity is the ENTITY's whole position, not per broker: a dividend is paid to the
shareholder, and older ledger rows carry no reliable broker attribution anyway.

Coverage is recorded per security in `dividend_coverage`. A security with no Yahoo
ticker (SME scrips, SGBs which pay interest not dividends, renamed tickers) is marked
unresolved rather than silently contributing zero — otherwise "no dividends" and "no
data" look identical on the page.

  python -m workers.dividend_worker                    # dry-run, all entities
  python -m workers.dividend_worker --commit
  python -m workers.dividend_worker --entity HHR --commit
  python -m workers.dividend_worker --validate         # computed vs broker variance

Cron (weekly is plenty — dividend events are infrequent and Yahoo backfills):
  30 2 * * 6 /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py \
      workers/dividend_worker.py --commit >> /var/log/mis-portal-dividends.log 2>&1
"""
import os
import sys
import argparse
import logging
import warnings
from datetime import date, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)
warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

# Exchange suffixes the brokers append that are NOT part of the Yahoo ticker.
# -EQ is Angel's NSE EQ-series tag, -BE the trade-to-trade segment, -ST/-SM the SME
# boards. See project_eq_suffix_duplicate_securities for how these bit us before.
_STRIP_SUFFIXES = ("-EQ", "-BE", "-ST", "-SM", "-BZ", "-GB", "-IV", "-N1", "-RE")

# Synthetic ledger sources: plugs and seeds, not real trades. They are INCLUDED when
# replaying quantity, unlike the dedup/prune paths that must exclude them — here they
# legitimately represent shares that were genuinely held (transferred in, or bought
# before our history starts), and those shares did earn dividends.
_LEDGER_SOURCES_EXCLUDED = ()


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def fy_label(d: date) -> str:
    """Indian financial year label for a date: Apr-Mar, so 2026-06-19 -> '2026-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def candidate_tickers(symbol: str):
    """Yahoo tickers to try for a broker symbol, best first."""
    base = (symbol or "").strip().upper()
    for suf in _STRIP_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    if not base:
        return []
    # .NS first (NSE is where these are held); .BO catches BSE-only listings.
    return [f"{base}.NS", f"{base}.BO"]


# Yahoo exchange suffix per foreign trading currency. US tickers carry no suffix;
# the others map to their home exchange (LSE .L, SGX .SI, HKEX .HK, Swiss .SW).
# UAE (AED) has no reliable Yahoo dividend feed, so it resolves to nothing.
_FOREIGN_SUFFIX = {"USD": "", "GBP": ".L", "SGD": ".SI", "HKD": ".HK", "CHF": ".SW"}


def candidate_foreign_tickers(symbol: str, currency: str):
    """Yahoo tickers to try for a foreign holding, keyed off its trading currency."""
    base = (symbol or "").strip().upper()
    if not base:
        return []
    suffix = _FOREIGN_SUFFIX.get((currency or "USD").upper())
    if suffix is None:      # a currency we have no exchange mapping for
        return []
    return [f"{base}{suffix}"] if suffix else [base]


def fetch_dividend_events(symbol: str, candidates=None):
    """Returns (events, ticker, status).

    status distinguishes two very different things that an empty dividend series
    cannot:
      'ok'            — ticker resolved and has dividend history
      'no-dividends'  — ticker resolved, company has simply never paid one
                        (ADANIGREEN, ADANIPOWER, AFFLE are all real NSE tickers)
      'unresolved'    — no such ticker on Yahoo (SME boards, renamed, delisted)

    Collapsing the middle case into 'unresolved' overstates the data gap and makes
    the coverage banner cry wolf: a growth company that pays nothing is complete
    information, not a hole. Confirming the ticker costs one extra price call, and
    only for symbols that returned no dividends.
    """
    import yfinance as yf
    cands = candidates if candidates is not None else candidate_tickers(symbol)
    # Pass 1: dividends from ANY candidate listing. This must exhaust every candidate
    # before concluding "pays none" — Yahoo sometimes carries the dividend history on
    # the BSE listing when the NSE one has prices but no corporate actions. Returning
    # early on the first ticker that merely *exists* silently dropped those events
    # (5 events / Rs 1.26L when this was first written the wrong way round).
    for tk in cands:
        try:
            s = yf.Ticker(tk).dividends
        except Exception:
            continue
        if s is None or len(s) == 0:
            continue
        out = []
        for ts, rate in s.items():
            try:
                out.append((ts.date(), float(rate)))
            except Exception:
                continue
        if out:
            return out, tk, "ok"

    # Pass 2: no dividends anywhere — but is the symbol real? A valid ticker has price
    # history; an invalid one has nothing. Only now can the two be told apart.
    for tk in cands:
        try:
            if len(yf.Ticker(tk).history(period="1mo")):
                return [], tk, "no-dividends"
        except Exception:
            continue
    return [], None, "unresolved"


# Where the broker tradebooks live. They are the only complete ISIN -> NSE-symbol
# source we have: they carry both columns for every trade ever made, including
# positions sold years ago that no longer appear in any holdings table.
TRADEBOOK_GLOBS = (
    "/var/www/TRADEBOOKS&LEDGERS/*/*.csv",
    "/var/www/New-Tradebooks/*.xlsx",
    "/var/www/AFTERNRITRADEBOOKS/*.csv",
)


def seed_symbols(conn, commit=False):
    """Persist an ISIN -> NSE-symbol map into dividend_coverage.

    `security_master.security_name` cannot be used as a ticker: it is a mix of real
    symbols (GOLDBEES, PGINVIT) and full company names (CMS INFO SYSTEMS LIM, CANARA
    BANK), and the latter resolve to nothing on Yahoo. `equity_holding` has the right
    symbol but only for what is held TODAY — it covered just 161 of 537 traded
    securities, because anything sold before the holdings snapshots began in April
    2026 has no row there.

    The tradebooks carry symbol AND isin on every line, so harvesting them resolves
    100% of traded securities. Persisting the result means the worker never depends on
    those files being present at run time.
    """
    import csv, glob
    m: dict[str, str] = {}
    for pat in TRADEBOOK_GLOBS:
        for f in glob.glob(pat):
            try:
                if f.lower().endswith(".csv"):
                    rows = list(csv.DictReader(open(f, errors="replace")))
                else:
                    import pandas as pd
                    df = pd.read_excel(f, header=14)
                    df.columns = [str(c).strip().lower() for c in df.columns]
                    rows = df.to_dict("records")
                for r in rows:
                    rr = {(k or "").strip().lower(): v for k, v in r.items()}
                    s, i = rr.get("symbol"), rr.get("isin")
                    if s and isinstance(i, str) and i.strip().startswith("IN"):
                        m.setdefault(i.strip(), str(s).strip().upper())
            except Exception:
                continue   # a malformed/unrelated file must not abort the seed

    cur = conn.cursor()
    # Holdings tables fill any ISIN the tradebooks missed (e.g. a broker-only position).
    cur.execute("""SELECT DISTINCT isin, symbol FROM equity_holding WHERE isin IS NOT NULL
                   UNION SELECT DISTINCT isin, symbol FROM equity_holding_history
                          WHERE isin IS NOT NULL""")
    for r in cur.fetchall():
        m.setdefault(r["isin"], r["symbol"])

    cur.execute("""SELECT id, isin, security_name FROM security_master
                    WHERE isin IS NOT NULL AND COALESCE(currency,'INR') = 'INR'""")
    secs = cur.fetchall()
    seeded = 0
    for s in secs:
        sym = m.get(s["isin"])
        if not sym:
            continue
        if commit:
            cur.execute("""
                INSERT INTO dividend_coverage (security_id, symbol)
                VALUES (%s, %s)
                ON CONFLICT (security_id) DO UPDATE SET symbol = EXCLUDED.symbol
            """, (s["id"], sym))
        seeded += 1
    if commit:
        conn.commit()
    cur.close()
    logger.info(f"symbol map: {len(m)} ISIN->symbol pairs harvested; "
                f"{seeded}/{len(secs)} INR securities seeded"
                + ("" if commit else "  (dry-run)"))
    return seeded


def load_traded_securities(conn, entity_ids):
    """Every (entity, security) pair with any trade history — held or since sold."""
    cur = conn.cursor()
    cur.execute("""
        SELECT st.entity_id, e.entity_name, sm.id AS security_id, sm.isin,
               sm.security_name,
               -- Ticker preference, best first: the seeded tradebook map (complete),
               -- then today's holdings symbol, then the security name as a last
               -- resort (right for some rows, a company name for others).
               COALESCE(dc.symbol, MAX(eh.symbol), sm.security_name) AS symbol,
               MIN(st.transaction_date) AS first_trade
          FROM stock_transaction st
          JOIN security_master sm ON sm.id = st.security_id
          JOIN entity e          ON e.id = st.entity_id
          LEFT JOIN dividend_coverage dc ON dc.security_id = sm.id
          LEFT JOIN equity_holding eh
                 ON eh.entity_id = st.entity_id AND eh.isin = sm.isin
                AND eh.broker NOT IN ('ibkr','vested','dbs')
         WHERE (%s::int[] IS NULL OR st.entity_id = ANY(%s::int[]))
           AND COALESCE(sm.currency, 'INR') = 'INR'
         GROUP BY st.entity_id, e.entity_name, sm.id, sm.isin, sm.security_name, dc.symbol
         ORDER BY e.entity_name, sm.security_name
    """, (entity_ids, entity_ids))
    rows = cur.fetchall()
    cur.close()
    return rows


def quantity_on(conn, entity_id: int, security_id: int, ex_date: date) -> float:
    """Net quantity held the day BEFORE the ex-date.

    To receive a dividend you must hold the share before it goes ex — a purchase ON
    the ex-date does not qualify. So the cut is strictly `< ex_date`.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(CASE WHEN transaction_type = 'BUY'
                                 THEN quantity ELSE -quantity END), 0) AS qty
          FROM stock_transaction
         WHERE entity_id = %s AND security_id = %s AND transaction_date < %s
    """, (entity_id, security_id, ex_date))
    q = float(cur.fetchone()["qty"] or 0)
    cur.close()
    return q


def fx_to_inr(conn, currency: str, on_date: date) -> float | None:
    """Currency→INR on a date: nearest rate on/before, else the earliest we have.
    Mirrors report_generator._fx_rate_on so foreign dividends convert on the same
    basis the realised-gains engine uses."""
    if not currency or currency.upper() == "INR":
        return 1.0
    cur = conn.cursor()
    try:
        cur.execute("""SELECT rate FROM fx_rate
                       WHERE from_currency=%s AND to_currency='INR' AND rate_date<=%s
                       ORDER BY rate_date DESC LIMIT 1""", (currency, on_date))
        row = cur.fetchone()
        if not row:
            cur.execute("""SELECT rate FROM fx_rate
                           WHERE from_currency=%s AND to_currency='INR'
                           ORDER BY rate_date ASC LIMIT 1""", (currency,))
            row = cur.fetchone()
    finally:
        cur.close()
    return float(row["rate"]) if row else None


def load_foreign_traded_securities(conn, entity_ids):
    """Every (entity, security) pair traded in a foreign currency — Harsh's Vested/US
    book lives in stock_transaction with currency<>'INR' and a security_master row
    whose security_name is the US ticker (AAPL, AMZN…). Same shape as
    load_traded_securities so the derivation loop is identical."""
    cur = conn.cursor()
    cur.execute("""
        SELECT st.entity_id, e.entity_name, sm.id AS security_id,
               sm.security_name AS ticker,
               COALESCE(st.currency, 'USD') AS currency,
               MIN(st.transaction_date) AS first_trade
          FROM stock_transaction st
          JOIN security_master sm ON sm.id = st.security_id
          JOIN entity e           ON e.id = st.entity_id
         WHERE COALESCE(st.currency, 'INR') <> 'INR'
           AND (%s::int[] IS NULL OR st.entity_id = ANY(%s::int[]))
         GROUP BY st.entity_id, e.entity_name, sm.id, sm.security_name, st.currency
         ORDER BY e.entity_name, sm.security_name
    """, (entity_ids, entity_ids))
    rows = cur.fetchall()
    cur.close()
    return rows


def _yahoo_us_ticker(ticker: str) -> str:
    """Yahoo writes US class shares with a dash, not a dot (BRK.B → BRK-B)."""
    return (ticker or "").strip().upper().replace(".", "-")


def run_foreign(conn, cur, entity_ids, commit, stats):
    """Derive foreign (currency<>INR) dividends into the same `dividend` table.
    amount is stored in INR (converted at the ex-date rate) so it sums with the
    domestic rows; rate_per_share and currency stay native so the API can split the
    domestic vs foreign views. Runs inside the caller's transaction."""
    pairs = load_foreign_traded_securities(conn, entity_ids)
    logger.info(f"{len(pairs)} foreign (entity, security) pair(s) with trade history")
    feed_cache: dict[str, tuple] = {}
    for p in pairs:
        ccy = (p["currency"] or "USD").upper()
        key = f"{ccy}:{p['ticker']}"
        if key not in feed_cache:
            cands = candidate_foreign_tickers(_yahoo_us_ticker(p["ticker"]), ccy)
            feed_cache[key] = fetch_dividend_events(p["ticker"], candidates=cands)
        events, _ticker, status = feed_cache[key]
        if status == "unresolved" or not events:
            if status == "unresolved":
                stats["unresolved"] += 1
            continue
        for ex_date, rate in events:
            if p["first_trade"] and ex_date <= p["first_trade"]:
                continue
            qty = quantity_on(conn, p["entity_id"], p["security_id"], ex_date)
            if qty <= 0:
                stats["zero_qty"] += 1
                continue
            fx = fx_to_inr(conn, ccy, ex_date)
            if fx is None:
                continue  # no rate — cannot express in INR
            amount_inr = round(qty * rate * fx, 2)
            stats["events"] += 1
            stats["rows"] += 1
            stats["amount"] += amount_inr
            if commit:
                upsert_dividend(cur, p["entity_id"], p["security_id"], ex_date,
                                qty, rate, amount_inr, "yfinance", currency=ccy)
            else:
                logger.info(f"  {p['entity_name']:<12} {p['ticker']:<10} {ex_date} "
                            f"{qty:>10,.2f} x {rate:>8.4f} {ccy} @ {fx:.2f} = Rs {amount_inr:>12,.2f}")


def upsert_dividend(cur, entity_id, security_id, ex_date, qty, rate, amount, feed,
                    currency="INR"):
    """Write one computed dividend. `amount` is ALWAYS INR (so figures sum across
    domestic and foreign); `rate_per_share` is in `currency` — INR for Indian scrips,
    the native currency (e.g. USD) for foreign ones. `currency` tags which it is, and
    is what the API scopes on to split the domestic vs foreign views."""
    cur.execute("""
        INSERT INTO dividend (entity_id, security_id, ex_date, quantity, rate_per_share,
                              amount, currency, fy, source, feed, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'computed',%s,NOW())
        ON CONFLICT (entity_id, security_id, ex_date, source) DO UPDATE
           SET quantity = EXCLUDED.quantity,
               rate_per_share = EXCLUDED.rate_per_share,
               amount = EXCLUDED.amount,
               currency = EXCLUDED.currency,
               fy = EXCLUDED.fy,
               feed = EXCLUDED.feed,
               updated_at = NOW()
    """, (entity_id, security_id, ex_date, qty, rate, amount, currency,
          fy_label(ex_date), feed))


def record_coverage(cur, security_id, symbol, ticker, events, note=None):
    cur.execute("""
        INSERT INTO dividend_coverage (security_id, symbol, yahoo_ticker, resolved,
                                       events_found, last_checked, note)
        VALUES (%s,%s,%s,%s,%s,NOW(),%s)
        ON CONFLICT (security_id) DO UPDATE
           SET symbol = EXCLUDED.symbol, yahoo_ticker = EXCLUDED.yahoo_ticker,
               resolved = EXCLUDED.resolved, events_found = EXCLUDED.events_found,
               last_checked = NOW(), note = EXCLUDED.note
    """, (security_id, symbol, ticker, bool(ticker), events, note))


def run(entities=None, commit=False, limit=None, seed=True):
    conn = get_conn()
    if seed:
        # Cheap and idempotent; keeps the ticker map current as new securities trade.
        seed_symbols(conn, commit=commit)
    cur = conn.cursor()

    entity_ids = None
    if entities:
        cur.execute("SELECT id FROM entity WHERE entity_name = ANY(%s)", (entities,))
        entity_ids = [r["id"] for r in cur.fetchall()]
        if not entity_ids:
            logger.error(f"no such entity: {entities}")
            return

    pairs = load_traded_securities(conn, entity_ids)
    logger.info(f"{len(pairs)} (entity, security) pairs with trade history")

    # Computed rows are fully derivable, so the current run is the whole truth for
    # them: clear the scope first, then rebuild. Upserting alone leaks rows that a
    # later run no longer produces (a corrected feed, a re-dated trade), and those
    # stale rows silently inflate every total. Safe because the delete and the
    # inserts share one transaction, committed only at the end.
    if commit:
        if entity_ids:
            cur.execute("DELETE FROM dividend WHERE source='computed' AND entity_id = ANY(%s)",
                        (entity_ids,))
        else:
            cur.execute("DELETE FROM dividend WHERE source='computed'")
        logger.info(f"cleared {cur.rowcount} existing computed row(s) for rebuild")

    # One Yahoo lookup per SYMBOL, reused across entities that hold it.
    feed_cache: dict[str, tuple] = {}
    stats = {"events": 0, "rows": 0, "unresolved": 0, "zero_qty": 0, "amount": 0.0}
    seen_securities = set()

    for i, p in enumerate(pairs):
        if limit and i >= limit:
            break
        sym = p["symbol"]
        if sym not in feed_cache:
            feed_cache[sym] = fetch_dividend_events(sym)
        events, ticker, status = feed_cache[sym]

        if p["security_id"] not in seen_securities:
            seen_securities.add(p["security_id"])
            if commit:
                # 'resolved' means we could look the security up — including when the
                # honest answer is "it pays no dividend". Only a genuine lookup failure
                # counts against coverage.
                record_coverage(cur, p["security_id"], sym, ticker, len(events),
                                {"ok": None,
                                 "no-dividends": "resolved; company pays no dividend",
                                 "unresolved": "no Yahoo ticker (.NS/.BO) resolved"}[status])
        if status == "unresolved":
            stats["unresolved"] += 1
            continue
        if not events:
            continue

        for ex_date, rate in events:
            # Nothing before the entity's first trade can have been held.
            if p["first_trade"] and ex_date <= p["first_trade"]:
                continue
            qty = quantity_on(conn, p["entity_id"], p["security_id"], ex_date)
            if qty <= 0:
                stats["zero_qty"] += 1
                continue
            amount = round(qty * rate, 2)
            stats["events"] += 1
            stats["rows"] += 1
            stats["amount"] += amount
            if commit:
                upsert_dividend(cur, p["entity_id"], p["security_id"], ex_date,
                                qty, rate, amount, "yfinance")
            else:
                logger.info(f"  {p['entity_name']:<12} {sym:<16} {ex_date} "
                            f"{qty:>10,.0f} x {rate:>8.4f} = Rs {amount:>12,.2f}")

    # Foreign (Vested/US) dividends into the same table — the start-of-run DELETE
    # already cleared them, so this rebuilds them in the same transaction.
    run_foreign(conn, cur, entity_ids, commit, stats)

    if commit:
        conn.commit()
    cur.close()
    conn.close()

    logger.info(f"\n{'COMMITTED' if commit else 'DRY-RUN'}: "
                f"{stats['rows']} dividend row(s), total Rs {stats['amount']:,.0f}; "
                f"{stats['unresolved']} pair(s) with no feed ticker, "
                f"{stats['zero_qty']} event(s) skipped (not held on ex-date)")
    if not commit:
        logger.info("re-run with --commit to write")


def validate():
    """Compare computed vs imported broker rows and record the variance.

    This is the monthly check: a broker dividend report is the authority, so where one
    exists the computed figure is scored against it. Persisting variance_pct means the
    next run can show WHICH securities the derivation gets wrong, rather than leaving
    "is the estimate any good?" as a matter of opinion.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id AS computed_id, e.entity_name, sm.security_name, c.ex_date,
               c.amount AS computed_amt, b.amount AS broker_amt
          FROM dividend c
          JOIN dividend b ON b.entity_id = c.entity_id
                         AND b.security_id = c.security_id
                         AND b.ex_date = c.ex_date
                         AND b.source = 'broker'
          JOIN entity e          ON e.id = c.entity_id
          JOIN security_master sm ON sm.id = c.security_id
         WHERE c.source = 'computed'
         ORDER BY ABS(c.amount - b.amount) DESC
    """)
    rows = cur.fetchall()
    if not rows:
        logger.info("no broker dividend rows to validate against — import one first "
                    "(source='broker'), then re-run --validate")
        cur.close(); conn.close()
        return

    worst = 0.0
    for r in rows:
        comp, brok = float(r["computed_amt"]), float(r["broker_amt"])
        var = ((comp - brok) / brok * 100.0) if brok else None
        cur.execute("UPDATE dividend SET variance_pct=%s, updated_at=NOW() WHERE id=%s",
                    (var, r["computed_id"]))
        if var is not None and abs(var) > 1.0:
            worst = max(worst, abs(var))
            logger.info(f"  {r['entity_name']:<12} {r['security_name']:<20} {r['ex_date']} "
                        f"computed Rs {comp:>12,.2f} vs broker Rs {brok:>12,.2f}  ({var:+.1f}%)")
    conn.commit()
    cur.close(); conn.close()
    logger.info(f"validated {len(rows)} event(s); worst variance {worst:.1f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="write (default dry-run)")
    ap.add_argument("--entity", help="comma-separated entity names")
    ap.add_argument("--limit", type=int, help="only process N pairs (smoke testing)")
    ap.add_argument("--validate", action="store_true",
                    help="score computed rows against imported broker rows")
    ap.add_argument("--seed-symbols-only", action="store_true",
                    help="rebuild the ISIN->ticker map and stop")
    args = ap.parse_args()

    if args.validate:
        validate()
        return
    if args.seed_symbols_only:
        seed_symbols(get_conn(), commit=args.commit)
        return
    run(entities=[e.strip() for e in args.entity.split(",")] if args.entity else None,
        commit=args.commit, limit=args.limit)


if __name__ == "__main__":
    main()
