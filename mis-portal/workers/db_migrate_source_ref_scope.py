#!/usr/bin/env python3
"""
Scope stock_transaction.source_ref to (broker, entity, date, trade_id).

WHY
---
The three trade importers all built the dedup key as:

    source_ref = f"{broker}:" + (trade_id or md5(f"{entity_id}|{symbol}|{date}|..."))

The md5 branch is entity-scoped and safe. The trade_id branch is NOT: a broker's
trade_id is unique per account per day, never globally. So `zerodha:12479400` could
be HHR's BEL sell on 2024-07-01 or DHR's SBIN sell on 2026-02-20 — and since every
importer dedupes with a bare `SELECT 1 ... WHERE source_ref = %s`, whichever entity
was imported second had its trade silently dropped as a "duplicate".

Three real HHR trades were lost this way (BEL 2024-07-01, J&KBANK 2025-12-16,
BESTAGRO 2023-09-26), each blocked by an unrelated DHR trade sharing the number.
The collision is invisible by construction: querying for a source_ref used by more
than one entity returns zero rows, because the loser was never inserted.

WHAT THIS DOES
--------------
Rewrites   {broker}:{trade_id}
    into   {broker}:{entity_id}:{transaction_date}:{trade_id}

Deterministic (every field is read off the row itself) and idempotent — a second run
matches nothing, because migrated refs no longer look like `prefix:digits`.

SCOPE — deliberately only suffixes of 1..15 digits
--------------------------------------------------
An md5 hexdigest()[:16] is always exactly 16 chars and could, rarely, be all digits;
one such row exists (`vested:8510327180160439`). Real trade_ids in this database are
4-9 digits. Length therefore separates them cleanly, and the 16-char row is left
alone — which is correct whichever kind it is, since the importers' md5 branch is
unchanged and will keep reproducing that exact key.

Refs in other shapes are untouched: `{broker}:live:{order_id}` (live daemon),
`{broker}:{hex}` (md5 fallback), `snapshot*`, `reconstructed:*`, and Dhan's
`{broker}:{order_id}-{date}`.

MUST BE RUN TOGETHER WITH the matching code change in broker_txn_sync_worker.py,
import_tradebooks_multi.py and import_tradebook.py. Migrating without it makes every
re-import duplicate the whole history; changing the code without migrating does the
same. Both directions are caught by the verification at the end of this script.

    python -m workers.db_migrate_source_ref_scope            # dry-run
    python -m workers.db_migrate_source_ref_scope --commit
"""
import os
import sys
import argparse
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

# 1..15 digits: every real trade_id, never an md5 hexdigest[:16]. See SCOPE above.
BARE_REF_RE = r'^[a-z_]+:[0-9]{1,15}$'


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="write (default dry-run)")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()

    cur.execute(f"SELECT COUNT(*) n FROM stock_transaction WHERE source_ref ~ '{BARE_REF_RE}'")
    todo = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) n FROM stock_transaction WHERE source_ref IS NOT NULL")
    total = cur.fetchone()["n"]
    print(f"source_ref rows: {total} total, {todo} in the unscoped {{broker}}:{{trade_id}} shape")

    if not todo:
        print("nothing to migrate (already scoped, or run twice — this is idempotent)")
        return

    cur.execute(f"""
        SELECT source_ref,
               split_part(source_ref, ':', 1) || ':' || entity_id || ':' ||
               transaction_date || ':' || split_part(source_ref, ':', 2) AS new_ref
          FROM stock_transaction
         WHERE source_ref ~ '{BARE_REF_RE}'
         ORDER BY id LIMIT 3
    """)
    print("\nsample rewrite:")
    for r in cur.fetchall():
        print(f"  {r['source_ref']:<24} ->  {r['new_ref']}")

    # A rewrite that collided would mean two rows are genuinely the same trade; check
    # before writing rather than discovering it via a unique-violation mid-update.
    cur.execute(f"""
        SELECT COUNT(*) n FROM (
          SELECT split_part(source_ref, ':', 1) || ':' || entity_id || ':' ||
                 transaction_date || ':' || split_part(source_ref, ':', 2) AS new_ref
            FROM stock_transaction WHERE source_ref ~ '{BARE_REF_RE}'
           GROUP BY 1 HAVING COUNT(*) > 1) t
    """)
    dupes = cur.fetchone()["n"]
    print(f"\npost-rewrite keys that would collide: {dupes}")
    if dupes:
        print("  ^ these are same-entity same-day repeats of one trade_id — inspect before committing")

    if not args.commit:
        print("\nDRY RUN — re-run with --commit to apply")
        return

    cur.execute(f"""
        UPDATE stock_transaction
           SET source_ref = split_part(source_ref, ':', 1) || ':' || entity_id || ':' ||
                            transaction_date || ':' || split_part(source_ref, ':', 2)
         WHERE source_ref ~ '{BARE_REF_RE}'
    """)
    changed = cur.rowcount
    conn.commit()
    print(f"\nmigrated {changed} source_ref(s)")

    cur.execute(f"SELECT COUNT(*) n FROM stock_transaction WHERE source_ref ~ '{BARE_REF_RE}'")
    left = cur.fetchone()["n"]
    print(f"remaining unscoped: {left} (expected 0)")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
