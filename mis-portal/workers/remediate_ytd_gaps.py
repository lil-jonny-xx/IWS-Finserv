#!/usr/bin/env python3
"""
Repair the ledger defects that leave equity holdings without FIFO lots (and so without
pnl_ytd / returns_ytd_pct). Found by auditing all 234 holdings on 2026-08-10.

Three independent fixes, each re-DETECTED at run time rather than hardcoded to row ids,
so the script stays correct if the data has moved since the audit:

  duplicates   The same fills recorded twice by two different ingestion paths. Each
               path writes a complete record of the day under its own source_ref
               scheme, so the ref-based dedup never sees a collision and the position
               reads double. broker_txn_sync_worker already supersedes '{broker}:live:%'
               rows once the authoritative trade_id fills land (see its sync_trades) —
               these are the ones that escaped it, plus one Dhan pair where the
               tradebook import and the API poll both booked the same trade.
               Verified damage: 4 groups, 2026-07-14 (Zerodha) and 2026-06-01 (Dhan).

  misdated     A 'reconstructed' BUY plug dated AFTER the sells it exists to cover.
  plugs        FIFO consumes oldest-first and cannot reach backwards, so the plug is
               never eaten and survives as phantom lots. HDR JIOFIN: plug of 28,070
               dated 2025-08-01 against sells on 2025-01-03 and 2025-04-30, leaving
               31,570 reconstructed against 3,500 held. The quantity is right; only
               the date is wrong, so this re-dates rather than deletes.

  unrecorded   Shares held and settled per the broker but with no trade record at all
  shares       (off-market transfer in, or a fill older than any tradebook we hold).
               Delegated to workers.reconstruct_history so there is exactly one
               implementation of plug pricing/dating. DHR FEDFINA: 1,000 shares,
               confirmed against the live Kite feed (6,585 settled, t1_quantity 0).

DELIBERATELY NOT FIXED — DHR angel_one CSLFINANCE-EQ (100 sh, ~₹0.2L). It holds shares
on Angel One with zero Angel One trades, so reconstruct_history skips it
('no-real-buys') and there is no snapshot seed either. Whether that is a transfer-in or
a mis-attributed broker is a judgement call about the real world, not a data repair.

Everything runs in ONE transaction: mutations are applied, the affected holdings are
then re-reconstructed IN THAT SAME uncommitted transaction, and the before/after is
printed. Without --commit the transaction is rolled back, so a dry-run reports the real
outcome rather than a prediction.

  python -m workers.remediate_ytd_gaps                    # dry-run, full report
  python -m workers.remediate_ytd_gaps --commit
  python -m workers.remediate_ytd_gaps --only duplicates  # duplicates,plugs,unrecorded
"""
import argparse
import re
import sys
from collections import defaultdict
from datetime import timedelta

sys.path.insert(0, "/var/www/mis-portal")

from workers import equity_txn_metrics_worker as M
from workers import reconstruct_history as R
from workers.corporate_actions import load_actions_by_isin

FIXES = ("duplicates", "plugs", "unrecorded")

# Which ingestion path wrote a row, from its source_ref shape.
_SCHEMES = (
    ("live-ws",        lambda s, r: ":live:" in r),
    ("trades-api",     lambda s, r: bool(re.match(r"^\w+:\d+:\d{4}-\d{2}-\d{2}:", r))),
    ("dhan-composite", lambda s, r: bool(re.match(r"^dhan:\d+-\d{4}-\d{2}-\d{2}T", r))),
    ("dhan-hash",      lambda s, r: bool(re.match(r"^dhan:[0-9a-f]{16}$", r))),
)

# Which path to KEEP when two claim the same fills. live-ws is lowest because
# broker_txn_sync_worker documents the authoritative trade_id fill as the truth (exact
# price, settled qty, correct date). The Dhan pair is a genuine tie — identical qty and
# price from the tradebook export and the API poll — so it falls to created_at order.
PRECEDENCE = {"trades-api": 3, "dhan-hash": 3, "dhan-composite": 2, "live-ws": 1}


