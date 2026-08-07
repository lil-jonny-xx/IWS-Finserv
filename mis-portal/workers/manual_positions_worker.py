#!/usr/bin/env python3
"""
Materialise manual (non-API-broker) equity positions into equity_holding.

Brokers like SBI Securities / HDFC Securities / ICICI Direct have NO live holdings
feed, so a stock held there is entered by hand in the trade register
(stock_transaction, source='manual', broker=<that broker>). Until now those rows
only fed the realised-gains FIFO — the OPEN position was invisible everywhere
(no equity_holding row, so no live price, no metrics, absent from every total).

This worker closes that gap. For each (entity, non-API broker, ISIN) it FIFO-reduces
the manual trades to the CURRENT open lots (reusing the exact same fifo_lots /
corporate-action machinery the metrics + realised-gains engines use, so the three
cannot drift), then:

  * net qty > 0  -> upsert an equity_holding row (qty / avg_cost / cost / first-buy
                    date). Price is left to equity_price_worker, which prices these
                    rows live via Zerodha Kite's instrument-quote API; the metrics
                    worker fills XIRR/CAGR/YTD from the same manual trades.
  * net qty == 0 -> the position is closed; prune any open row (realised gains are
                    untouched — they live in stock_transaction, not here).

Why this is duplication-safe: the row is keyed on the NON-API broker, disjoint from
the API-fed brokers (zerodha/angel_one/dhan). The same stock held at Zerodha AND at
SBI Securities is two rows under two broker keys — two real demats — never one
position counted twice. equity_sync_worker's feed-absence prune only ever runs for
the API broker it is syncing, so it never touches these rows; this worker is their
sole owner.

Dry-run by default; pass --commit to write. --entity to scope to one entity.

    cd /var/www/mis-portal && .venv/bin/python workers/manual_positions_worker.py --commit
"""
import os
import sys
import argparse
import logging
from datetime import date

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

sys.path.insert(0, "/var/www/mis-portal")
load_dotenv("/var/www/mis-portal/.env", override=True)

from workers.equity_txn_metrics_worker import fifo_lots
from workers.corporate_actions import load_actions_by_isin

# Brokers WITH a live API feed — their equity_holding rows are owned by
# equity_sync_worker / equity_price_worker, so a manual position is never
# materialised for them (it would double-count the fed row; a manual trade there
# only adjusts that row's FIFO). Everything else is treated as a non-API demat.
# Keep in sync with main.py API_FED_BROKERS.
API_FED_BROKERS = {"zerodha", "angel_one", "dhan"}

# Below this, a net position is treated as fully closed (guards float drift on
# fractional-share arithmetic).
QTY_EPS = 1e-4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("manual_positions")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def load_manual_positions(cur, entity_id=None):
    """Group manual trades at non-API brokers into per-position trade lists.

    Returns {(entity_id, broker, isin): {"symbol", "exchange", "txns": [...]}}, with
    txns ordered date-then-id and shaped for fifo_lots ({d, side, q, p}).
    """
    where = ["st.source = 'manual'", "st.broker <> ALL(%s)"]
    params = [list(API_FED_BROKERS)]
    if entity_id:
        where.append("st.entity_id = %s")
        params.append(entity_id)
    cur.execute(f"""
        SELECT st.entity_id, st.broker, sm.isin,
               sm.security_name AS symbol,
               COALESCE(sm.exchange, 'NSE') AS exchange,
               st.transaction_date AS d, st.transaction_type AS side,
               st.quantity AS q, st.price AS p
          FROM stock_transaction st
          JOIN security_master sm ON sm.id = st.security_id
         WHERE {' AND '.join(where)}
           AND sm.isin IS NOT NULL
         ORDER BY st.entity_id, st.broker, sm.isin, st.transaction_date, st.id
    """, params)

    groups = {}
    for r in cur.fetchall():
        key = (r["entity_id"], r["broker"], r["isin"])
        g = groups.setdefault(key, {"symbol": r["symbol"],
                                    "exchange": r["exchange"], "txns": []})
        g["txns"].append({"d": r["d"], "side": r["side"], "q": r["q"], "p": r["p"]})
    return groups


