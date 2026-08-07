#!/usr/bin/env python3
"""Reconcile our FIFO realised gains against imported broker P&L statements.

The broker statement is a per-scrip ORACLE: for a given (entity, broker, FY) it
states the true realised P&L per scrip, computed off the depository's real
corporate-action history. We recompute our own per-scrip FIFO realised (the exact
engine the Realised Gains page uses — report_generator._fetch_realised_gains, which
already applies verified corporate actions) for the same window and diff scrip by
scrip, classifying every divergence so the backfill step
(workers/backfill_from_statements.py) knows what kind of fix, if any, is warranted:

  MATCH          |diff| within tolerance — the statement confirms us.
  ISIN_MIGRATION statement ISIN ≠ ours but the same scrip/economics — a corporate
                 action minted a new ISIN and split our lot pool. (nets ≈ 0)
  CA_COST_DRIFT  same scrip, SELLS match, cost basis differs — a wrong/missing
                 corporate action (e.g. INOXWIND bonus with the wrong ex-date).
  SELL_GAP       proceeds differ — genuinely missing/extra trades. NOT auto-fixed.
  NO_DATA        the statement has a scrip we hold nothing for.

Read-only. Import `reconcile_statement()` from the endpoints (works on a parsed,
uncommitted statement) or run this file to reconcile everything already committed.
"""
from __future__ import annotations

import sys
import os
from datetime import date, datetime
from collections import defaultdict

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.report_generator import _fetch_realised_gains  # noqa: E402
from equity.symbol_bridge import _alnum, _token_match        # noqa: E402


