#!/usr/bin/env python3
"""
Equity holding metrics from transaction history.

Fills the equity_holding columns that the broker-API holdings feed leaves empty —
xirr_inception_pct, cagr_inception_pct, pnl_ytd, returns_ytd_pct — and backfills any
missing first_invested_date, using the stock_transaction history imported from broker
tradebooks (see import_tradebooks_multi.py).

Per holding (matched to transactions by entity + broker + ISIN):

  first_invested_date  earliest BUY date (only fills rows currently NULL).

  xirr_inception_pct   money-weighted return (equity.finmath.xirr). If the linked
                       transactions reconstruct the current quantity (full history),
                       real dated flows are used: each buy -qty*price, each sell
                       +qty*price, plus current market value as a final inflow today.
                       Otherwise a 2-point fallback is used: -cost at first_invested_date,
                       +current_market_value today (== money-weighted point-to-point).

  cagr_inception_pct   (cmv/cost)^(365/holding_days) - 1, from first_invested_date.

  pnl_ytd              FY-to-date P&L on the position: current_mv - mv_at_FY_start
                       - net_invested_during_FY  (buys-sells since 1-Apr). Captures
                       price change on the held position plus proceeds of any in-year
                       trims. mv_at_FY_start comes from equity_holding_history (which
                       begins 2026-04-01). Positions opened mid-FY have mv_start = 0.
  returns_ytd_pct      pnl_ytd / capital_base * 100, base = mv_at_FY_start or in-year cost.

Dry-run by default; pass --commit to write. --entity / --broker to scope.
"""
import os
import sys
import argparse
from datetime import date

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, "/var/www/mis-portal")
from equity.finmath import xirr

load_dotenv("/var/www/mis-portal/.env", override=True)

FY_START = date(2026, 4, 1)   # current Indian financial year start
TODAY = date.today()
INR_BROKERS = ("zerodha", "angel_one", "dhan")


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
    )


def f(x):
    return float(x) if x is not None else None


ANNUALISE_MIN_DAYS = 365   # only show annualised returns (CAGR/XIRR) once held ≥1 year

def ann_guard(value_pct, days):
    """Annualised returns (CAGR/XIRR) are only meaningful once a position has been held
    for a full year — below that the per-period rate gets annualised into a misleading
    figure (and explodes numerically for very short holds). For sub-year holdings the UI
    shows the absolute return (returns_inception_pct) instead, so suppress (NULL) anything
    under 365 days or outside a plausible [-99.99, 1000]% band."""
    if value_pct is None or days is None or days < ANNUALISE_MIN_DAYS:
        return None
    if value_pct < -99.99 or value_pct > 1000.0:
        return None
    return round(value_pct, 4)


def fy_start_price(cur, entity_id, broker, isin):
    """(market_value, quantity) at FY start from history; (0,0) if not held then."""
    cur.execute("""SELECT market_value, quantity FROM equity_holding_history
                   WHERE entity_id=%s AND broker=%s AND isin=%s AND snapshot_date=%s
                   ORDER BY market_value DESC LIMIT 1""", (entity_id, broker, isin, FY_START))
    row = cur.fetchone()
    if not row or not row["quantity"]:
        return 0.0, 0.0
    return f(row["market_value"]) or 0.0, f(row["quantity"]) or 0.0


def fifo_lots(txns):
    """Reconstruct the cost lots making up the CURRENT position via FIFO:
    sells consume the oldest buys, so what remains is the held shares with the real
    dates/prices they were bought at. Returns [(date, qty, price)]."""
    from collections import deque
    lots = deque()
    for t in txns:
        q, p, d = f(t["q"]), f(t["p"]), t["d"]
        if t["side"] == "BUY":
            lots.append([d, q, p])
        else:
            s = q
            while s > 1e-9 and lots:
                if lots[0][1] <= s + 1e-9:
                    s -= lots[0][1]; lots.popleft()
                else:
                    lots[0][1] -= s; s = 0
    return [(d, q, p) for d, q, p in lots if q > 1e-9]