def scheme(src, ref):
    ref = ref or ""
    if src in ("manual", "reconstructed", "snapshot", "snapshot_open"):
        return src
    for name, test in _SCHEMES:
        if test(src, ref):
            return name
    return f"other({src})"


def reconstructed_qty(cur, h, ca_by_isin):
    """Quantity the ledger reconstructs for this holding, and whether it reconciles —
    the exact gate equity_txn_metrics_worker uses to decide if it can build lots."""
    eid, broker, isin = h["entity_id"], h["broker"], h["isin"]
    qty = M.f(h["quantity"]) or 0.0
    if not isin:
        return None, False
    cur.execute("""SELECT st.transaction_date d, st.transaction_type side,
                          st.quantity q, st.price p, st.source src
                   FROM stock_transaction st JOIN security_master sm ON sm.id=st.security_id
                   WHERE st.entity_id=%s AND sm.isin=%s
                     AND ( st.source=%s
                        OR (st.source IN ('manual','reconstructed') AND st.broker=%s)
                        OR (st.source IN ('snapshot','snapshot_open') AND st.broker=%s) )
                   ORDER BY st.transaction_date, st.id""",
                (eid, isin, broker, broker, broker))
    rows = cur.fetchall()
    if not rows:
        return None, False
    auth = [r for r in rows if r["src"] not in ("snapshot", "snapshot_open")]
    if any(r["side"] == "BUY" for r in auth):
        ma = max(r["d"] for r in auth)
        txns = [r for r in rows if r["src"] not in ("snapshot", "snapshot_open")
                or (r["src"] == "snapshot" and r["d"] > ma)]
    else:
        txns = rows
    net = sum(q for _, q, _ in M.fifo_lots(txns, (ca_by_isin or {}).get(isin)))
    tol = max(1.0, 0.02 * qty) if qty >= 50 else 0.02 * qty + 1e-6
    return net, abs(net - qty) <= tol


# ---------------------------------------------------------------------------
# Fix 1 — fills recorded twice by two ingestion paths
# ---------------------------------------------------------------------------

def find_duplicates(cur):
    """(entity, broker, security, date, side) groups claimed by two feed paths.

    Broker is part of the key on purpose: one entity legitimately trades the same
    security on the same day in two different broker accounts, and that is not a
    duplicate. Feed rows often leave st.broker NULL, where `source` names the broker.
    Only EXACT quantity matches are treated as duplicates — a partial overlap means the
    two paths disagree about the day, which is a reconciliation question for a human,
    not something to delete automatically.
    """
    cur.execute("""SELECT st.id, st.entity_id, e.entity_name, st.security_id,
                          sm.security_name, sm.isin, st.transaction_date d,
                          st.transaction_type side, st.quantity q, st.price p,
                          st.source src, st.source_ref ref, st.broker, st.created_at
                   FROM stock_transaction st
                   JOIN entity e ON e.id=st.entity_id
                   JOIN security_master sm ON sm.id=st.security_id
                   WHERE st.source NOT IN ('reconstructed','snapshot_open')""")
    grp = defaultdict(lambda: defaultdict(list))
    for r in cur.fetchall():
        key = (r["entity_name"], r["broker"] or r["src"], r["security_name"],
               r["isin"], r["d"], r["side"])
        grp[key][scheme(r["src"], r["ref"])].append(r)

    out = []
    for key, by_scheme in grp.items():
        feeds = {s: rs for s, rs in by_scheme.items() if s in PRECEDENCE and rs}
        if len(feeds) < 2:
            continue
        totals = {s: sum(float(x["q"]) for x in rs) for s, rs in feeds.items()}
        if len(set(round(v, 6) for v in totals.values())) != 1:
            continue                                   # not an exact double — leave it
        ranked = sorted(feeds, key=lambda s: (-PRECEDENCE[s],
                                              min(x["created_at"] for x in feeds[s])))
        keep, drop = ranked[0], ranked[1:]
        out.append({"key": key, "keep": keep, "keep_rows": feeds[keep],
                    "drop": {s: feeds[s] for s in drop}, "qty": totals[keep]})
    return out