def get_conn():
    """Own connection (RealDictCursor) so this runs under the cron venv without
    importing main — which needs python-multipart the worker venv lacks."""
    from dotenv import load_dotenv
    load_dotenv("/var/www/mis-portal/.env", override=True)
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        port=os.getenv("DB_PORT", "5432"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# Tolerances: a scrip "matches" if realised is within the larger of an absolute
# floor and a small fraction of the statement figure. Sells "match" (→ cost drift,
# not a trade gap) on a looser band because the drift we chase lives in cost basis.
def _pnl_tol(stmt_pnl: float) -> float:
    return max(2_000.0, abs(stmt_pnl) * 0.01)


def _sell_tol(stmt_sell: float) -> float:
    return max(15_000.0, abs(stmt_sell) * 0.01)


def _fy_window(period_from: date, period_to: date) -> tuple[date, date]:
    return period_from, period_to


def _our_realised(conn, entity_id: int, broker: str, lo: date, hi: date):
    """Our per-scrip FIFO realised for (entity, broker) with sale_date in [lo,hi].
    Returns (by_isin, by_name) aggregations of {pnl, sell, names/isins}. Uses the
    same engine + corporate-action application as the live Realised Gains page."""
    rows = _fetch_realised_gains(conn, [entity_id], date.today(),
                                 since_inception=True, by_broker=True)
    # name → isin, so ISIN-carrying statements (Zerodha) match cleanly.
    cur = conn.cursor()
    cur.execute("SELECT security_name, isin FROM security_master WHERE isin IS NOT NULL")
    name2isin = {r["security_name"]: r["isin"] for r in cur.fetchall()}
    cur.close()

    by_isin = defaultdict(lambda: {"pnl": 0.0, "sell": 0.0, "names": set()})
    by_name = defaultdict(lambda: {"pnl": 0.0, "sell": 0.0, "isins": set(), "raw": ""})
    for r in rows:
        if r.get("category") not in ("Equity", "Commodities"):
            continue
        if (r.get("broker") or "") != broker:
            continue
        sd = r.get("sale_date")
        if sd is None or not (lo <= sd <= hi):
            continue
        pnl = r.get("pnl") or 0.0
        sell = r.get("sale_amount") or 0.0
        nm = r["security_name"]
        isin = name2isin.get(nm)
        na = _alnum(nm)
        by_name[na]["pnl"] += pnl
        by_name[na]["sell"] += sell
        by_name[na]["raw"] = nm
        if isin:
            by_name[na]["isins"].add(isin)
            by_isin[isin]["pnl"] += pnl
            by_isin[isin]["sell"] += sell
            by_isin[isin]["names"].add(nm)
    return by_isin, by_name


def reconcile_statement(conn, entity_id: int, parsed_or_lines, *,
                        broker: str, period_from: date, period_to: date,
                        fy_label: str | None = None) -> dict:
    """Diff a statement's EQ lines against our FIFO realised for the same window.

    `parsed_or_lines` may be a full parsed dict (from broker_pnl_statement.parse)
    or a bare list of line dicts. F&O lines are passed through untouched (nothing to
    reconcile against — we have no F&O realised engine)."""
    lines = parsed_or_lines["lines"] if isinstance(parsed_or_lines, dict) else parsed_or_lines
    eq_lines = [l for l in lines if l.get("segment", "EQ") == "EQ"]
    fno_lines = [l for l in lines if l.get("segment") == "FnO"]

    lo, hi = _fy_window(period_from, period_to)
    by_isin, by_name = _our_realised(conn, entity_id, broker, lo, hi)
    matched_isins, matched_names = set(), set()

    scrips = []
    for l in eq_lines:
        s_pnl = float(l.get("realised_pnl") or 0.0)
        s_sell = float(l.get("sell_value") or 0.0)
        s_isin = l.get("isin")
        na = _alnum(l["security_name"])

        our = None
        our_key_isin = None
        # 1) exact ISIN match (Zerodha)
        if s_isin and s_isin in by_isin:
            our = by_isin[s_isin]; our_key_isin = s_isin; matched_isins.add(s_isin)
        # 2) name match (Angel/Dhan have no ISIN; or ISIN migrated)
        if our is None:
            cand = by_name.get(na)
            if cand is None:
                # token/prefix fallback for truncated broker names
                for k, v in by_name.items():
                    if k in matched_names:
                        continue
                    if _token_match(na, k) or _token_match(k, na):
                        cand = v; na = k; break
            if cand is not None:
                our = cand; matched_names.add(na)

        if our is None:
            scrips.append(_row(l, s_pnl, None, "NO_DATA", our_isin=None))
            continue

        o_pnl = our["pnl"]; o_sell = our["sell"]
        diff = o_pnl - s_pnl

        # ISIN migration: statement ISIN differs from the ISIN(s) we booked this scrip
        # under (name matched, ISIN did not) and the P&L is ~equal → same economics,
        # split lot pool.
        our_isins = ({our_key_isin} if our_key_isin else set(
            i for i in (by_name.get(na, {}).get("isins") or set())))
        migrated = bool(s_isin) and our_key_isin is None and our_isins and s_isin not in our_isins

        if abs(diff) <= _pnl_tol(s_pnl):
            status = "MATCH"
        elif migrated and abs(diff) <= _pnl_tol(s_pnl) * 3:
            # statement prints a post-action ISIN but the economics already agree —
            # a cosmetic relabel, nets ~0. Only call it a migration when the P&L
            # matches; a large gap under a different ISIN is a real cost problem and
            # must fall through to the corporate-action search below.
            status = "ISIN_MIGRATION"
        elif abs(o_sell - s_sell) <= _sell_tol(s_sell):
            status = "CA_COST_DRIFT"
        else:
            status = "SELL_GAP"

        scrips.append(_row(l, s_pnl, o_pnl, status,
                           our_isin=(sorted(our_isins)[0] if our_isins else None),
                           our_sell=o_sell))

    # Our scrips in-window that the statement never mentioned (usually the other half
    # of an ISIN migration, or something the broker rolled up differently).
    extra = []
    for isin, v in by_isin.items():
        if isin in matched_isins:
            continue
        # skip if any name of this isin was name-matched
        if any(_alnum(n) in matched_names for n in v["names"]):
            continue
        extra.append({"isin": isin, "security_name": sorted(v["names"])[0] if v["names"] else isin,
                      "our_pnl": round(v["pnl"], 2), "status": "NOT_IN_STMT"})

    stmt_total = round(sum(float(l.get("realised_pnl") or 0) for l in eq_lines), 2)
    our_total = round(sum(s["our_pnl"] for s in scrips if s["our_pnl"] is not None)
                      + sum(e["our_pnl"] for e in extra), 2)

    by_status = defaultdict(lambda: {"n": 0, "stmt_pnl": 0.0, "our_pnl": 0.0, "gap": 0.0})
    for s in scrips:
        b = by_status[s["status"]]
        b["n"] += 1; b["stmt_pnl"] += s["stmt_pnl"]
        b["our_pnl"] += (s["our_pnl"] or 0.0)
        b["gap"] += ((s["our_pnl"] or 0.0) - s["stmt_pnl"])
    for k in by_status:
        for f in ("stmt_pnl", "our_pnl", "gap"):
            by_status[k][f] = round(by_status[k][f], 2)

    return {
        "broker": broker, "fy_label": fy_label,
        "period_from": str(period_from), "period_to": str(period_to),
        "stmt_total": stmt_total, "our_total": our_total,
        "variance": round(our_total - stmt_total, 2),
        "scrips": scrips, "extra": extra,
        "by_status": dict(by_status),
        "fno_lines": [{"security_name": l["security_name"],
                       "realised_pnl": float(l.get("realised_pnl") or 0)} for l in fno_lines],
    }


def _row(l, s_pnl, o_pnl, status, *, our_isin=None, our_sell=None):
    return {
        "security_name": l["security_name"],
        "isin": l.get("isin") or our_isin,
        "our_isin": our_isin,
        "quantity": l.get("quantity"),
        "stmt_sell": l.get("sell_value"),
        "our_sell": round(our_sell, 2) if our_sell is not None else None,
        "stmt_pnl": round(s_pnl, 2),
        "our_pnl": round(o_pnl, 2) if o_pnl is not None else None,
        "gap": round((o_pnl - s_pnl), 2) if o_pnl is not None else None,
        "status": status,
    }


# ---- CLI: reconcile everything already committed for an entity (+optional broker) --
def _load_committed(conn, entity_id=None, broker=None):
    cur = conn.cursor()
    q = "SELECT * FROM broker_pnl_statement WHERE 1=1"
    p = []
    if entity_id is not None:
        q += " AND entity_id=%s"; p.append(entity_id)
    if broker:
        q += " AND broker=%s"; p.append(broker)
    q += " ORDER BY entity_id, broker, period_from"
    cur.execute(q, p)
    stmts = cur.fetchall()
    out = []
    for s in stmts:
        cur.execute("SELECT * FROM broker_pnl_line WHERE statement_id=%s", (s["id"],))
        lines = [dict(r) for r in cur.fetchall()]
        for l in lines:
            for f in ("quantity", "buy_value", "sell_value", "realised_pnl", "st_pnl", "lt_pnl", "return_pct"):
                if l.get(f) is not None:
                    l[f] = float(l[f])
        out.append((dict(s), lines))
    cur.close()
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", type=int, default=None)
    ap.add_argument("--broker", default=None)
    ap.add_argument("--all-scrips", action="store_true", help="list MATCH rows too")
    args = ap.parse_args()

    conn = get_conn()
    try:
        for s, lines in _load_committed(conn, args.entity, args.broker):
            rec = reconcile_statement(
                conn, s["entity_id"], lines, broker=s["broker"],
                period_from=s["period_from"], period_to=s["period_to"],
                fy_label=s["fy_label"])
            print(f"\n=== entity={s['entity_id']} {s['broker']} {s['fy_label']} "
                  f"({s['period_from']}→{s['period_to']}) ===")
            print(f"  stmt_total={rec['stmt_total']:>15,.0f}  our_total={rec['our_total']:>15,.0f}"
                  f"  variance={rec['variance']:>13,.0f}")
            for st, b in sorted(rec["by_status"].items()):
                print(f"    {st:16} n={b['n']:<4} gap={b['gap']:>14,.0f}")
            for sc in sorted(rec["scrips"], key=lambda x: -abs(x["gap"] or 0)):
                if sc["status"] == "MATCH" and not args.all_scrips:
                    continue
                print(f"    [{sc['status']:14}] {sc['security_name'][:22]:22} "
                      f"stmt={sc['stmt_pnl']:>12,.0f} our={(sc['our_pnl'] or 0):>12,.0f} "
                      f"gap={(sc['gap'] or 0):>12,.0f}  isin={sc['isin']}")
            if rec["fno_lines"]:
                fno = sum(x["realised_pnl"] for x in rec["fno_lines"])
                print(f"    F&O (broker-only): {len(rec['fno_lines'])} rows, realised={fno:,.0f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