def compute(cur, h):
    """Return dict of metric updates for one holding row h."""
    eid, broker, isin = h["entity_id"], h["broker"], h["isin"]
    cost = f(h["cost"]) or 0.0
    cmv = f(h["current_market_value"])
    cur_price = f(h["current_price"])
    qty = f(h["quantity"]) or 0.0
    out = {"method": "none"}

    txns = []
    if isin:
        cur.execute("""SELECT st.transaction_date d, st.transaction_type side,
                              st.quantity q, st.price p
                       FROM stock_transaction st JOIN security_master sm ON sm.id=st.security_id
                       WHERE st.entity_id=%s AND st.source=%s AND sm.isin=%s
                       ORDER BY st.transaction_date, st.id""", (eid, broker, isin))
        txns = cur.fetchall()

    # Does the transaction history reconstruct the current position?
    lots = []
    if txns:
        net_q = sum((f(t["q"]) if t["side"] == "BUY" else -f(t["q"])) for t in txns)
        if abs(net_q - qty) <= max(1.0, 0.02 * qty):
            lots = fifo_lots(txns)

    first_dt = h["first_invested_date"]
    if lots:                                          # earliest held-lot purchase = true inception
        lot_first = min(d for d, _, _ in lots)
        if first_dt is None or lot_first < first_dt:
            first_dt = lot_first
            out["first_invested_date"] = lot_first
    days = (TODAY - first_dt).days if first_dt else None

    # XIRR — held lots as dated outflows + current value inflow (no intraday churn)
    rate = None
    if lots and cmv is not None:
        flows = [(d, -q * p) for d, q, p in lots] + [(TODAY, cmv)]
        rate = xirr(flows)
        if rate is not None:
            out["method"] = "fifo-flow"
    if rate is None and first_dt and cost > 0 and cmv is not None:   # 2-point fallback
        rate = xirr([(first_dt, -cost), (TODAY, cmv)])
        if rate is not None:
            out["method"] = "2-point"
    if rate is not None:
        xirr_pct = ann_guard(rate * 100, days)
        if xirr_pct is not None:
            out["xirr_inception_pct"] = xirr_pct
        else:
            out["method"] += "*"                       # computed but suppressed (too short/extreme)

    # CAGR (point-to-point) — only annualise once held ≥1 year; below that the UI shows
    # the absolute return (returns_inception_pct) instead of a misleading annualised rate.
    if first_dt and cost > 0 and cmv is not None and days and days >= ANNUALISE_MIN_DAYS:
        cagr = ann_guard(((cmv / cost) ** (365.0 / days) - 1) * 100, days)
        if cagr is not None:
            out["cagr_inception_pct"] = cagr

    # FY-to-date P&L — price-based per held lot (bounded, churn-free)
    if cur_price is not None:
        mv0, qty0 = fy_start_price(cur, eid, broker, isin)
        p_fy = (mv0 / qty0) if qty0 else None
        if lots:
            pnl = base = 0.0
            for d, q, p in lots:
                ref = p if d >= FY_START else (p_fy if p_fy else p)
                pnl += q * (cur_price - ref)
                base += q * ref
            out["pnl_ytd"] = round(pnl, 2)
            if base > 0:
                out["returns_ytd_pct"] = round(pnl / base * 100, 4)
        elif qty0 and p_fy:                            # no lots: use FY-start price on current qty
            pnl = qty * (cur_price - p_fy)
            out["pnl_ytd"] = round(pnl, 2)
            if p_fy > 0:
                out["returns_ytd_pct"] = round((cur_price - p_fy) / p_fy * 100, 4)
    return out


def compute_foreign(cur, h):
    """Vested (foreign_equity_holding) YTD, computed FROM EACH HOLDING'S PURCHASE DATE.

    Foreign holdings have no FY-start price snapshot (foreign_equity_holding_history only
    starts mid-June), so — as requested — a Vested position's return is measured from the
    day it was bought, via its native-USD transaction lots (FIFO). pnl_ytd is stored in INR
    (native P&L x fx_rate); returns_ytd_pct is the native return. xirr/cagr/first_invested
    are left to the Vested scraper, which already sets them."""
    sym = h["symbol"]
    qty = f(h["quantity"]) or 0.0
    curp = f(h["current_price_native"])
    fx = f(h["fx_rate"]) or 1.0
    out = {"method": "none"}
    if curp is None:
        return out
    cur.execute("""SELECT st.transaction_date d, st.transaction_type side, st.quantity q, st.price p
                   FROM stock_transaction st JOIN security_master sm ON sm.id=st.security_id
                   WHERE st.entity_id=%s AND st.source='vested' AND sm.security_name=%s
                   ORDER BY st.transaction_date, st.id""", (h["entity_id"], sym))
    txns = cur.fetchall()
    if not txns:
        return out
    net_q = sum((f(t["q"]) if t["side"] == "BUY" else -f(t["q"])) for t in txns)
    if abs(net_q - qty) > max(1.0, 0.02 * qty):       # history doesn't reconstruct → skip
        return out
    lots = fifo_lots(txns)
    if not lots:
        return out
    pnl_native = sum(q * (curp - p) for _, q, p in lots)    # measured from each lot's buy price
    base = sum(q * p for _, q, p in lots)
    out["method"] = "fifo-buy"
    out["pnl_ytd"] = round(pnl_native * fx, 2)              # INR column
    if base > 0:
        out["returns_ytd_pct"] = round(pnl_native / base * 100, 4)
    return out


