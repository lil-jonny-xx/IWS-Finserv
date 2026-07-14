#!/usr/bin/env python3
"""Snapshot-derived XIRR — the "middle path" that needs no tradebook.

Each daily `equity_holding_history` snapshot stores quantity + close_price, so a
position's cashflow series can be RECONSTRUCTED from day-over-day quantity changes:

    opening  : -(value held at the first snapshot)          [or -cost at the tradebook
                                                              first_invested_date if that
                                                              predates our snapshots]
    in-period: -(Δqty * close_price)  on every day qty moved  (buy = outflow, sell = inflow)
    terminal : +(current market value)                        (as if liquidated today)

Feed that to finmath.xirr → a money-weighted annual return that is INDEPENDENT of the
broker's tradebook. It is approximate: it uses the snapshot close price (not the real
fill price), misses same-day round-trips, and — until we accumulate a full year of
snapshots (we began 2026-04-01) — its annualisation is suppressed by the same ≥1-year
`ann_guard` used everywhere else. It writes `xirr_inception_pct` ONLY where the
transaction-derived worker left it NULL, so it never overrides a more-accurate tradebook
XIRR; it is a fallback + a forward-looking accumulator (it will start emitting real
values once each position's reconstructable history crosses 365 days).

Dry-run by default; --commit writes. `--show ISIN` prints the inferred flow series and
the raw (ungated) XIRR for one holding, for inspection.
"""
import os, sys, argparse
from datetime import date
from decimal import Decimal
import psycopg2, psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, "/var/www/mis-portal")
load_dotenv("/var/www/mis-portal/.env", override=True)
from equity.finmath import xirr                       # noqa: E402
from workers.equity_txn_metrics_worker import ann_guard, ANNUALISE_MIN_DAYS  # noqa: E402

TODAY = date.today()
QTY_EPS = Decimal("0.0001")     # ignore quantity noise below this


def f(x):
    return float(x) if x is not None else None


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def snapshot_series(cur, entity_id, broker, isin):
    """One (date, qty, price, mv) per snapshot date for an ISIN — deduped across symbol
    variants (history is symbol-keyed; an ISIN may appear under VAML and VAML-BE)."""
    cur.execute("""
        SELECT DISTINCT ON (snapshot_date)
               snapshot_date d, quantity q, close_price p, market_value mv
        FROM   equity_holding_history
        WHERE  entity_id=%s AND broker=%s AND isin=%s AND quantity IS NOT NULL
        ORDER  BY snapshot_date, market_value DESC NULLS LAST
    """, (entity_id, broker, isin))
    return cur.fetchall()


def build_flows(series, current_mv, first_invested_date, cost):
    """Cashflow list [(date, amount)] — outflow negative (buy), inflow positive (sell).

    Returns None when the series is unusable — including when a quantity jump looks
    like a corporate action (split/bonus): quantity moves materially but market value
    doesn't move with the implied cash flow. Treating a 1:1 split as a real buy would
    inject a huge phantom outflow and produce garbage XIRR, so the holding is skipped
    entirely (this worker is a best-effort fallback; skipping is safe)."""
    if not series or current_mv is None:
        return None
    flows = []
    first = series[0]
    q0 = Decimal(str(first["q"]))
    # Opening: prefer the true tradebook inception (date+cost) when it predates our first
    # snapshot — gives a longer, more accurate window; else open at first snapshot value.
    if first_invested_date and cost and first_invested_date < first["d"] and cost > 0:
        flows.append((first_invested_date, -float(cost)))
    else:
        mv0 = first["mv"] if first["mv"] is not None else (q0 * Decimal(str(first["p"] or 0)))
        flows.append((first["d"], -float(mv0)))
    # In-period flows from quantity changes.
    prev_q = q0
    prev_mv = first["mv"]
    for s in series[1:]:
        q = Decimal(str(s["q"]))
        dq = q - prev_q
        if abs(dq) >= QTY_EPS:
            price = s["p"]
            if price is None and s["mv"] is not None and q != 0:
                price = Decimal(str(s["mv"])) / q
            if price is not None:
                # Corporate-action guard: a ≥5% quantity move whose implied cash
                # flow is NOT reflected in the day's market-value change is
                # split/bonus-shaped — the "trade" moved no money.
                if (prev_q > 0 and prev_mv is not None and s["mv"] is not None
                        and float(abs(dq)) / float(prev_q) >= 0.05):
                    implied = abs(float(dq) * float(price))
                    if abs(float(s["mv"]) - float(prev_mv)) < 0.5 * implied:
                        return None
                flows.append((s["d"], -float(dq) * float(price)))
        prev_q = q
        prev_mv = s["mv"] if s["mv"] is not None else prev_mv
    # Terminal: liquidate at today's market value.
    flows.append((TODAY, float(current_mv)))
    return flows


