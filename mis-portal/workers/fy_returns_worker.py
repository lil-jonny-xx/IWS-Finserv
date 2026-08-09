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
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# Project root on sys.path so the `workers.` imports below resolve when this file is
# run DIRECTLY — cron_wrapper spawns `python workers/fy_returns_worker.py` as a fresh
# subprocess with no package context, so without this the scheduled run dies on
# ModuleNotFoundError before doing anything.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv("/var/www/mis-portal/.env", override=True)

from workers.equity_txn_metrics_worker import fifo_lots, f  # noqa: E402  (reuse, don't re-implement)
from workers.fy_price_backfill import fy_boundaries, fy_label  # noqa: E402
from workers.corporate_actions import (  # noqa: E402
    load_actions_by_isin, cumulative_ratio_after,
)

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
                     split_dates: list[date] | None = None, actions=None):
    """One completed FY for one equity holding. None when it can't be known.

    `actions` is this security's recorded [(ex_date, ratio), ...] corporate actions.
    When present they are applied so the ledger lots share the price series' fully
    split-adjusted basis, which is what lets a split-affected year be computed at all
    (see the split gate below)."""
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
    # Net on the held (fully split-adjusted) basis: a pre-split fill counts for
    # ratio× shares today, so a depository-credited bonus/split doesn't read as a gap.
    net = sum((((f(t["q"]) or 0.0) if t["side"] == "BUY" else -(f(t["q"]) or 0.0))
               * cumulative_ratio_after(actions, t["d"])) for t in all_txns)
    tol = max(1.0, 0.02 * qty) if qty >= 50 else 0.02 * qty + 1e-6
    if abs(net - qty) > tol:
        return None

    txns = [t for t in all_txns if t["d"] <= end_anchor]
    if not txns:
        return None

    # fifo_lots applies any split whose ex-date falls within the replayed window
    # (lazily, at the first trade on/after it). Splits AFTER the FY end are not seen
    # by the replay, yet the price series is ALREADY back-adjusted for them, so restate
    # the surviving lots onto that same fully-adjusted basis (qty ×ratio, price ÷ratio).
    lots = fifo_lots(txns, actions)
    if not lots:
        return None   # flat at FY end
    r_future = cumulative_ratio_after(actions, end_anchor)
    if r_future != 1.0:
        lots = [(d, q * r_future, p / r_future) for d, q, p in lots]

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
    # A recorded corporate action has already been applied above (lots are on the
    # adjusted basis), so it is SAFE — the gate only needs to fire for a split Yahoo
    # shows but which is NOT recorded, where the raw in-year lot and the adjusted price
    # still disagree. Match recorded ex-dates within a few days of the Yahoo date.
    recorded = [ex for ex, _ in (actions or [])]
    unrecorded = [sd for sd, _ in (split_dates or [])
                  if not any(abs((sd - ex).days) <= 5 for ex in recorded)]
    in_year = [d for d, _, _ in lots if d > start_anchor]
    if in_year and any(sd > min(in_year) for sd in unrecorded):
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
# Foreign equity
# ---------------------------------------------------------------------------
def foreign_txns(cur, h):
    """Trade history for one foreign holding, oldest first, in NATIVE currency.

    Two feeds, two tables: Vested is scraped into stock_transaction (source='vested',
    matched on security_name — the scrape carries no ISIN), IBKR's Flex statement lands
    in equity_trade_ledger. DBS is a holdings-only CSV with no trade history at all, so
    it has nothing to replay and correctly reports NULL for every year.
    """
    if h["broker"] == "vested":
        cur.execute("""SELECT st.transaction_date d, st.transaction_type side,
                              st.quantity q, st.price p, COALESCE(st.currency,'USD') currency
                       FROM stock_transaction st JOIN security_master sm ON sm.id = st.security_id
                       WHERE st.entity_id = %s AND st.source = 'vested' AND sm.security_name = %s
                       ORDER BY st.transaction_date, st.id""", (h["entity_id"], h["symbol"]))
        return cur.fetchall()
    if h["broker"] == "ibkr":
        cur.execute("""SELECT trade_date d, side, quantity q, price_native p, currency
                       FROM equity_trade_ledger
                       WHERE entity_id = %s AND broker = 'ibkr' AND symbol = %s
                         AND price_native IS NOT NULL
                       ORDER BY trade_date, id""", (h["entity_id"], h["symbol"]))
        return cur.fetchall()
    return []


