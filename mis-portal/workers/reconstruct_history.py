#!/usr/bin/env python3
"""
Self-made transaction history — reconcile each equity holding's REAL trade history
(tradebook + manual) to the quantity actually held today by synthesising ONE
balancing transaction for the gap.

Why: NRI conversion moved resident-demat holdings (e.g. Zerodha RW6148 -> RRM941,
KR0478 -> RRX118) into the new account via OFF-MARKET transfer, and some positions
were part-bought before the oldest tradebook we hold. Those shares carry no trade
record anywhere, so tradebook net != held qty and the metrics worker can't build
FIFO lots. We can't source the missing trades, so we reconstruct them from what we
do know: the shares exist (held qty), and their cost is the broker's average cost.

The synthetic row is tagged source='reconstructed' (authoritative tier in the
metrics worker, fully reversible — delete by that source to undo):
  * gap > 0  (held > recorded)  -> a BUY  for the shortfall (transferred-in / pre-history
             shares), dated at the EARLIEST real buy for that security (so inception
             is meaningful), priced at the holding's average cost.
  * gap < 0  (held < recorded)  -> a SELL for the excess (shares that left off-market),
             dated at the LAST real trade, priced at average cost (no phantom gain).

After the synthetic row, (real auth + reconstructed + post-cutoff snapshot) == held,
so lots reconstruct exactly. On --commit we also supersede the crude auto-seed the
importer would leave (snapshot_open entirely; snapshot up to the last real trade),
mirroring import_tradebooks_multi.

Default DRY-RUN. Re-runnable: skips a holding that already has a 'reconstructed' row.
  python -m workers.reconstruct_history --entity SDR,DHR,HHR            # dry-run
  python -m workers.reconstruct_history --entity SDR,DHR,HHR --commit
"""
import os
import sys
import argparse
import hashlib
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

NRI_ENTITIES = ["SDR", "DHR", "HHR"]


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
    )


def plan_for_holding(cur, h, tol):
    """Return a reconstruction plan dict for one holding, or a skip-reason string."""
    eid, broker, isin = h["entity_id"], h["broker"], h["isin"]
    qty = f(h["quantity"]) or 0.0
    cost = f(h["cost"]) or 0.0
    if qty <= 0:
        return "zero-qty"
    if not isin:
        return "no-isin"

    cur.execute("""SELECT st.id, st.transaction_date d, st.transaction_type side,
                          st.quantity q, st.source src
                   FROM stock_transaction st JOIN security_master sm ON sm.id=st.security_id
                   WHERE st.entity_id=%s AND sm.isin=%s
                     AND ( st.source=%s
                        OR (st.source IN ('manual','reconstructed','snapshot','snapshot_open')
                            AND st.broker=%s) )""",
                (eid, isin, broker, broker))
    rows = cur.fetchall()
    if any(r["src"] == "reconstructed" for r in rows):
        return "already-reconstructed"

    real = [r for r in rows if r["src"] not in ("snapshot", "snapshot_open")]
    real_buys = [r for r in real if r["side"] == "BUY"]
    if not real_buys:
        return "no-real-buys (leave to snapshot seed)"

    max_real = max(r["d"] for r in real)
    min_buy = min(r["d"] for r in real_buys)
    # snapshot rows that survive supersession (dated after the last real trade)
    kept_snap = [r for r in rows if r["src"] == "snapshot" and r["d"] > max_real]

    def net(rs):
        return sum((f(r["q"]) if r["side"] == "BUY" else -f(r["q"])) for r in rs)

    base_net = net(real) + net(kept_snap)
    gap = qty - base_net
    if abs(gap) <= tol:
        return None  # already reconciles — nothing to synthesise

    avg_cost = (cost / qty) if (cost > 0 and qty) else f(h["current_price"])
    if not avg_cost or avg_cost <= 0:
        return "no-cost-basis"

    side = "BUY" if gap > 0 else "SELL"
    date = min_buy if side == "BUY" else max_real
    return {
        "entity": h["entity_name"], "symbol": h["symbol"], "isin": isin, "broker": broker,
        "held": qty, "real_net": net(real), "base_net": base_net, "gap": gap,
        "side": side, "qty": abs(gap), "date": date, "price": round(avg_cost, 4),
        "max_real": max_real,
    }


