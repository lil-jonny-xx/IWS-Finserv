#!/usr/bin/env python3
"""De-duplicate equity_holding rows that share an ISIN under one (entity, broker).

Root cause: the unique key is (entity_id, broker, symbol). When a broker changes a
ticker's series suffix for the SAME security (e.g. VAML -> VAML-BE, GCHOTELS-SM ->
GCHOTELS-ST, SGBAUG28V -> SGBAUG28V-GB), the sync upsert sees a "new" symbol and
INSERTs a second row instead of updating — double-counting the holding in portfolio
totals/exposure.

This script:
  1. For every (entity_id, broker, isin) group with >1 row, picks a keeper (most
     populated txn-derived metrics; tie-break = lowest id) and merges any metric the
     keeper is missing from the other row(s) via COALESCE.
  2. Deletes the duplicate row(s).
  3. Swaps the unique constraint from (entity_id, broker, symbol) to
     (entity_id, broker, isin) so a future series-suffix change UPDATES the existing
     row (renaming its symbol) instead of inserting. ISIN is the stable natural key —
     one security can't be held twice in one account.

Safe to re-run. Dry-run by default; pass --commit to write. Aborts if any row has a
NULL/empty isin (the new constraint would be unsafe).
"""
import os, sys, argparse
import psycopg2, psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

METRIC_COLS = (
    "first_invested_date", "cagr_inception_pct", "xirr_inception_pct",
    "pnl_ytd", "returns_ytd_pct",
)
OLD_UNIQUE = "equity_holding_entity_id_broker_symbol_key"
NEW_UNIQUE = "equity_holding_entity_id_broker_isin_key"


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def metric_score(row):
    return sum(1 for c in METRIC_COLS if row[c] is not None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()

    conn = connect(); cur = conn.cursor()

    cur.execute("SELECT count(*) n FROM equity_holding WHERE isin IS NULL OR isin=''")
    if cur.fetchone()["n"]:
        sys.exit("ABORT: some equity_holding rows have NULL/empty isin — "
                 "isin-based unique constraint would be unsafe. Backfill ISIN first.")

    cur.execute(f"""
        SELECT * FROM equity_holding
        WHERE (entity_id, broker, isin) IN (
            SELECT entity_id, broker, isin FROM equity_holding
            GROUP BY entity_id, broker, isin HAVING count(*) > 1)
        ORDER BY entity_id, broker, isin, id
    """)
    rows = cur.fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r["entity_id"], r["broker"], r["isin"]), []).append(r)

    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — {len(groups)} duplicate ISIN group(s), "
          f"{len(rows)} rows\n")
    deletes, merges = [], []
    for key, grp in groups.items():
        keeper = max(grp, key=lambda r: (metric_score(r), -r["id"]))
        others = [r for r in grp if r["id"] != keeper["id"]]
        # metrics the keeper is missing but a sibling has
        fill = {}
        for c in METRIC_COLS:
            if keeper[c] is None:
                for o in others:
                    if o[c] is not None:
                        fill[c] = o[c]; break
        ent, brk, isin = key
        print(f"[{ent}/{brk}/{isin}] keep id={keeper['id']} ({keeper['symbol']}), "
              f"drop {[ (o['id'], o['symbol']) for o in others ]}"
              + (f"  merge-fill {fill}" if fill else ""))
        if fill:
            merges.append((keeper["id"], fill))
        deletes.extend(o["id"] for o in others)

    if not args.commit:
        print(f"\nDRY-RUN — would merge {len(merges)} keeper(s), delete {len(deletes)} row(s), "
              f"and swap unique constraint {OLD_UNIQUE} -> {NEW_UNIQUE}.")
        return

    for kid, fill in merges:
        sets = ", ".join(f"{c}=%s" for c in fill)
        cur.execute(f"UPDATE equity_holding SET {sets}, updated_at=NOW() WHERE id=%s",
                    list(fill.values()) + [kid])
    if deletes:
        cur.execute("DELETE FROM equity_holding WHERE id = ANY(%s)", (deletes,))
        print(f"deleted {cur.rowcount} duplicate row(s)")

    # Swap the unique constraint to the ISIN natural key.
    cur.execute("""SELECT 1 FROM pg_constraint WHERE conname=%s
                   AND conrelid='equity_holding'::regclass""", (NEW_UNIQUE,))
    if not cur.fetchone():
        cur.execute(f'ALTER TABLE equity_holding ADD CONSTRAINT {NEW_UNIQUE} '
                    f'UNIQUE (entity_id, broker, isin)')
        print(f"added unique constraint {NEW_UNIQUE}")
    cur.execute("""SELECT 1 FROM pg_constraint WHERE conname=%s
                   AND conrelid='equity_holding'::regclass""", (OLD_UNIQUE,))
    if cur.fetchone():
        cur.execute(f'ALTER TABLE equity_holding DROP CONSTRAINT {OLD_UNIQUE}')
        print(f"dropped old unique constraint {OLD_UNIQUE}")

    conn.commit()
    print("\ncommitted.")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
