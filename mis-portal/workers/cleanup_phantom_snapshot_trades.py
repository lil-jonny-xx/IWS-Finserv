#!/usr/bin/env python3
"""
One-off cleanup — remove PHANTOM snapshot trades caused by broker symbol relabels.

Background
----------
`equity_snapshot_worker.detect_trades` used to key its tick-to-tick position diff
on the raw broker *symbol* string. When a broker relabels an unchanged holding
overnight (same ISIN, same quantity — e.g. SGBAUG28V -> SGBAUG28V-GB, or
TRANSWORLD -> TRANSWORLD-BE), the old string dropped to 0 (read as a SELL) and the
new string appeared from 0 (read as a BUY). Both legs resolve to the SAME
security_id, so the phantom SELL consumed real cost basis and fabricated a realised
gain on the Equity / Realised-Gains page. No trade actually happened.

The worker is now fixed to diff on ISIN-first identity, so this cannot recur. This
script removes the phantom rows already written to `stock_transaction`.

What it targets (relabel signature — deliberately narrow, so genuine trades survive)
------------------------------------------------------------------------------------
A phantom pair is, within source='snapshot':
  * same entity_id, same security_id, same transaction_date,
  * exactly one BUY and one SELL,
  * identical |quantity|,
  * whose two broker symbols (parsed from source_ref) DIFFER.

The differing-symbol condition is what separates a relabel (SGBAUG28V vs
SGBAUG28V-GB) from a genuine same-day round-trip (same symbol on both legs), which
is left untouched. Genuine one-sided buys/sells (VEDPOWER, VISL, IIRM, ...) are
never matched because they have no equal-and-opposite partner.

Usage
-----
  python -m workers.cleanup_phantom_snapshot_trades           # DRY RUN (prints only)
  python -m workers.cleanup_phantom_snapshot_trades --apply   # actually delete

Idempotent: after --apply there are no matching pairs, so a re-run is a no-op.
"""
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)


def _symbol_from_ref(source_ref: str):
    """Broker symbol encoded in a legacy snapshot source_ref:
    'snapshot:{entity}|{symbol}|{ts}|{side}'  ->  symbol. New-format refs use the
    identity token ('ISIN:...' / 'SYM:...') in that slot, which never matches an old
    symbol string, so those are simply not paired."""
    try:
        return (source_ref or "").split("|")[1]
    except IndexError:
        return None


def main(apply: bool):
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT st.id, st.entity_id, e.entity_name, st.security_id, sm.security_name,
                   st.transaction_date, st.transaction_type AS side, st.quantity,
                   st.price, st.amount, st.source_ref
            FROM   stock_transaction st
            JOIN   entity e ON e.id = st.entity_id
            JOIN   security_master sm ON sm.id = st.security_id
            WHERE  st.source = 'snapshot'
            ORDER  BY st.entity_id, st.security_id, st.transaction_date, st.id
            """
        )
        rows = cur.fetchall()

        # Group by (entity, security, date, |qty|); a phantom pair = one BUY + one
        # SELL in the group whose broker symbols differ.
        groups: dict = {}
        for r in rows:
            key = (r["entity_id"], r["security_id"], r["transaction_date"],
                   abs(r["quantity"]))
            groups.setdefault(key, []).append(r)

        victim_ids = []
        pairs = []
        for key, grp in groups.items():
            buys = [r for r in grp if (r["side"] or "").upper() in ("BUY", "B", "PURCHASE")]
            sells = [r for r in grp if (r["side"] or "").upper() in ("SELL", "S", "SALE")]
            if len(buys) != 1 or len(sells) != 1:
                continue
            b, s = buys[0], sells[0]
            sym_b = _symbol_from_ref(b["source_ref"])
            sym_s = _symbol_from_ref(s["source_ref"])
            if sym_b is None or sym_s is None or sym_b == sym_s:
                continue  # same/undetermined symbol → not a relabel; leave it alone
            pairs.append((b, s))
            victim_ids.extend([b["id"], s["id"]])

        if not pairs:
            print("No phantom relabel pairs found — nothing to clean.")
            return

        print(f"Found {len(pairs)} phantom relabel pair(s) "
              f"({len(victim_ids)} stock_transaction rows):\n")
        for b, s in pairs:
            print(f"  {b['entity_name']} / {s['security_name']} "
                  f"({s['transaction_date']}, qty {abs(s['quantity'])}):")
            print(f"    SELL id={s['id']}  {_symbol_from_ref(s['source_ref']):<16} "
                  f"@ {s['price']}  amount={s['amount']}")
            print(f"    BUY  id={b['id']}  {_symbol_from_ref(b['source_ref']):<16} "
                  f"@ {b['price']}  amount={b['amount']}")

        if not apply:
            print("\nDRY RUN — no rows deleted. Re-run with --apply to delete the above.")
            return

        cur.execute(
            "DELETE FROM stock_transaction WHERE id = ANY(%s)", (victim_ids,)
        )
        conn.commit()
        print(f"\nDeleted {cur.rowcount} phantom row(s). Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv[1:])