def foreign_ledger_start(cur, entity_id, broker) -> date | None:
    """Earliest trade we hold for this (entity, broker) — the honesty gate.

    Deliberately per (entity, broker) and NOT per symbol, mirroring the Indian path:
    before this date we cannot know what was held, so any FY starting earlier is
    unknowable for the whole broker. But a stock genuinely first BOUGHT mid-year on a
    broker whose history does reach back is perfectly computable — it just takes its
    fill price as the reference. Gating per symbol would blank every such holding.
    """
    if broker == "vested":
        cur.execute("""SELECT MIN(transaction_date) d FROM stock_transaction
                       WHERE entity_id = %s AND source = 'vested'""", (entity_id,))
    elif broker == "ibkr":
        cur.execute("""SELECT MIN(trade_date) d FROM equity_trade_ledger
                       WHERE entity_id = %s AND broker = 'ibkr'""", (entity_id,))
    else:
        return None
    r = cur.fetchone()
    return r["d"] if r else None


def fx_at(cur, ccy: str, anchor: date):
    """currency→INR on or just before an anchor. 31-Mar is often a non-business day
    and frankfurter only publishes business days, so look back a short window."""
    if not ccy or ccy.upper() == "INR":
        return 1.0
    cur.execute("""SELECT rate FROM fx_rate
                   WHERE from_currency = %s AND to_currency = 'INR'
                     AND rate_date <= %s AND rate_date >= %s
                   ORDER BY rate_date DESC LIMIT 1""",
                (ccy.upper(), anchor, anchor - timedelta(days=10)))
    r = cur.fetchone()
    return f(r["rate"]) if r else None


