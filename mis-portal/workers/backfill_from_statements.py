#!/usr/bin/env python3
"""Authoritative corporate-action backfill, driven by broker P&L statements + yfinance.

The reconciliation step (workers/reconcile_pnl_statements.py) flags scrips whose
realised P&L disagrees with the broker statement. The dominant cause is a wrong or
missing corporate action — most often a bonus/split recorded with the WRONG ex-date,
so our FIFO scales the wrong lots (the INOXWIND case: bonus 4:1 recorded on
2024-05-17 instead of 2024-05-24, turning an ₹858k gain into a ₹143k loss).

This worker fixes those, using the statement as an ORACLE and yfinance as the source
of candidate ratios/dates:

  For each CA_COST_DRIFT security, it gathers yfinance split/bonus events in the
  trade window, enumerates candidate corporate-action configurations (yfinance often
  double-lists one event on two nearby dates — we try each), recomputes the security's
  realised per FY under each candidate, and picks the configuration that best matches
  the broker statement across EVERY FY we have a statement for. A change is applied
  ONLY if it (a) is backed by a yfinance event and (b) moves realised toward the
  statement within tolerance across those FYs. Otherwise the scrip is left flagged.

It NEVER fabricates trades: it only rewrites `corporate_action` (ex_date/ratio),
which is re-derivable and reversible, and which the split-aware FIFO consumes. ISIN
migrations and genuine SELL_GAPs are reported for review, not auto-plugged.

Dry-run by default; pass --commit to persist. Run under the worker venv:
  /var/www/.venv/bin/python -m workers.backfill_from_statements --entity 10 --broker zerodha
"""
from __future__ import annotations

import sys
import os
from datetime import date, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.reconcile_pnl_statements import get_conn, reconcile_statement, _load_committed  # noqa: E402
from workers.report_generator import _fifo_realised_grouped                                  # noqa: E402
from workers.corporate_actions import yahoo_splits                                            # noqa: E402


TOL = 3_000.0          # per-FY match tolerance (rupees)
GROUP_DAYS = 10        # yfinance events this close with the same ratio = one real event
CANON_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0)   # standard bonus/split magnitudes


def _ratio_candidates(r: float):
    """Canonical true magnitudes to try at a yfinance-CONFIRMED event date. yfinance
    reports the split factor but can miss a SIMULTANEOUS bonus (e.g. Bajaj Finserv's
    5:1 split + 1:1 bonus = 10x, but yfinance shows only 5.0), so we also try the
    yfinance ratio times a small bonus and the standard magnitudes. The statement
    oracle + tight tolerance rejects any wrong pick — we are correcting the magnitude
    of a confirmed event, never inventing one."""
    cands = {round(r, 4)}
    for k in (2, 3, 4):
        cands.add(round(r * k, 4))
    cands.update(CANON_RATIOS)
    return sorted(x for x in cands if 1.5 <= x <= 40)


