#!/usr/bin/env python3
"""
One-time migration for snapshot-derived stock_transaction rows.

Two jobs, both idempotent:

  1. BACKFILL broker — snapshot/snapshot_open rows historically carried no broker
     tag (the column is new). Fill it from equity_position_snapshot, which records
     (entity, broker, isin, symbol) for every tick. Without this the metrics worker
     can't scope a snapshot trade to the right broker (an ISIN held at two brokers
     by one entity would double-count).

  2. CLEAN stale snapshot_open — an opening seed must never coexist with real BUY
     history for the same (entity, security): the import / broker-sync dedup deletes
     it, but a seed can survive if authoritative rows were created by a path that
     bypassed that dedup (e.g. a manual ISIN repoint). Such a survivor double-counts
     the cost basis. Delete snapshot_open rows for any (entity, security) that has a
     non-snapshot BUY.

Dry-run by default; pass --apply to write. Going forward the snapshot worker tags
broker itself, so this only needs to run once (but re-running is harmless).
"""
import os
import sys
import argparse

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()

    # 1) Backfill broker from equity_position_snapshot (most-frequent broker per
    #    entity+security, matched by ISIN first then symbol).
    cur.execute("""
        WITH resolved AS (
            SELECT st.id,
                   (SELECT e.broker FROM equity_position_snapshot e
                    WHERE e.entity_id = st.entity_id
                      AND (e.isin = sm.isin OR e.symbol = sm.security_name)
                    GROUP BY e.broker ORDER BY COUNT(*) DESC LIMIT 1) AS broker
            FROM stock_transaction st JOIN security_master sm ON sm.id = st.security_id
            WHERE st.source IN ('snapshot','snapshot_open') AND st.broker IS NULL
        )
        SELECT COUNT(*) n, COUNT(broker) resolvable FROM resolved
    """)
    r = cur.fetchone()
    print(f"[backfill] {r['n']} snapshot rows missing broker; {r['resolvable']} resolvable")

    if args.apply and r["resolvable"]:
        cur.execute("""
            UPDATE stock_transaction st SET broker = res.broker
            FROM (
                SELECT st.id,
                       (SELECT e.broker FROM equity_position_snapshot e
                        WHERE e.entity_id = st.entity_id
                          AND (e.isin = sm.isin OR e.symbol = sm.security_name)
                        GROUP BY e.broker ORDER BY COUNT(*) DESC LIMIT 1) AS broker
                FROM stock_transaction st JOIN security_master sm ON sm.id = st.security_id
                WHERE st.source IN ('snapshot','snapshot_open') AND st.broker IS NULL
            ) res
            WHERE st.id = res.id AND res.broker IS NOT NULL
        """)
        print(f"[backfill] tagged {cur.rowcount} rows")

    # 2) Stale snapshot_open where authoritative BUY history exists.
    cur.execute("""
        SELECT e.entity_name, sm.security_name, st.id, st.transaction_date, st.quantity
        FROM stock_transaction st
        JOIN entity e ON e.id = st.entity_id
        JOIN security_master sm ON sm.id = st.security_id
        WHERE st.source = 'snapshot_open'
          AND EXISTS (SELECT 1 FROM stock_transaction r
                      WHERE r.entity_id = st.entity_id AND r.security_id = st.security_id
                        AND r.transaction_type = 'BUY'
                        AND r.source NOT IN ('snapshot','snapshot_open'))
        ORDER BY 1,2
    """)
    stale = cur.fetchall()
    print(f"\n[clean] {len(stale)} stale snapshot_open row(s) (real BUY history exists):")
    for s in stale:
        print(f"   {s['entity_name']:6} {s['security_name']:20} qty={s['quantity']} {s['transaction_date']}  id={s['id']}")

    if args.apply and stale:
        cur.execute("""
            DELETE FROM stock_transaction st
            WHERE st.source = 'snapshot_open'
              AND EXISTS (SELECT 1 FROM stock_transaction r
                          WHERE r.entity_id = st.entity_id AND r.security_id = st.security_id
                            AND r.transaction_type = 'BUY'
                            AND r.source NOT IN ('snapshot','snapshot_open'))
        """)
        print(f"[clean] deleted {cur.rowcount} stale snapshot_open row(s)")

    if args.apply:
        conn.commit()
        print("\ncommitted.")
    else:
        print("\ndry-run — nothing written. Re-run with --apply.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