def fix_duplicates(cur, commit, log):
    dupes = find_duplicates(cur)
    if not dupes:
        log("  none found")
        return [], 0
    touched, removed = [], 0
    for d in dupes:
        ent, brk, sec, isin, dt, side = d["key"]
        log(f"  {ent} / {brk} / {sec} {dt} {side} {d['qty']:g} sh")
        log(f"      keep  {d['keep']:15} {[r['id'] for r in d['keep_rows']]}")
        for s, rows in d["drop"].items():
            ids = [r["id"] for r in rows]
            log(f"      DROP  {s:15} {ids}  ({sum(float(r['q']) for r in rows):g} sh)")
            if commit:
                cur.execute("DELETE FROM stock_transaction WHERE id = ANY(%s)", (ids,))
                removed += cur.rowcount
        touched.append((ent, brk, isin))
    return touched, removed


# ---------------------------------------------------------------------------
# Fix 2 — reconstructed plugs dated after the sells they cover
# ---------------------------------------------------------------------------

def find_misdated_plugs(cur, ca_by_isin):
    """Plugs sitting after the point the ledger first goes short.

    A BUY plug exists because shares were held that no trade explains. If the running
    balance goes negative BEFORE the plug's date, those shares were demonstrably already
    there — the plug is dated too late and FIFO, which only consumes forwards, can never
    apply it. The cure is the date, not the quantity.

    Moving the date is NOT just a date change when a split or bonus sits between the two
    dates. reconstruct_history stores a plug in the basis of its OWN date (apply_plan
    divides the held-basis gap by cumulative_ratio_after), and the FIFO engines scale it
    forward at each ex-date. Re-date a 2026 plug back past a 2:1 split and the same row
    now means twice the shares. So the held-basis quantity and the total cost are what
    get preserved here; the stored quantity and price are re-expressed for the new date.
    """
    from workers.corporate_actions import cumulative_ratio_after

    cur.execute("""SELECT st.id, st.entity_id, e.entity_name, st.broker, sm.isin,
                          sm.security_name, st.transaction_date d, st.quantity q, st.price p
                   FROM stock_transaction st
                   JOIN entity e ON e.id=st.entity_id
                   JOIN security_master sm ON sm.id=st.security_id
                   WHERE st.source='reconstructed' AND st.transaction_type='BUY'""")
    out = []
    for plug in cur.fetchall():
        cur.execute("""SELECT st.transaction_date d, st.transaction_type side, st.quantity q
                       FROM stock_transaction st JOIN security_master sm ON sm.id=st.security_id
                       WHERE st.entity_id=%s AND sm.isin=%s AND st.id<>%s
                         AND ( st.source=%s OR (st.source IN ('manual','snapshot') AND st.broker=%s) )
                       ORDER BY st.transaction_date""",
                    (plug["entity_id"], plug["isin"], plug["id"], plug["broker"], plug["broker"]))
        per = defaultdict(float)
        for r in cur.fetchall():
            per[r["d"]] += float(r["q"]) * (1 if r["side"] == "BUY" else -1)
        run, first_short = 0.0, None
        for dt in sorted(per):
            run += per[dt]
            if run < -1e-9 and first_short is None:
                first_short = dt
        if not (first_short and plug["d"] > first_short):
            continue
        new_date = first_short - timedelta(days=1)
        actions = (ca_by_isin or {}).get(plug["isin"])
        f_old = cumulative_ratio_after(actions, plug["d"])
        f_new = cumulative_ratio_after(actions, new_date)
        # Preserve held-basis quantity (q x factor) and total cost (q x price).
        new_q = float(plug["q"]) * f_old / f_new
        new_p = float(plug["p"] or 0) * f_new / f_old
        out.append({"plug": plug, "first_short": first_short, "new_date": new_date,
                    "new_q": new_q, "new_p": new_p, "rescaled": abs(f_old - f_new) > 1e-9,
                    "f_old": f_old, "f_new": f_new})
    return out