def _fy_label(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{str(y)[2:]}-{str(y + 1)[2:]}"


def _seq_for_security(cur, entity_id: int, security_id: int):
    cur.execute("""SELECT transaction_date d, transaction_type tt, quantity q, price p
                   FROM stock_transaction
                   WHERE entity_id=%s AND security_id=%s
                   ORDER BY transaction_date, id""", (entity_id, security_id))
    seq = []
    for r in cur.fetchall():
        tt = (r["tt"] or "").upper()
        kind = "buy" if tt.startswith(("B", "P")) else ("sell" if tt.startswith(("S",)) else None)
        if not kind:
            continue
        seq.append({"date": r["d"], "kind": kind, "units": float(r["q"] or 0),
                    "price": float(r["p"] or 0), "name": "X", "group": "Equity", "sec": security_id})
    return seq


def _realised_by_fy(seq, actions_for_sec) -> dict:
    """{fy_label: realised} for one security under a candidate CA config."""
    out = _fifo_realised_grouped(seq, date(1900, 1, 1),
                                 {seq[0]["sec"]: actions_for_sec} if seq else {})
    by = defaultdict(float)
    for o in out:
        if o["pnl"] is None:
            continue
        by[_fy_label(o["sale_date"])] += o["pnl"]
    return dict(by)


def _candidate_configs(recorded, yf_events):
    """Enumerate candidate CA configs (each a list of (ex_date, ratio)).

    yfinance frequently reports one event on two adjacent dates with the same ratio;
    we group those and try each date as the single true ex-date. Distinct ratios / far
    apart dates are treated as separate real events and kept together."""
    # group yfinance events
    groups = []
    for d, r in sorted(yf_events):
        placed = False
        for g in groups:
            if abs(round(g[0][1] - r, 4)) < 1e-6 and abs((d - g[-1][0]).days) <= GROUP_DAYS:
                g.append((d, r)); placed = True; break
        if not placed:
            groups.append([(d, r)])

    # "No corporate action" is a candidate ONLY when yfinance confirms no event.
    # yfinance is the authority on whether an event EXISTS; the statement oracle only
    # calibrates its date/magnitude. Never drop a confirmed event just because doing so
    # happens to fit one entity's numbers (that would corrupt other holders — e.g.
    # removing the real HDFCAMC 2:1 bonus to fit DHR would break HHR).
    configs = {}
    if not yf_events:
        configs[("none",)] = []
    if recorded:
        configs[tuple(sorted((str(d), r) for d, r in recorded))] = list(recorded)

    # cartesian: one representative date per group, over canonical magnitudes of the
    # group's yfinance ratio. Capped to keep the search bounded for the rare
    # multi-event security.
    import itertools
    if groups and len(groups) <= 3:
        per_group = []
        for g in groups:
            dates = sorted({d for d, _ in g})
            ratio = g[0][1]
            per_group.append([(d, r) for d in dates for r in _ratio_candidates(ratio)])
        for combo in itertools.product(*per_group):
            cfg = sorted(combo)
            configs[tuple((str(d), r) for d, r in cfg)] = list(cfg)
    return configs


def backfill(conn, entity_id=None, broker=None, commit=False) -> dict:
    cur = conn.cursor()

    # 1) reconcile everything committed → collect CA_COST_DRIFT targets keyed by
    #    SECURITY (a corporate action is a security-level fact shared by every holder),
    #    recording each (entity, fy) statement figure so a candidate CA is scored
    #    against ALL holders at once, never overfitted to one.
    targets = defaultdict(dict)          # security_id -> {(entity_id, fy_label): stmt_pnl}
    sec_name = {}                        # security_id -> security_name
    isin_migrations, sell_gaps = [], []

    for s, lines in _load_committed(conn, entity_id, broker):
        rec = reconcile_statement(conn, s["entity_id"], lines, broker=s["broker"],
                                  period_from=s["period_from"], period_to=s["period_to"],
                                  fy_label=s["fy_label"])
        for sc in rec["scrips"]:
            isin = sc.get("our_isin") or sc.get("isin")
            if sc["status"] == "CA_COST_DRIFT" and isin and s["fy_label"]:
                cur.execute("SELECT id, security_name FROM security_master WHERE isin=%s", (isin,))
                row = cur.fetchone()
                if not row:
                    continue
                sec_name[row["id"]] = row["security_name"]
                targets[row["id"]][(s["entity_id"], s["fy_label"])] = sc["stmt_pnl"]
            elif sc["status"] == "ISIN_MIGRATION":
                isin_migrations.append((s["entity_id"], s["broker"], sc))
            elif sc["status"] == "SELL_GAP":
                sell_gaps.append((s["entity_id"], s["broker"], sc))

    # 2) for each drifted security, search yfinance-backed CA configs against the
    #    oracle summed over EVERY (entity, fy) that has a statement for it.
    proposals = []
    for sid, tgt in sorted(targets.items()):
        name = sec_name[sid]
        entities = sorted({e for (e, _fy) in tgt})
        seqs = {e: _seq_for_security(cur, e, sid) for e in entities}
        seqs = {e: sq for e, sq in seqs.items() if sq}
        if not seqs:
            continue
        all_trades = [t for sq in seqs.values() for t in sq]
        lo = min(t["date"] for t in all_trades) - timedelta(days=7)
        hi = max(t["date"] for t in all_trades)
        yf_events = yahoo_splits(name, lo, hi) or []

        cur.execute("SELECT ex_date, ratio, action_type FROM corporate_action WHERE security_id=%s", (sid,))
        rec_rows = cur.fetchall()
        recorded = [(r["ex_date"], float(r["ratio"])) for r in rec_rows]
        act_type = rec_rows[0]["action_type"] if rec_rows else None

        rec_byfy = {e: _realised_by_fy(seqs[e], recorded) for e in seqs}

        def evaluate(cfg):
            """A corporate-action change only moves realised in the FYs whose open lots
            span the ex-date; discrepancies in other FYs come from other causes and must
            not gate this fix. So judge a candidate on the (entity,fy) it actually
            CHANGES: those must now match their statement target, and nothing anywhere
            may move further from target. Returns (affected, ok, total_err)."""
            byfy = {e: _realised_by_fy(seqs[e], cfg) for e in seqs}
            affected, ok, total = [], True, 0.0
            for (e, fy), t in tgt.items():
                if e not in seqs:
                    continue
                rv = rec_byfy[e].get(fy, 0.0)
                bv = byfy[e].get(fy, 0.0)
                total += abs(bv - t)
                if abs(bv - rv) > 1.0:                    # the CA touched this (entity,fy)
                    affected.append((e, fy))
                    if abs(bv - t) > TOL:                 # …but it still doesn't match
                        ok = False
                if abs(bv - t) > abs(rv - t) + TOL:       # moved AWAY from the oracle
                    ok = False
            return affected, ok, total

        rec_key = tuple(sorted((str(d), r) for d, r in recorded))
        _, _, recorded_err = evaluate(recorded)
        best_key = best_cfg = None
        best_affected, best_err = None, None
        for key, cfg in _candidate_configs(recorded, yf_events).items():
            if key == rec_key:
                continue
            affected, ok, total = evaluate(cfg)
            if not affected or not ok:
                continue
            if best_err is None or total < best_err:
                best_key, best_cfg, best_affected, best_err = key, cfg, affected, total

        if best_cfg is not None and best_err + 1 < recorded_err:
            proposals.append({
                "entity_id": entities[0] if len(entities) == 1 else entities,
                "security_id": sid, "name": name,
                "action_type": act_type or ("bonus" if best_cfg and best_cfg[0][1] >= 1.5 else "split"),
                "from": [(str(d), r) for d, r in recorded],
                "to": [(str(d), r) for d, r in best_cfg],
                "fys": {f"{e}:{fy}": t for (e, fy), t in tgt.items()
                        if (e, fy) in best_affected},
                "recorded_err": round(recorded_err, 2), "new_err": round(best_err, 2),
                "yf_events": [(str(d), r) for d, r in yf_events],
            })

    # 3) report + (optionally) apply
    print(f"\n{'='*70}\nCA backfill — entity={entity_id} broker={broker}  commit={commit}")
    print(f"CA_COST_DRIFT securities examined: {len(targets)}   proposals: {len(proposals)}")
    for p in proposals:
        print(f"\n  {p['name']} (sec {p['security_id']}, entity {p['entity_id']}, {p['action_type']})")
        print(f"    yfinance events : {p['yf_events']}")
        print(f"    recorded CA     : {p['from']}   (err {p['recorded_err']:,.0f})")
        print(f"    → corrected CA  : {p['to']}   (err {p['new_err']:,.0f})")
        for fy, t in sorted(p["fys"].items()):
            print(f"        {fy} (entity:fy): statement target {t:,.0f}")

    if isin_migrations:
        print(f"\n  ISIN_MIGRATION flagged (review, not auto-merged): {len(isin_migrations)}")
        for eid, bk, sc in isin_migrations[:20]:
            print(f"    {sc['security_name'][:20]:20} stmt_isin={sc['isin']} our_isin={sc['our_isin']} "
                  f"gap={sc['gap']:,.0f}")
    if sell_gaps:
        print(f"\n  SELL_GAP flagged (missing/extra trades — NOT auto-fixed): {len(sell_gaps)}")
        for eid, bk, sc in sorted(sell_gaps, key=lambda x: -abs(x[2]['gap'] or 0))[:20]:
            print(f"    {sc['security_name'][:20]:20} isin={sc['isin']} gap={(sc['gap'] or 0):,.0f}")

    if commit and proposals:
        for p in proposals:
            sid = p["security_id"]
            # replace this security's CA rows with the corrected config
            cur.execute("DELETE FROM corporate_action WHERE security_id=%s", (sid,))
            for ds, r in p["to"]:
                cur.execute("""
                    INSERT INTO corporate_action
                        (security_id, action_type, ex_date, ratio, source, verified, evidence, created_at)
                    VALUES (%s,%s,%s,%s,'statement_reconciled',TRUE,%s,NOW())
                    ON CONFLICT (security_id, ex_date, action_type) DO UPDATE
                        SET ratio=EXCLUDED.ratio, source=EXCLUDED.source,
                            verified=TRUE, evidence=EXCLUDED.evidence
                """, (sid, p["action_type"], ds, r,
                      f"reconciled vs broker statement; yfinance {p['yf_events']}"))
        conn.commit()
        print(f"\n  COMMITTED {len(proposals)} corporate-action correction(s).")
    else:
        conn.rollback()
        if proposals:
            print("\n  (dry-run — re-run with --commit to apply)")

    cur.close()
    return {"examined": len(targets), "proposals": proposals,
            "isin_migrations": len(isin_migrations), "sell_gaps": len(sell_gaps)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", type=int, default=None)
    ap.add_argument("--broker", default=None)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = get_conn()
    try:
        backfill(conn, args.entity, args.broker, commit=args.commit)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
