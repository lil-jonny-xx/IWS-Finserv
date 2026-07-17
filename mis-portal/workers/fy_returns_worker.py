#!/usr/bin/env python3
"""
Completed financial-year growth per holding — Equity + Mutual Funds.

Writes equity_holding.fy_returns / holding.fy_returns as
    {"2025-26": {"pnl": 12345.67, "pct": 8.9}, "2024-25": {...}}
The CURRENT financial year is deliberately NOT in here — it stays as the
existing pnl_ytd / returns_ytd_pct, which this worker does not touch.

Shape of the calculation (same as the YTD one in equity_txn_metrics_worker, just
between two 31-Mar anchors instead of 1-Apr→today):

    per held lot at FY end:  ref = buy price if bought during the FY
                                   else the FY-start price
    pnl  = Σ qty × (fy_end_price − ref)
    base = Σ qty × ref
    pct  = pnl / base × 100

Measured on the position held AT FY END, reconstructed by replaying the ledger
to that date. Realised gains on trims within the year are excluded — same policy
as YTD; they live in Realised Gains.

WHEN A YEAR IS NULL (never 0 — 0 reads as "flat", which is a different claim):
  * The broker's ledger starts after the FY began. We cannot know the opening
    position, so the whole year is unknowable for that broker — this is why
    Angel One (ledger from 2025-08-21) and Dhan (from 2026-01-19) report "—"
    for FY2025-26 and earlier. Guard is per (entity, broker), not per stock: a
    stock genuinely first bought mid-FY on a broker whose ledger DOES reach back
    is computable, and gets its buy price as the reference.
  * No price anchor at a boundary (recent IPO, or a symbol Yahoo doesn't carry —
    SME boards, the Sovereign Gold Bond).
  * The position was flat at FY end.

Equity prices come from security_price_history (see fy_price_backfill).
MF NAVs come from nav_history, which already runs back to 2006 — no backfill.

Dry-run by default; pass --commit to write.
  python -m workers.fy_returns_worker --commit
"""
import argparse
import json
import logging
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

from workers.equity_txn_metrics_worker import fifo_lots, f  # noqa: E402  (reuse, don't re-implement)
from workers.fy_price_backfill import fy_boundaries, fy_label  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

N_YEARS = 3          # completed FYs to publish
NAV_LOOKBACK = 10    # 31-Mar is often a holiday; accept the last NAV before it


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------
def broker_ledger_start(cur, entity_id, broker) -> date | None:
    """Earliest transaction we hold for this (entity, broker).

    The honesty gate: before this date we have no idea what was held, so any FY
    beginning earlier cannot be computed for this broker at all.
    """
    cur.execute("""SELECT MIN(st.transaction_date) d FROM stock_transaction st
                   WHERE st.entity_id = %s
                     AND (st.broker = %s OR (st.broker IS NULL AND st.source = %s))""",
                (entity_id, broker, broker))
    r = cur.fetchone()
    return r["d"] if r else None


def equity_txns(cur, entity_id, broker, isin):
    """Trade history for one (entity, broker, ISIN), authoritative tier preferred.

    Mirrors equity_txn_metrics_worker.compute's tier precedence: real fills win,
    snapshot rows only fill in after the last authoritative trade, so the two
    can't double-count.
    """
    if not isin:
        return []
    cur.execute("""SELECT st.transaction_date d, st.transaction_type side,
                          st.quantity q, st.price p, st.source src
                   FROM stock_transaction st JOIN security_master sm ON sm.id = st.security_id
                   WHERE st.entity_id = %s AND sm.isin = %s
                     AND ( st.source = %s
                        OR (st.source IN ('manual','reconstructed') AND st.broker = %s)
                        OR (st.source IN ('snapshot','snapshot_open') AND st.broker = %s) )
                   ORDER BY st.transaction_date, st.id""",
                (entity_id, isin, broker, broker, broker))
    rows = cur.fetchall()
    auth = [r for r in rows if r["src"] not in ("snapshot", "snapshot_open")]
    if any(r["side"] == "BUY" for r in auth):
        max_auth = max(r["d"] for r in auth)
        return [r for r in rows
                if r["src"] not in ("snapshot", "snapshot_open")
                or (r["src"] == "snapshot" and r["d"] > max_auth)]
    return rows


def price_at(cur, yahoo_symbol, anchor: date):
    if not yahoo_symbol:
        return None
    cur.execute("""SELECT close FROM security_price_history
                   WHERE yahoo_symbol = %s AND price_date = %s""", (yahoo_symbol, anchor))
    r = cur.fetchone()
    return f(r["close"]) if r else None


def splits_for(cur, ysym) -> list[tuple[date, float]]:
    if not ysym:
        return []
    cur.execute("SELECT split_date, ratio FROM security_split WHERE yahoo_symbol=%s ORDER BY 1", (ysym,))
    return [(r["split_date"], float(r["ratio"])) for r in cur.fetchall()]




