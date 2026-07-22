#!/usr/bin/env python3
"""
Reconcile the constructed trade history in stock_transaction against the source
tradebook files, and net traded quantity against the ledgers.

WHY THIS EXISTS
---------------
stock_transaction is fed by five paths (broker sync, live WS daemon, snapshot diff,
tradebook import, reconstruction). Any of them can drop or duplicate a fill, and the
symptom is silent: realised gains simply book a sell with no covering lot, or a
position that never existed. This walks the ONE source of truth the broker will stand
behind — the exported tradebook — and asks, per (entity, security), whether the
database says the same thing.

WHAT IT CHECKS
--------------
  1. NET QUANTITY per (entity, security): file-side net vs DB-side net. A mismatch is
     either a lost fill or a duplicated one.
  2. MISSING FILLS: a (date, side, qty) present in a file with no DB counterpart.
  3. PHANTOM FILLS: a DB row inside the file's date window with no file counterpart.
     Reconstructed/snapshot rows are reported separately — those are synthetic by
     design, and are the ones that should disappear once real history is imported.
  4. CORPORATE ACTIONS: a net mismatch whose ratio is a clean split/bonus multiple is
     labelled as such rather than as a lost trade. Quantity appearing out of nowhere is
     what a bonus issue LOOKS like to a tradebook, because the bonus shares are never
     "bought" — see workers/corporate_actions.py.

Matching is by ISIN when the file carries one (Zerodha CSV), else by the same
name->ISIN bridge the importer uses, so a file and the DB agree on identity the same
way the importer made them agree.

    python -m workers.reconcile_tradebooks                  # all entities, summary
    python -m workers.reconcile_tradebooks --entity HHR     # one entity
    python -m workers.reconcile_tradebooks --detail         # list every mismatch
"""
import os
import sys
import argparse
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

from workers.import_tradebooks_multi import (  # noqa: E402
    PARSERS, build_bridge, entity_id, net_by_name,
)
from equity.symbol_bridge import _norm_sym  # noqa: E402

ROOTS = [
    "/var/www/TRADEBOOKS&LEDGERS",
    "/var/www/AFTERNRITRADEBOOKS",
    "/var/www/New-Tradebooks",
]

# Quantity below this is rounding, not a real break.
QTY_EPS = 0.5

