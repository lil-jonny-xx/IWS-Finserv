#!/usr/bin/env python3
"""
Equity holding metrics from transaction history.

Fills the equity_holding columns that the broker-API holdings feed leaves empty —
xirr_inception_pct, cagr_inception_pct, pnl_ytd, returns_ytd_pct — and backfills any
missing first_invested_date, using stock_transaction history.

Trade history is drawn from three tiers per (entity, broker, ISIN), most to least
authoritative, so a position gets metrics with ZERO manual import:
  1. imported tradebook (source=broker) + manual register (source='manual')
  2. auto-captured intraday trades (source='snapshot', equity_snapshot_worker)
  3. the auto-seeded opening position (source='snapshot_open') — this is the
     self-building tradebook: a brand-new holding is seeded at its broker avg cost,
     so it reconstructs and gets full metrics without anyone importing anything.
Precedence mirrors the DB dedup: when authoritative BUY history exists we drop
snapshot_open and keep only snapshot rows dated after the last authoritative trade,
so a dedup gap can't double-count. Reconstructions that lean on snapshot data are
logged as method 'snap-flow' (vs 'fifo-flow' for pure tradebook history).

Per holding (matched to transactions by entity + broker + ISIN):

  first_invested_date  earliest BUY date (only fills rows currently NULL).

  xirr_inception_pct   money-weighted return (equity.finmath.xirr). If the linked
                       transactions reconstruct the current quantity (full history),
                       real dated flows are used: each buy -qty*price, each sell
                       +qty*price, plus current market value as a final inflow today.
                       Otherwise a 2-point fallback is used: -cost at first_invested_date,
                       +current_market_value today (== money-weighted point-to-point).

  cagr_inception_pct   (cmv/cost)^(365/holding_days) - 1, from first_invested_date.

  pnl_ytd              FY-to-date P&L on the HELD position, per FIFO lot: lots bought
                       during the FY are measured from their buy price; lots held
                       before 1-Apr from the FY-start price (nearest snapshot on or
                       before 1-Apr in equity_holding_history). Realised gains on
                       in-year trims are NOT included (they live in Realised Gains).
                       If a pre-FY lot has no FY-start snapshot (entity/broker
                       onboarded mid-FY), pnl_ytd is left NULL rather than mislabelling
                       the inception gain as YTD.
  returns_ytd_pct      pnl_ytd / capital_base * 100, base = sum of lot reference values.

Metric columns this worker owns (xirr/cagr/pnl_ytd/returns_ytd) are written every run
INCLUDING NULLs, so a holding that stops qualifying (broken txn linkage, suppressed
annualisation) has its stale value cleared instead of lingering.

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

TODAY = date.today()


def fy_start(d: date) -> date:
    """Start of the Indian financial year (1 April) containing date d. Self-advancing so
    YTD resets correctly each April instead of measuring from a stale hardcoded year."""
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


FY_START = fy_start(TODAY)     # current Indian financial year start
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
    """(market_value, quantity) at FY start from history; (0,0) if not held then.

    Nearest snapshot ON OR BEFORE 1-Apr (not exact-date): a single missed snapshot
    on Apr 1 must not silently zero the anchor. Snapshots began 2026-04-01, so for
    FY26-27 this is effectively the exact date."""
    cur.execute("""SELECT market_value, quantity FROM equity_holding_history
                   WHERE entity_id=%s AND broker=%s AND isin=%s AND snapshot_date<=%s
                   ORDER BY snapshot_date DESC, market_value DESC LIMIT 1""",
                (entity_id, broker, isin, FY_START))
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


def had_full_exit(txns) -> bool:
    """True if the position was fully closed (net qty returned to ~0) at some point
    and then re-opened. Units are netted per date so same-day trims can't false-trip.
    Used to decide whether the current-lot inception may move LATER than the stored
    date: a genuine sell-to-zero-then-rebuy resets inception, but a fresh snapshot_open
    seed (which just starts recent because that's all the history we have) must not."""
    per = {}
    for t in txns:
        q = f(t["q"]) or 0.0
        per[t["d"]] = per.get(t["d"], 0.0) + (q if t["side"] == "BUY" else -q)
    run = 0.0
    opened = False        # position has been positive at some point
    closed_once = False   # returned to flat after being open
    reopened = False      # opened again after a prior close
    for d in sorted(per):
        if per[d] > 1e-9 and run <= 1e-9 and closed_once:
            reopened = True
        run += per[d]
        if run > 1e-9:
            opened = True
        elif opened:
            closed_once = True
    return reopened


def compute(cur, h):
    """Return dict of metric updates for one holding row h."""
    eid, broker, isin = h["entity_id"], h["broker"], h["isin"]
    cost = f(h["cost"]) or 0.0
    cmv = f(h["current_market_value"])
    cur_price = f(h["current_price"])
    qty = f(h["quantity"]) or 0.0
    # Owned metric columns default to None and are ALWAYS written (clears stale
    # values when a holding stops qualifying); first_invested_date is only added
    # when it improves on the stored value.
    out = {"method": "none", "xirr_inception_pct": None, "cagr_inception_pct": None,
           "pnl_ytd": None, "returns_ytd_pct": None}

    # Three tiers of trade history for this (entity, broker, ISIN), most to least
    # authoritative:
    #   authoritative  = imported tradebook (source=broker) + manual register rows
    #   snapshot       = auto-captured intraday buys/sells (equity_snapshot_worker)
    #   snapshot_open  = the auto-seeded opening position for a stock that has no
    #                    tradebook — this is what makes the register self-building:
    #                    a new holding gets metrics with zero manual import.
    # Precedence (mirrors the DB dedup so a dedup gap can't double-count): when
    # authoritative BUY history exists, drop snapshot_open entirely and keep only
    # snapshot rows dated AFTER the last authoritative trade. Otherwise use the
    # snapshot tier as-is.
    txns = []
    used_snapshot = False
    if isin:
        cur.execute("""SELECT st.transaction_date d, st.transaction_type side,
                              st.quantity q, st.price p, st.source src
                       FROM stock_transaction st JOIN security_master sm ON sm.id=st.security_id
                       WHERE st.entity_id=%s AND sm.isin=%s
                         AND ( st.source=%s
                            OR (st.source='manual' AND st.broker=%s)
                            OR (st.source IN ('snapshot','snapshot_open') AND st.broker=%s) )
                       ORDER BY st.transaction_date, st.id""",
                    (eid, isin, broker, broker, broker))
        rows = cur.fetchall()
        auth = [r for r in rows if r["src"] not in ("snapshot", "snapshot_open")]
        auth_has_buy = any(r["side"] == "BUY" for r in auth)
        if auth_has_buy:
            max_auth = max(r["d"] for r in auth)
            txns = [r for r in rows
                    if r["src"] not in ("snapshot", "snapshot_open")
                    or (r["src"] == "snapshot" and r["d"] > max_auth)]
        else:
            txns = rows
        used_snapshot = any(r["src"] in ("snapshot", "snapshot_open") for r in txns)

    # Does the transaction history reconstruct the current position?
    # Tolerance: 2% of quantity; the 1-share absolute slack only applies to larger
    # positions — for a 2-share holding a ±1 mismatch is a 50% error, and lots built
    # from such history are wrong.
    lots = []
    if txns:
        net_q = sum((f(t["q"]) if t["side"] == "BUY" else -f(t["q"])) for t in txns)
        tol = max(1.0, 0.02 * qty) if qty >= 50 else 0.02 * qty + 1e-6
        if abs(net_q - qty) <= tol:
            lots = fifo_lots(txns)

    first_dt = h["first_invested_date"]
    if lots:                                          # current held-lot start = true inception
        lot_first = min(d for d, _, _ in lots)
        # Move earlier freely; move LATER only on a genuine sell-to-zero-then-rebuy
        # (had_full_exit) so a fresh snapshot_open seed can't reset a real old
        # inception to a recent date, while an exited-and-re-entered stock resets
        # off its closed lot instead of anchoring to the original buy.
        if first_dt is None or lot_first < first_dt or \
           (lot_first > first_dt and had_full_exit(txns)):
            first_dt = lot_first
            out["first_invested_date"] = lot_first
    days = (TODAY - first_dt).days if first_dt else None

    # XIRR — held lots as dated outflows + current value inflow (no intraday churn)
    rate = None
    if lots and cmv is not None:
        flows = [(d, -q * p) for d, q, p in lots] + [(TODAY, cmv)]
        rate = xirr(flows)
        if rate is not None:
            out["method"] = "snap-flow" if used_snapshot else "fifo-flow"
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
        cagr = ann_guard(((cmv / cost) ** (365.25 / days) - 1) * 100, days)
        if cagr is not None:
            out["cagr_inception_pct"] = cagr

    # FY-to-date P&L — price-based per held lot (bounded, churn-free)
    if cur_price is not None:
        mv0, qty0 = fy_start_price(cur, eid, broker, isin)
        p_fy = (mv0 / qty0) if qty0 else None
        if lots:
            pnl = base = 0.0
            for d, q, p in lots:
                if d >= FY_START:
                    ref = p                            # bought in-year: from buy price
                elif p_fy:
                    ref = p_fy                         # held at FY start: from FY-start price
                else:
                    # Pre-FY lot with no FY-start snapshot (entity/broker onboarded
                    # mid-FY). Measuring from the buy price would mislabel the whole
                    # inception gain as "YTD" — leave pnl_ytd NULL instead.
                    pnl = base = None
                    break
                pnl += q * (cur_price - ref)
                base += q * ref
            if pnl is not None:
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

    metric_cols = ("xirr_inception_pct", "cagr_inception_pct", "pnl_ytd", "returns_ytd_pct")
    methods = {"fifo-flow": 0, "2-point": 0, "none": 0}
    filled = {c: 0 for c in (*metric_cols, "first_invested_date")}
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — {len(rows)} holding(s)\n")
    print(f"{'ENT':5}{'BROKER':10}{'SYMBOL':16}{'method':10}{'XIRR%':>9}{'CAGR%':>9}{'YTD P&L':>12}")
    for h in rows:
        u = compute(cur, h)
        methods[u["method"]] = methods.get(u["method"], 0) + 1
        # Owned metric columns are written every run INCLUDING NULLs so stale values
        # clear when a holding stops qualifying; first_invested_date only when improved.
        sets = [f"{c}=%s" for c in metric_cols]
        vals = [u[c] for c in metric_cols]
        for c in metric_cols:
            if u[c] is not None:
                filled[c] += 1
        if u.get("first_invested_date") is not None:
            sets.append("first_invested_date=%s")
            vals.append(u["first_invested_date"])
            filled["first_invested_date"] += 1
        if args.commit:
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