def fy_return_equity(cur, h, ysym, start_anchor: date, end_anchor: date, ledger_start,
                     split_dates: list[date] | None = None):
    """One completed FY for one equity holding. None when it can't be known."""
    # The broker's history has to cover the start of the year, or the opening
    # position is a guess.
    if ledger_start is None or ledger_start > start_anchor:
        return None

    p_end = price_at(cur, ysym, end_anchor)
    if p_end is None:
        return None

    all_txns = equity_txns(cur, h["entity_id"], h["broker"], h["isin"])
    if not all_txns:
        return None

    # Reconciliation gate, as in the YTD worker: if replaying the ledger doesn't
    # land on the quantity actually held, shares moved by routes no tradebook
    # records (off-market transfers, bonuses, inter-account moves), and the lots
    # are wrong. 2% tolerance, with a 1-share floor only for larger positions.
    qty = f(h["quantity"]) or 0.0
    net = sum(((f(t["q"]) or 0.0) if t["side"] == "BUY" else -(f(t["q"]) or 0.0)) for t in all_txns)
    tol = max(1.0, 0.02 * qty) if qty >= 50 else 0.02 * qty + 1e-6
    if abs(net - qty) > tol:
        return None

    txns = [t for t in all_txns if t["d"] <= end_anchor]
    if not txns:
        return None

    lots = fifo_lots(txns)
    if not lots:
        return None   # flat at FY end

    # Split gate. Yahoo restates every historical close for later splits; our ledger
    # keeps fills at their raw executed price. The two bases only ever meet on an
    # IN-YEAR lot — a pre-year lot is measured anchor-to-anchor, both adjusted, and
    # stays exact. So a year is unsafe precisely when an in-year lot predates a
    # split; that mix read Canara Bank as -79.7% for a year it merely split 1:5.
    #
    # Restating the fills onto Yahoo's basis (price/factor, qty*factor) was tried and
    # REVERTED: it lowers coverage rather than raising it, because reconstruct_history
    # has already balanced these ledgers with a synthetic plug sized against the RAW
    # net — NESTLEIND's plug is 843 of its 1000 shares, i.e. the plug IS the ×20
    # split, papered over. Adjusting the real lots around that plug double-counts and
    # the reconciliation gate then rejects the holding anyway. Recovering these years
    # needs corporate actions recorded as ledger events, not a factor applied on read.
    in_year = [d for d, _, _ in lots if d > start_anchor]
    if in_year and any(sd > min(in_year) for sd, _ in (split_dates or [])):
        return None

    p_start = price_at(cur, ysym, start_anchor)
    pnl = base = 0.0
    for d, q, p in lots:
        if d > start_anchor:
            ref = p                 # bought during the year: measure from the fill
        elif p_start is not None:
            ref = p_start           # held into the year: measure from the FY-start close
        else:
            return None             # held before the year but no anchor to measure from
        if ref is None or ref <= 0:
            return None
        pnl += q * (p_end - ref)
        base += q * ref
    if base <= 0:
        return None
    # `base` is the capital the return is measured on. It ships so the table
    # footer can value-weight the FY % across rows (Σpnl / Σbase) — averaging
    # percentages would weight a ₹5k holding like a ₹5cr one.
    return {"pnl": round(pnl, 2), "pct": round(pnl / base * 100, 4), "base": round(base, 2)}


# ---------------------------------------------------------------------------
# Mutual funds
# ---------------------------------------------------------------------------
def nav_at(cur, security_id, anchor: date):
    """Last NAV on or before the anchor (31-Mar is routinely a holiday)."""
    cur.execute("""SELECT nav FROM nav_history
                   WHERE security_id = %s AND nav_date <= %s AND nav_date >= %s
                   ORDER BY nav_date DESC LIMIT 1""",
                (security_id, anchor, anchor - timedelta(days=NAV_LOOKBACK)))
    r = cur.fetchone()
    return f(r["nav"]) if r else None


def mf_txns(cur, entity_id, security_id, folio, upto: date):
    """Unit-moving CAS rows for one (entity, security, folio), up to a date.

    Two things the schema does that are easy to get wrong:
      * `units` is SIGNED — REDEMPTION / SWITCH_OUT are already negative, so
        direction comes from the sign, never from the type label. Negating them
        by label turns a redemption into a purchase.
      * Tax rows (STAMP_DUTY_TAX ×351, TDS_TAX, STT_TAX) carry NULL units and no
        NAV. They are cash charges, not unit movements, and must not become lots.
    Folio matters: holdings are per (entity, security, folio), and the same fund
    can be held in two folios by one entity.
    """
    cur.execute("""SELECT transaction_date d, units u, nav p
                   FROM mf_transaction
                   WHERE entity_id = %s AND security_id = %s AND folio_number = %s
                     AND transaction_date <= %s
                     AND units IS NOT NULL AND nav IS NOT NULL
                   ORDER BY transaction_date, id""",
                (entity_id, security_id, folio, upto))
    out = []
    for r in cur.fetchall():
        u = f(r["u"]) or 0.0
        if abs(u) < 1e-9:
            continue
        out.append({"d": r["d"], "side": "BUY" if u > 0 else "SELL",
                    "q": abs(u), "p": r["p"]})
    return out