def upsert_position(cur, entity_id, broker, isin, symbol, exchange,
                    qty, avg_cost, cost, first_dt):
    """Insert or refresh a manual-broker equity_holding row.

    On a NEW row, current_price/current_market_value are seeded at cost (break-even)
    so the position is never NULL in a total before its first Kite tick. On an
    EXISTING row we keep the live price the price worker set and only re-derive
    market value from the (possibly changed) quantity — we must not stamp cost back
    over a real LTP, nor fake as_of_date freshness (the price worker owns staleness).
    """
    cur.execute(
        """
        INSERT INTO equity_holding
            (entity_id, broker, symbol, isin, exchange, asset_class,
             quantity, avg_cost, cost, current_price, current_market_value,
             currency, fx_rate, as_of_date, first_invested_date, updated_at)
        VALUES (%s,%s,%s,%s,%s,'equity',
                %s,%s,%s,%s,%s,
                'INR',1,%s,%s,NOW())
        ON CONFLICT (entity_id, broker, isin) DO UPDATE SET
            symbol               = EXCLUDED.symbol,
            quantity             = EXCLUDED.quantity,
            avg_cost             = EXCLUDED.avg_cost,
            cost                 = EXCLUDED.cost,
            first_invested_date  = LEAST(equity_holding.first_invested_date,
                                         EXCLUDED.first_invested_date),
            current_market_value = EXCLUDED.quantity
                                   * COALESCE(equity_holding.current_price, EXCLUDED.avg_cost),
            updated_at           = NOW()
        """,
        (entity_id, broker, symbol, isin, exchange,
         qty, avg_cost, cost, avg_cost, cost,
         date.today(), first_dt),
    )


def run(commit=False, entity_id=None):
    conn = get_db()
    cur = conn.cursor()
    ca_by_isin = load_actions_by_isin(cur)

    groups = load_manual_positions(cur, entity_id)
    open_keys = set()      # (entity_id, broker, isin) that should have a live row
    upserts = 0

    for (eid, broker, isin), g in groups.items():
        lots = fifo_lots(g["txns"], ca_by_isin.get(isin))
        net_qty = sum(q for _, q, _ in lots)
        if net_qty <= QTY_EPS:
            continue                       # fully sold -> no open row (pruned below)
        cost = sum(q * p for _, q, p in lots)
        avg_cost = cost / net_qty
        first_dt = min(d for d, _, _ in lots)
        open_keys.add((eid, broker, isin))
        upserts += 1
        logger.info("  %s / %s / %s  qty=%.4f avg=%.2f cost=%.2f since=%s",
                    eid, broker, g["symbol"], net_qty, avg_cost, cost, first_dt)
        if commit:
            upsert_position(cur, eid, broker, isin, g["symbol"], g["exchange"],
                            round(net_qty, 4), round(avg_cost, 4), round(cost, 2), first_dt)

    # Prune manual-broker rows that no longer correspond to an open position — a fully
    # sold stock, or one whose manual trades were all deleted. Scoped to non-API
    # brokers only, so an API-fed holding can never be caught by this.
    cur.execute(
        "SELECT id, entity_id, broker, isin, symbol FROM equity_holding "
        "WHERE broker <> ALL(%s)" + (" AND entity_id = %s" if entity_id else ""),
        ([list(API_FED_BROKERS)] + ([entity_id] if entity_id else [])),
    )
    stale = [r for r in cur.fetchall()
             if (r["entity_id"], r["broker"], r["isin"]) not in open_keys]
    for r in stale:
        logger.info("  prune closed manual position: %s / %s / %s",
                    r["entity_id"], r["broker"], r["symbol"])
    if commit and stale:
        cur.execute("DELETE FROM equity_holding WHERE id = ANY(%s)", ([r["id"] for r in stale],))

    if commit:
        conn.commit()
    logger.info("%s manual position(s) open, %d pruned. %s",
                upserts, len(stale), "committed." if commit else "DRY-RUN (use --commit).")
    cur.close()
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Materialise manual non-API-broker equity positions.")
    ap.add_argument("--commit", action="store_true", help="write to equity_holding (default: dry-run)")
    ap.add_argument("--entity", type=int, default=None, help="scope to one entity id")
    args = ap.parse_args()
    run(commit=args.commit, entity_id=args.entity)