def run_foreign(cur, commit):
    cur.execute("""SELECT feh.*, e.entity_name FROM foreign_equity_holding feh
                   JOIN entity e ON e.id=feh.entity_id
                   WHERE feh.broker='vested' ORDER BY e.entity_name, symbol""")
    rows = cur.fetchall()
    n = 0
    print(f"\n--- Vested (foreign_equity_holding): {len(rows)} holding(s) ---")
    for h in rows:
        u = compute_foreign(cur, h)
        sets, vals = [], []
        for col in ("pnl_ytd", "returns_ytd_pct"):
            if u.get(col) is not None:
                sets.append(f"{col}=%s"); vals.append(u[col])
        if sets:
            n += 1
            if commit:
                cur.execute(f"UPDATE foreign_equity_holding SET {', '.join(sets)}, updated_at=NOW() WHERE id=%s",
                            vals + [h["id"]])
    print(f"Vested: filled pnl_ytd/returns_ytd on {n}/{len(rows)} (measured from purchase date)")


def main():
    ap = argparse.ArgumentParser(description="Backfill equity_holding metrics from transactions.")
    ap.add_argument("--entity", help="entity_name filter")
    ap.add_argument("--broker", choices=INR_BROKERS, help="broker filter")
    ap.add_argument("--no-foreign", action="store_true", help="skip Vested foreign holdings")
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    conn = connect(); cur = conn.cursor()
    where = ["broker = ANY(%s)"]; params = [list(INR_BROKERS)]
    if args.broker:
        where = ["broker = %s"]; params = [args.broker]
    if args.entity:
        cur.execute("SELECT id FROM entity WHERE entity_name=%s", (args.entity,))
        r = cur.fetchone()
        if not r: sys.exit(f"unknown entity {args.entity}")
        where.append("entity_id = %s"); params.append(r["id"])
    cur.execute(f"""SELECT eh.*, e.entity_name FROM equity_holding eh
                    JOIN entity e ON e.id=eh.entity_id
                    WHERE {' AND '.join(where)} ORDER BY e.entity_name, broker, symbol""", params)
    rows = cur.fetchall()

    methods = {"real-flow": 0, "2-point": 0, "none": 0}
    filled = {"xirr_inception_pct": 0, "cagr_inception_pct": 0, "pnl_ytd": 0,
              "returns_ytd_pct": 0, "first_invested_date": 0}
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — {len(rows)} holding(s)\n")
    print(f"{'ENT':5}{'BROKER':10}{'SYMBOL':16}{'method':10}{'XIRR%':>9}{'CAGR%':>9}{'YTD P&L':>12}")
    for h in rows:
        u = compute(cur, h)
        methods[u["method"]] = methods.get(u["method"], 0) + 1
        sets, vals = [], []
        for col in filled:
            if col in u and u[col] is not None:
                sets.append(f"{col}=%s"); vals.append(u[col]); filled[col] += 1
        if sets and args.commit:
            cur.execute(f"UPDATE equity_holding SET {', '.join(sets)}, updated_at=NOW() WHERE id=%s",
                        vals + [h["id"]])
        print(f"{h['entity_name'][:4]:5}{h['broker']:10}{h['symbol'][:15]:16}{u['method']:10}"
              f"{(u.get('xirr_inception_pct') or 0):>9.1f}{(u.get('cagr_inception_pct') or 0):>9.1f}"
              f"{(u.get('pnl_ytd') or 0):>12,.0f}")

    print(f"\nmethods: {methods}")
    print(f"filled : {filled}")

    if not args.no_foreign and not args.broker and not args.entity:
        run_foreign(cur, args.commit)

    if args.commit:
        conn.commit()
    print("\ncommitted." if args.commit else "\ndry-run — nothing written.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