def fy_return_mf(cur, hold, start_anchor: date, end_anchor: date):
    sid, folio = hold["security_id"], hold["folio_number"]
    nav_end = nav_at(cur, sid, end_anchor)
    if nav_end is None:
        return None

    txns = mf_txns(cur, hold["entity_id"], sid, folio, end_anchor)
    if not txns:
        return None

    # Trust the reconstruction only if replaying every unit-moving row lands on
    # the units actually held today. Two folios currently fail this and must stay
    # NULL rather than publish a wrong year: DHR's ICICI Liquid folio sums
    # NEGATIVE (a known corrupt ledger), and one folio is off by a lone REVERSAL.
    held_now = f(hold["quantity"]) or 0.0
    cur.execute("""SELECT COALESCE(SUM(units),0) bal FROM mf_transaction
                   WHERE entity_id=%s AND security_id=%s AND folio_number=%s
                     AND units IS NOT NULL""",
                (hold["entity_id"], sid, folio))
    ledger_now = f(cur.fetchone()["bal"]) or 0.0
    if ledger_now < 0 or abs(ledger_now - held_now) > max(0.01, 0.02 * abs(held_now)):
        return None

    lots = fifo_lots(txns)
    if not lots:
        return None   # flat at FY end

    nav_start = nav_at(cur, sid, start_anchor)
    pnl = base = 0.0
    for d, q, p in lots:
        ref = f(p) if d > start_anchor else nav_start
        if ref is None or ref <= 0:
            return None
        pnl += q * (nav_end - ref)
        base += q * ref
    if base <= 0:
        return None
    # `base` is the capital the return is measured on. It ships so the table
    # footer can value-weight the FY % across rows (Σpnl / Σbase) — averaging
    # percentages would weight a ₹5k holding like a ₹5cr one.
    return {"pnl": round(pnl, 2), "pct": round(pnl / base * 100, 4), "base": round(base, 2)}


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--years", type=int, default=N_YEARS)
    args = ap.parse_args()

    bounds = fy_boundaries(args.years)
    fys = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    logger.info(f"FYs: {', '.join(fy_label(s.year) for s, _ in fys)}\n")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT symbol, resolved_symbol FROM security_symbol_map")
    ymap = {r["symbol"]: r["resolved_symbol"] for r in cur.fetchall()}

    # ---- equity
    cur.execute("""SELECT id, entity_id, broker, symbol, isin, quantity
                   FROM equity_holding
                   WHERE (currency = 'INR' OR currency IS NULL)
                   ORDER BY entity_id, broker, symbol""")
    eq = cur.fetchall()
    ledger_cache: dict[tuple, date | None] = {}
    split_cache: dict[str | None, list] = {}
    eq_filled = eq_rows = 0
    for h in eq:
        key = (h["entity_id"], h["broker"])
        if key not in ledger_cache:
            ledger_cache[key] = broker_ledger_start(cur, *key)
        ysym = ymap.get(h["symbol"])
        if ysym not in split_cache:
            split_cache[ysym] = splits_for(cur, ysym)
        out = {}
        for s, e in fys:
            r = fy_return_equity(cur, h, ysym, s, e, ledger_cache[key], split_cache[ysym])
            if r:
                out[fy_label(s.year)] = r
        eq_rows += 1
        if out:
            eq_filled += 1
        if args.commit:
            cur.execute("UPDATE equity_holding SET fy_returns=%s WHERE id=%s",
                        (json.dumps(out) if out else None, h["id"]))
    if args.commit:
        conn.commit()
    logger.info(f"Equity: {eq_filled}/{eq_rows} holdings got at least one FY figure")

    # ---- mutual funds
    cur.execute("""SELECT id, entity_id, security_id, folio_number, quantity FROM holding
                   ORDER BY entity_id, security_id, folio_number""")
    mf = cur.fetchall()
    mf_filled = mf_rows = 0
    for h in mf:
        out = {}
        for s, e in fys:
            r = fy_return_mf(cur, h, s, e)
            if r:
                out[fy_label(s.year)] = r
        mf_rows += 1
        if out:
            mf_filled += 1
        if args.commit:
            cur.execute("UPDATE holding SET fy_returns=%s WHERE id=%s",
                        (json.dumps(out) if out else None, h["id"]))
    if args.commit:
        conn.commit()
    logger.info(f"MF    : {mf_filled}/{mf_rows} holdings got at least one FY figure")

    # ---- coverage, per FY, so a silently-empty year is visible
    logger.info("\nCoverage by FY (holdings with a figure):")
    for s, _ in fys:
        lbl = fy_label(s.year)
        cur.execute("SELECT COUNT(*) n FROM equity_holding WHERE fy_returns ? %s", (lbl,))
        en = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM holding WHERE fy_returns ? %s", (lbl,))
        mn = cur.fetchone()["n"]
        logger.info(f"   FY{lbl}: equity={en:>3}/{eq_rows}  mf={mn:>3}/{mf_rows}")

    if not args.commit:
        logger.info("\nDRY RUN — nothing written. Re-run with --commit.")
    conn.close()


if __name__ == "__main__":
    main()