def fix_misdated_plugs(cur, commit, log, ca_by_isin):
    bad = find_misdated_plugs(cur, ca_by_isin)
    if not bad:
        log("  none found")
        return [], 0
    touched, moved = [], 0
    for b in bad:
        p = b["plug"]
        log(f"  {p['entity_name']} / {p['broker']} / {p['security_name']} "
            f"plug id={p['id']} {float(p['q']):g} sh")
        log(f"      ledger first goes short on {b['first_short']}, plug dated {p['d']}")
        log(f"      RE-DATE {p['d']} -> {b['new_date']}")
        if b["rescaled"]:
            log(f"      RESCALE across corporate action: qty {float(p['q']):g} -> "
                f"{b['new_q']:g}, price {float(p['p'] or 0):,.2f} -> {b['new_p']:,.2f} "
                f"(split factor {b['f_old']:g} -> {b['f_new']:g}; held-basis qty and "
                f"total cost unchanged)")
        if commit:
            cur.execute("""UPDATE stock_transaction
                           SET transaction_date=%s, quantity=%s, price=%s,
                               amount=%s, total_cost=%s
                           WHERE id=%s""",
                        (b["new_date"], b["new_q"], b["new_p"],
                         b["new_q"] * b["new_p"], b["new_q"] * b["new_p"], p["id"]))
            moved += cur.rowcount
        touched.append((p["entity_name"], p["broker"], p["isin"]))
    return touched, moved


# ---------------------------------------------------------------------------
# Fix 3 — held shares with no trade record (delegated to reconstruct_history)
# ---------------------------------------------------------------------------

def fix_unrecorded(cur, commit, log, ca_by_isin, only_short=True):
    """Plug holdings whose ledger falls SHORT of the settled quantity.

    Restricted to short-fall (gap > 0) cases on purpose: a ledger running AHEAD of the
    demat is usually unsettled stock on its way in, and plugging that is precisely the
    T+1 phantom this codebase already had once (HDR TVSHLTD, 2026-07-21). Direction
    aside, all plug pricing and dating is reconstruct_history's, not reimplemented here.
    """
    cur.execute("""SELECT h.*, e.entity_name FROM equity_holding h
                   JOIN entity e ON e.id=h.entity_id
                   WHERE h.quantity>0 ORDER BY e.entity_name, h.broker, h.symbol""")
    holdings = cur.fetchall()
    touched, added = [], 0
    for h in holdings:
        net, ok = reconstructed_qty(cur, h, ca_by_isin)
        if ok or net is None:
            continue
        if only_short and net >= float(h["quantity"]):
            continue                                   # ledger ahead — not ours to plug
        plan = R.plan_for_holding(cur, h, tol=0.5, actions=(ca_by_isin or {}).get(h["isin"]))
        if not isinstance(plan, dict):
            log(f"  {h['entity_name']} / {h['broker']} / {h['symbol']}: skipped ({plan})")
            continue
        log(f"  {h['entity_name']} / {h['broker']} / {h['symbol']}: held {plan['held']:g}, "
            f"ledger {plan['base_net']:g}")
        log(f"      PLUG {plan['side']} {plan['qty']:.0f} @ {plan['price']:.2f} on {plan['date']}")
        if commit:
            added += R.apply_plan(cur, h["entity_id"], plan)
        touched.append((h["entity_name"], h["broker"], h["isin"]))
    if not touched:
        log("  none found")
    return touched, added


# ---------------------------------------------------------------------------