def apply_plan(cur, eid, p):
    """Insert the synthetic row and supersede the auto-seed for that security."""
    cur.execute("SELECT id FROM security_master WHERE isin=%s ORDER BY id LIMIT 1", (p["isin"],))
    sec = cur.fetchone()
    if not sec:
        return 0
    sec_id = sec["id"]
    amount = p["qty"] * p["price"]
    sref = "reconstructed:" + hashlib.md5(
        f"{eid}|{p['broker']}|{p['isin']}".encode()).hexdigest()[:16]
    cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref=%s", (sref,))
    if cur.fetchone():
        return 0
    cur.execute("""
        INSERT INTO stock_transaction
          (entity_id, security_id, transaction_date, transaction_type, quantity, price,
           amount, total_cost, currency, exchange, source, source_ref, broker, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'INR',NULL,'reconstructed',%s,%s,NOW())
    """, (eid, sec_id, p["date"], p["side"], p["qty"], p["price"], amount, amount, sref, p["broker"]))
    # supersede the crude auto-seed this now replaces (mirror import_tradebooks_multi)
    cur.execute("DELETE FROM stock_transaction WHERE source='snapshot_open' "
                "AND entity_id=%s AND security_id=%s AND broker=%s", (eid, sec_id, p["broker"]))
    cur.execute("DELETE FROM stock_transaction WHERE source='snapshot' "
                "AND entity_id=%s AND security_id=%s AND broker=%s AND transaction_date<=%s",
                (eid, sec_id, p["broker"], p["max_real"]))
    return 1


def main():
    ap = argparse.ArgumentParser(description="Reconstruct equity history to held qty.")
    ap.add_argument("--entity", default=",".join(NRI_ENTITIES),
                    help="comma-separated entity names (default SDR,DHR,HHR)")
    ap.add_argument("--broker", help="broker filter (e.g. zerodha)")
    ap.add_argument("--tol", type=float, default=0.5, help="qty tolerance (default 0.5)")
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()
    entities = [e.strip() for e in args.entity.split(",") if e.strip()]

    conn = connect()
    cur = conn.cursor()
    q2 = conn.cursor()
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — entities {entities}"
          + (f", broker={args.broker}" if args.broker else "") + "\n")

    plans, skips = [], {}
    for ent in entities:
        cur.execute("SELECT id FROM entity WHERE entity_name=%s", (ent,))
        row = cur.fetchone()
        if not row:
            print(f"!! unknown entity {ent}"); continue
        eid = row["id"]
        sql = """SELECT h.*, %s AS entity_name FROM equity_holding h
                 WHERE h.entity_id=%s AND h.quantity>0"""
        params = [ent, eid]
        if args.broker:
            sql += " AND h.broker=%s"; params.append(args.broker)
        sql += " ORDER BY h.broker, h.symbol"
        cur.execute(sql, params)
        for h in cur.fetchall():
            res = plan_for_holding(q2, h, args.tol)
            if isinstance(res, dict):
                res["entity_id"] = eid
                plans.append(res)
            elif isinstance(res, str):
                skips[res] = skips.get(res, 0) + 1

    if plans:
        print(f"{'entity':5} {'symbol':13} {'broker':10} {'held':>8} {'real_net':>9} "
              f"{'gap':>8}  {'synthetic':>22}")
        for p in sorted(plans, key=lambda x: (x["entity"], x["broker"], x["symbol"])):
            syn = f"{p['side']} {p['qty']:.0f} @{p['price']:.2f} {p['date']}"
            print(f"{p['entity']:5} {p['symbol']:13} {p['broker']:10} {p['held']:>8.0f} "
                  f"{p['real_net']:>9.0f} {p['gap']:>+8.0f}  {syn:>22}")
    print(f"\n{len(plans)} holding(s) to reconstruct.  skips: {skips}")

    if args.commit and plans:
        n = 0
        for p in plans:
            n += apply_plan(q2, p["entity_id"], p)
        conn.commit()
        print(f"committed {n} synthetic row(s).  Now re-run: python -m workers.equity_txn_metrics_worker --commit")
    else:
        conn.rollback()
        print("dry-run — nothing written." if not args.commit else "nothing to write.")
    cur.close(); q2.close(); conn.close()


if __name__ == "__main__":
    main()