def compute(cur, h):
    series = snapshot_series(cur, h["entity_id"], h["broker"], h["isin"])
    flows = build_flows(series, h["current_market_value"], h["first_invested_date"], h["cost"])
    if not flows or len(flows) < 2:
        return None, None, None
    open_date = flows[0][0]
    days = (TODAY - open_date).days
    rate = xirr(flows)
    raw = rate * 100 if rate is not None else None
    gated = ann_guard(raw, days)
    return raw, gated, days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--show", metavar="ISIN", help="print the inferred flow series for one ISIN")
    args = ap.parse_args()

    conn = connect(); cur = conn.cursor()
    where = "WHERE eh.isin IS NOT NULL"
    params = []
    if args.show:
        where += " AND eh.isin=%s"; params.append(args.show)
    cur.execute(f"""SELECT eh.id, eh.entity_id, eh.broker, eh.isin, eh.symbol, e.entity_name,
                           eh.quantity, eh.cost, eh.current_market_value, eh.first_invested_date,
                           eh.xirr_inception_pct
                    FROM equity_holding eh JOIN entity e ON e.id=eh.entity_id
                    {where} ORDER BY e.entity_name, eh.symbol""", params)
    rows = cur.fetchall()

    if args.show:
        for h in rows:
            series = snapshot_series(cur, h["entity_id"], h["broker"], h["isin"])
            flows = build_flows(series, h["current_market_value"], h["first_invested_date"], h["cost"])
            print(f"\n{h['entity_name']} {h['symbol']} ({h['isin']}) — {len(series)} snapshots")
            for d, amt in (flows or []):
                print(f"  {d}  {amt:>16,.2f}")
            raw, gated, days = compute(cur, h)
            print(f"  days={days}  raw XIRR={raw and round(raw,2)}%  "
                  f"gated(≥{ANNUALISE_MIN_DAYS}d)={gated}")
        return

    filled = would_fill = skipped_have = errored = 0
    for h in rows:
        try:
            raw, gated, days = compute(cur, h)
        except Exception as e:          # one bad row must not abort the whole run
            print(f"  ! {h['entity_name']} {h['symbol']} ({h['isin']}): {e}")
            errored += 1
            continue
        if h["xirr_inception_pct"] is not None:
            skipped_have += 1            # tradebook XIRR already present — never override
            continue
        if gated is not None:
            would_fill += 1
            if args.commit:
                cur.execute("UPDATE equity_holding SET xirr_inception_pct=%s, updated_at=NOW() WHERE id=%s",
                            (gated, h["id"]))
                filled += 1
    if args.commit:
        conn.commit()
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — {len(rows)} holding(s): "
          f"tradebook-XIRR already set {skipped_have}, snapshot-XIRR "
          f"{'filled' if args.commit else 'would fill'} {filled or would_fill}, errors {errored}.")
    print(f"(snapshots began 2026-04-01; annualised output stays suppressed until a "
          f"position's reconstructable window reaches {ANNUALISE_MIN_DAYS} days.)")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