def snapshot_state(cur, keys, ca_by_isin):
    """Reconstruction state for the given (entity, broker, isin) holdings."""
    state = {}
    for ent, brk, isin in keys:
        cur.execute("""SELECT h.*, e.entity_name FROM equity_holding h
                       JOIN entity e ON e.id=h.entity_id
                       WHERE e.entity_name=%s AND h.broker=%s AND h.isin=%s""",
                    (ent, brk, isin))
        h = cur.fetchone()
        if not h:
            continue
        net, ok = reconstructed_qty(cur, h, ca_by_isin)
        out = M.compute(cur, h, ca_by_isin)
        state[(ent, brk, isin)] = {"sym": h["symbol"], "held": float(h["quantity"]),
                                   "net": net, "ok": ok, "ytd": out["pnl_ytd"],
                                   "method": out["method"]}
    return state


def main():
    ap = argparse.ArgumentParser(description="Repair ledger defects that blank pnl_ytd.")
    ap.add_argument("--only", help=f"comma-separated subset of {','.join(FIXES)}")
    ap.add_argument("--commit", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()
    run = [f.strip() for f in (args.only or ",".join(FIXES)).split(",") if f.strip()]
    bad = [f for f in run if f not in FIXES]
    if bad:
        sys.exit(f"unknown fix(es) {bad}; choose from {list(FIXES)}")

    conn = R.connect()
    cur = conn.cursor()
    ca_by_isin = load_actions_by_isin(cur)
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — fixes: {run}\n")

    def log(msg):
        print(msg)

    touched, counts = [], {}
    try:
        # Mutations always run; the transaction is what does or does not persist, so a
        # dry-run measures the real result instead of predicting it.
        if "duplicates" in run:
            print("[duplicates] fills recorded twice by two ingestion paths")
            t, n = fix_duplicates(cur, True, log); touched += t; counts["rows deleted"] = n
            print()
        if "plugs" in run:
            print("[plugs] reconstructed BUYs dated after the sells they cover")
            t, n = fix_misdated_plugs(cur, True, log, ca_by_isin); touched += t; counts["plugs re-dated"] = n
            print()
        if "unrecorded" in run:
            print("[unrecorded] settled shares with no trade record")
            t, n = fix_unrecorded(cur, True, log, ca_by_isin); touched += t
            counts["plugs created"] = n
            print()

        keys = sorted(set(touched))
        if keys:
            after = snapshot_state(cur, keys, ca_by_isin)
            print("verification (reconstruction re-run inside the same transaction):")
            print(f"  {'entity':13}{'broker':10}{'symbol':16}{'held':>10}{'ledger':>10}"
                  f"  {'lots':5} {'method':12} {'pnl_ytd':>14}")
            for k in keys:
                a = after.get(k)
                if not a:
                    continue
                ytd = "NULL" if a["ytd"] is None else f"{float(a['ytd']):,.2f}"
                print(f"  {k[0]:13}{k[1]:10}{a['sym'][:16]:16}{a['held']:>10,.0f}"
                      f"{(a['net'] or 0):>10,.0f}  {'OK' if a['ok'] else 'FAIL':5} "
                      f"{a['method']:12} {ytd:>14}")
            unresolved = [k for k in keys if after.get(k) and not after[k]["ok"]]
            print()
            if unresolved:
                print(f"!! {len(unresolved)} holding(s) still do not reconcile: "
                      f"{[after[k]['sym'] for k in unresolved]}")
        else:
            print("nothing to do.")

        print("counts:", counts)
        if args.commit:
            conn.commit()
            ents = sorted({k[0] for k in keys})
            print("\ncommitted.  Now re-run metrics for the affected entities:")
            for e in ents:
                print(f"  python -m workers.equity_txn_metrics_worker --entity '{e}' --commit")
        else:
            conn.rollback()
            print("\ndry-run — rolled back, nothing written.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


if __name__ == "__main__":
    main()