def fy_return_foreign(cur, h, ysym, start_anchor: date, end_anchor: date, ledger_start):
    """One completed FY for one foreign holding, measured in INR. None when unknowable.

    Unlike the Indian path this is computed on the INR value at each end, not the native
    one: the family reports in rupees, so a year in which the stock went nowhere but the
    dollar moved 8% DID earn 8%. Pricing both ends natively and converting at a single
    rate would silently discard that — and would also make the table footer's Σpnl/Σbase
    incoherent, since pnl would be INR and base native.

    No split gate here (unlike fy_return_equity): the foreign ledgers are broker-reported
    executions rather than reconstructed history, so there is no synthetic plug for a
    split to interact with. Yahoo's back-adjusted closes and the raw fills still disagree
    across a split, which the reconciliation gate below catches as a quantity mismatch."""
    ccy = (h.get("currency") or "USD").upper()

    # The broker's history has to cover the start of the year, or the opening
    # position is a guess. Per broker, not per stock — see foreign_ledger_start.
    if ledger_start is None or ledger_start > start_anchor:
        return None

    txns = foreign_txns(cur, h)
    if not txns:
        return None
    # Every leg must be in the holding's own currency, or the lots and the anchor
    # prices are not the same unit — SDR's DFND is dual-listed and has both GBP and
    # USD fills. Mixing them invents a return.
    if any((t.get("currency") or ccy).upper() != ccy for t in txns):
        return None

    p_end = price_at(cur, ysym, end_anchor)
    if p_end is None:
        return None
    fx_end = fx_at(cur, ccy, end_anchor)
    if fx_end is None:
        return None

    # Reconciliation gate — same rule as the YTD worker. For IBKR this is load-bearing:
    # shares moved between an entity's own accounts have a sell leg with no matching buy,
    # so a lot pool built from them would be wrong.
    qty = f(h["quantity"]) or 0.0
    net = sum(((f(t["q"]) or 0.0) if t["side"] == "BUY" else -(f(t["q"]) or 0.0)) for t in txns)
    tol = max(1.0, 0.02 * qty) if qty >= 50 else 0.02 * qty + 1e-6
    if abs(net - qty) > tol:
        return None

    lots = fifo_lots([t for t in txns if t["d"] <= end_anchor])
    if not lots:
        return None   # flat at FY end

    p_start  = price_at(cur, ysym, start_anchor)
    fx_start = fx_at(cur, ccy, start_anchor)
    pnl_inr = base_inr = 0.0
    for d, q, p in lots:
        if d > start_anchor:
            # Bought during the year: measure from the fill, at the fill date's own rate,
            # so the currency move since purchase is part of the year's return.
            ref, ref_fx = p, fx_at(cur, ccy, d)
        else:
            ref, ref_fx = p_start, fx_start
        if ref is None or ref <= 0 or ref_fx is None:
            return None
        ref_inr = q * ref * ref_fx
        pnl_inr  += q * p_end * fx_end - ref_inr
        base_inr += ref_inr
    if base_inr <= 0:
        return None
    return {"pnl": round(pnl_inr, 2), "pct": round(pnl_inr / base_inr * 100, 4),
            "base": round(base_inr, 2)}


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

    # Recorded splits/bonuses, applied so a split-affected year is computable rather
    # than blanked (see fy_return_equity).
    ca_by_isin = load_actions_by_isin(cur)

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
            r = fy_return_equity(cur, h, ysym, s, e, ledger_cache[key], split_cache[ysym],
                                 ca_by_isin.get(h["isin"]))
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

    # ---- foreign equity
    # Resolved straight off symbol_override (never security_symbol_map, which is keyed
    # on the bare symbol and collides: META is Meta Infotech on the BSE and Meta
    # Platforms on NASDAQ) — see the matching note in fy_price_backfill.
    cur.execute("""SELECT id, entity_id, broker, symbol, currency, quantity,
                          COALESCE(NULLIF(symbol_override,''), symbol) AS ysym
                   FROM foreign_equity_holding
                   ORDER BY broker, entity_id, symbol""")
    fgn = cur.fetchall()
    fgn_filled = fgn_rows = 0
    fgn_ledger_cache: dict[tuple, date | None] = {}
    for h in fgn:
        fkey = (h["entity_id"], h["broker"])
        if fkey not in fgn_ledger_cache:
            fgn_ledger_cache[fkey] = foreign_ledger_start(cur, *fkey)
        out = {}
        for s, e in fys:
            r = fy_return_foreign(cur, h, h["ysym"], s, e, fgn_ledger_cache[fkey])
            if r:
                out[fy_label(s.year)] = r
        fgn_rows += 1
        if out:
            fgn_filled += 1
        if args.commit:
            cur.execute("UPDATE foreign_equity_holding SET fy_returns=%s WHERE id=%s",
                        (json.dumps(out) if out else None, h["id"]))
    if args.commit:
        conn.commit()
    logger.info(f"Foreign: {fgn_filled}/{fgn_rows} holdings got at least one FY figure")

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
        cur.execute("SELECT COUNT(*) n FROM foreign_equity_holding WHERE fy_returns ? %s", (lbl,))
        fn = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM holding WHERE fy_returns ? %s", (lbl,))
        mn = cur.fetchone()["n"]
        logger.info(f"   FY{lbl}: equity={en:>3}/{eq_rows}  foreign={fn:>3}/{fgn_rows}  mf={mn:>3}/{mf_rows}")

    if not args.commit:
        logger.info("\nDRY RUN — nothing written. Re-run with --commit.")
    conn.close()


if __name__ == "__main__":
    main()
