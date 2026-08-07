"""Upsert a parsed broker P&L statement into broker_pnl_statement + broker_pnl_line.

A statement is identified by (entity_id, broker, client_id, period_from, period_to):
re-uploading the same window REPLACES the prior copy (lines are cascade-deleted and
re-inserted) rather than duplicating. Nothing here touches stock_transaction — these
rows are a per-scrip realised oracle, consumed by workers/reconcile_pnl_statements.py.

With commit=False the whole thing rolls back (dry-run) so the /preview endpoint can
show what WOULD be written without persisting.
"""
from __future__ import annotations

import json
from datetime import date


def ingest(conn, entity_id: int, parsed: dict, commit: bool = False) -> dict:
    """Replace this (entity, broker, window) statement with `parsed`.

    Returns {statement_id, broker, client_id, fy_label, period_from, period_to,
             replaced (bool), lines_inserted, segment_totals}."""
    cur = conn.cursor()
    broker = parsed["broker"]
    client_id = parsed.get("client_id")
    pf, pt = parsed.get("period_from"), parsed.get("period_to")
    if pf is None or pt is None:
        raise ValueError("Statement is missing its reporting period — cannot ingest.")

    # Was there already a statement for this exact window?
    cur.execute("""SELECT id FROM broker_pnl_statement
                   WHERE entity_id=%s AND broker=%s
                     AND client_id IS NOT DISTINCT FROM %s
                     AND period_from=%s AND period_to=%s""",
                (entity_id, broker, client_id, pf, pt))
    existing = cur.fetchone()
    replaced = existing is not None

    cur.execute("""
        INSERT INTO broker_pnl_statement
            (entity_id, broker, client_id, period_from, period_to, fy_label,
             segment_totals, stored_path, downloaded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (entity_id, broker, client_id, period_from, period_to)
        DO UPDATE SET fy_label       = EXCLUDED.fy_label,
                      segment_totals = EXCLUDED.segment_totals,
                      stored_path    = COALESCE(EXCLUDED.stored_path, broker_pnl_statement.stored_path),
                      downloaded_at  = EXCLUDED.downloaded_at,
                      created_at     = NOW()
        RETURNING id
    """, (entity_id, broker, client_id, pf, pt, parsed.get("fy_label"),
          json.dumps(parsed.get("segment_totals", {})),
          parsed.get("stored_path"), parsed.get("downloaded_at")))
    statement_id = cur.fetchone()["id"]

    # Snapshot semantics: wipe the old lines, re-insert.
    cur.execute("DELETE FROM broker_pnl_line WHERE statement_id=%s", (statement_id,))
    inserted = 0
    for l in parsed.get("lines", []):
        cur.execute("""
            INSERT INTO broker_pnl_line
                (statement_id, segment, security_name, isin, quantity,
                 buy_value, sell_value, realised_pnl, st_pnl, lt_pnl, return_pct)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (statement_id, l.get("segment", "EQ"), l["security_name"], l.get("isin"),
              l.get("quantity"), l.get("buy_value"), l.get("sell_value"),
              l["realised_pnl"], l.get("st_pnl"), l.get("lt_pnl"), l.get("return_pct")))
        inserted += 1

    summary = {
        "statement_id": statement_id, "broker": broker, "client_id": client_id,
        "fy_label": parsed.get("fy_label"),
        "period_from": str(pf), "period_to": str(pt),
        "replaced": replaced, "lines_inserted": inserted,
        "segment_totals": parsed.get("segment_totals", {}),
    }
    if commit:
        conn.commit()
    else:
        conn.rollback()
    cur.close()
    return summary