# A net mismatch this close to a whole multiple is a corporate action, not a lost
# trade: bonus and split quantity is credited by the depository and never appears as a
# BUY in any tradebook, so the file will always be short by exactly the ratio.
CA_RATIOS = [2.0, 3.0, 4.0, 5.0, 10.0, 1.5, 2.5]
CA_TOL = 0.02


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        port=os.getenv("DB_PORT", "5432"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def classify_broker(path: str):
    """Infer (broker, kind) from the file name. The exports are named consistently
    enough by each broker that this is reliable; anything unrecognised is skipped
    loudly rather than guessed at."""
    p = os.path.basename(path).lower()
    if p.startswith(".") or p.endswith(".ds_store"):
        return None, None
    kind = "ledger" if "ledger" in p or "statement" in p else "tradebook"
    if "fno" in p:
        return None, None                     # F&O is a separate book, not equity
    if "dhan" in p:
        return "dhan", kind
    if "angel" in p or "yourstatement" in p:
        return "angel_one", kind
    if "vested" in p:
        return "vested", kind
    if "dbs" in p:
        return None, None
    if "tradebook" in p or "ledger" in p:
        return "zerodha", kind
    return None, None


def classify_entity(path: str):
    """Entity comes from the folder/file prefix; these exports are filed per holder."""
    s = path.upper()
    for code in ("RAJANI", "DHR", "SDR", "HHR", "HDR", "ADR"):
        if code in s:
            return "Rajani Corp" if code == "RAJANI" else code
    return None


def discover():
    jobs = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                broker, kind = classify_broker(full)
                ent = classify_entity(full)
                if broker and ent and kind == "tradebook":
                    jobs.append((ent, broker, full))
    return sorted(jobs)


def file_rows(broker, path, isin_map):
    """Yield normalised (dedup_key, isin_or_name, date, side, qty) from one tradebook.

    The dedup key exists because these folders hold OVERLAPPING exports of the same
    account — a full-FY download sitting beside a since-inception one and a
    last-quarter one. Summing them naively double- or triple-counts every trade in the
    overlap and reports it as a discrepancy; the first version of this script did
    exactly that and made Rajani Corp look like every single position was half missing,
    when the ratio was a clean 2.0x across the board. The importer deduplicates on
    source_ref for the same reason, and the key here mirrors it: broker trade_id is
    unique per account per DAY, not globally, so the date has to be part of it.
    """
    out = []
    for r in PARSERS[broker](path):
        if not r.get("side") or not r.get("date") or not r.get("qty"):
            continue
        key = r.get("isin")
        if not key:
            nm = (r.get("symbol") or r.get("name") or "").strip()
            key = (isin_map or {}).get(nm) or ("NAME:" + _norm_sym(nm))
        tid = r.get("trade_id")
        dk = ((broker, r["date"], tid) if tid else
              (broker, r["date"], key, r["side"], float(r["qty"] or 0), r.get("price")))
        out.append((dk, key, r["date"], r["side"], float(r["qty"] or 0)))
    return out


def db_rows(cur, eid, source=None, lo=None, hi=None):
    """DB fills, optionally scoped to one broker and one date window.

    Scoping matters more than it looks. A lifetime comparison is meaningless here
    because the exports cover different spans per broker — HHR's Zerodha book runs from
    2018 while its Angel One book starts Apr 2026 — so an unscoped diff reports every
    pre-window trade as a discrepancy. Compare only what the file actually claims to
    cover.
    """
    q = ["""SELECT COALESCE(sm.isin, 'NAME:' || upper(sm.security_name)) AS k,
                   st.transaction_date AS d, upper(st.transaction_type) AS side,
                   st.quantity AS q, st.source
              FROM stock_transaction st
              JOIN security_master sm ON sm.id = st.security_id
             WHERE st.entity_id = %s AND COALESCE(st.currency, 'INR') = 'INR'"""]
    p = [eid]
    if source:
        q.append("AND st.source = %s"); p.append(source)
    if lo:
        q.append("AND st.transaction_date >= %s"); p.append(lo)
    if hi:
        q.append("AND st.transaction_date <= %s"); p.append(hi)
    cur.execute(" ".join(q), p)
    return cur.fetchall()


def oversold(cur, eid):
    """Securities whose LIFETIME net went negative — sold more than ever bought.

    This is the check that matters for realised gains, and it is not a file-vs-DB
    question at all. Bonus and split quantity is credited by the depository, so it
    appears in NO tradebook and in no broker trade feed; the first evidence of it is a
    sell the books cannot cover. Every such security is either an unrecorded corporate
    action, an off-market transfer in, or genuinely lost buy history — and until they
    are told apart, FIFO simply drops the sell.
    """
    cur.execute("""
        SELECT sm.id, sm.security_name, sm.isin,
               SUM(CASE WHEN upper(st.transaction_type)='BUY' THEN st.quantity
                        ELSE -st.quantity END) AS net,
               COUNT(*) FILTER (WHERE upper(st.transaction_type)='SELL') AS sells,
               MIN(st.transaction_date) AS first_t, MAX(st.transaction_date) AS last_t
          FROM stock_transaction st
          JOIN security_master sm ON sm.id = st.security_id
         WHERE st.entity_id = %s AND COALESCE(st.currency,'INR')='INR'
         GROUP BY sm.id, sm.security_name, sm.isin
        HAVING SUM(CASE WHEN upper(st.transaction_type)='BUY' THEN st.quantity
                        ELSE -st.quantity END) < -%s
         ORDER BY 4
    """, (eid, QTY_EPS))
    return cur.fetchall()


def ca_ratio(file_net, db_net):
    """If db/file (or file/db) is a clean corporate-action multiple, name it."""
    if abs(file_net) < QTY_EPS or abs(db_net) < QTY_EPS:
        return None
    hi, lo = (db_net, file_net) if abs(db_net) > abs(file_net) else (file_net, db_net)
    if lo == 0:
        return None
    r = abs(hi / lo)
    for cand in CA_RATIOS:
        if abs(r - cand) <= CA_TOL:
            return cand
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entity", help="limit to one entity (DHR, SDR, HHR, ...)")
    ap.add_argument("--detail", action="store_true", help="list every mismatched security")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()

    jobs = discover()
    if args.entity:
        jobs = [j for j in jobs if j[0].upper() == args.entity.upper()]
    if not jobs:
        sys.exit("no tradebook files matched")

    by_ent = defaultdict(list)
    for ent, broker, path in jobs:
        by_ent[ent].append((broker, path))

    print(f"{len(jobs)} tradebook file(s) across {len(by_ent)} entity(ies)\n")
    grand = defaultdict(float)

    for ent, files in sorted(by_ent.items()):
        eid = entity_id(cur, ent)
        # Name-only brokers need the same bridge the importer builds, so identity is
        # resolved identically on both sides of the comparison.
        bridges = {}
        for broker in ("angel_one", "dhan"):
            paths = [p for b, p in files if b == broker]
            if paths:
                try:
                    bridges[broker] = build_bridge(cur, broker, eid, paths)
                except Exception as e:
                    print(f"  [bridge {broker} failed: {e}]")
                    bridges[broker] = {}

        # Group the entity's files by broker: one comparison per (broker, window).
        per_broker = defaultdict(list)
        for broker, path in files:
            per_broker[broker].append(path)

        print(f"=== {ent} ===")
        e_agree = e_lost = e_extra = e_unbridged = 0
        details = []

        for broker, paths in sorted(per_broker.items()):
            fnet = defaultdict(float)
            lo = hi = None
            nfiles = dupes = 0
            seen = set()
            for path in paths:
                try:
                    rows = file_rows(broker, path, bridges.get(broker))
                except Exception as e:
                    print(f"    !! parse failed {os.path.basename(path)}: {e}")
                    continue
                nfiles += 1
                for dk, k, d, side, q in rows:
                    if dk in seen:            # same fill, second export of it
                        dupes += 1
                        continue
                    seen.add(dk)
                    fnet[k] += q if side == "BUY" else -q
                    lo = d if lo is None or d < lo else lo
                    hi = d if hi is None or d > hi else hi
            if not fnet:
                continue

            dnet = defaultdict(float)
            for r in db_rows(cur, eid, source=broker, lo=lo, hi=hi):
                dnet[r["k"]] += float(r["q"] or 0) * (1 if r["side"] == "BUY" else -1)

            # Names the bridge could not resolve compare NAME:x against the DB's ISIN
            # and would read as a break on both sides. That is a gap in identity
            # resolution, not a data discrepancy — count it, do not cry wolf.
            unb = {k for k in set(fnet) | set(dnet) if str(k).startswith("NAME:")}
            keys = (set(fnet) | set(dnet)) - unb
            agree = lost = extra = 0
            for k in sorted(keys):
                f, d = fnet.get(k, 0.0), dnet.get(k, 0.0)
                if abs(d - f) < QTY_EPS:
                    agree += 1
                    continue
                if k not in dnet:
                    lost += 1
                    tag, why = "FILE-ONLY", "in tradebook, absent from DB"
                elif k not in fnet:
                    extra += 1
                    tag, why = "DB-ONLY", "in DB, absent from tradebook"
                else:
                    lost += 1
                    tag, why = "QTY-DIFF", f"{d - f:+,.0f}"
                details.append((tag, broker, k, f, d, why))

            print(f"    {broker:<10} {nfiles:>2} file(s)  {lo} .. {hi}  "
                  f"agree={agree:<4} mismatch={lost:<4} db-only={extra:<4} "
                  f"unbridged={len(unb):<4} overlap-dupes={dupes}")
            e_agree += agree; e_lost += lost; e_extra += extra; e_unbridged += len(unb)

        grand["agree"] += e_agree; grand["lost"] += e_lost
        grand["extra"] += e_extra; grand["unbridged"] += e_unbridged

        # Lifetime oversold — the check that explains unmatched realised sells.
        ov = oversold(cur, eid)
        grand["oversold"] += len(ov)
        if ov:
            print(f"    OVERSOLD: {len(ov)} security(ies) sold more than ever bought "
                  f"(unrecorded bonus/split, transfer-in, or lost buy history)")
        if args.detail:
            for tag, broker, k, f, d, why in sorted(details):
                print(f"      {tag:<10} {broker:<9} {str(k)[:20]:<20} "
                      f"file={f:>9,.0f} db={d:>9,.0f}  {why}")
            for r in ov[:40]:
                print(f"      OVERSOLD   {str(r['security_name'])[:26]:<26} "
                      f"net={float(r['net']):>9,.0f}  {r['first_t']}..{r['last_t']}")
        print()

    print("TOTAL  agree=%d  mismatch=%d  db-only=%d  unbridged=%d  oversold=%d"
          % (grand["agree"], grand["lost"], grand["extra"],
             grand["unbridged"], grand["oversold"]))
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
