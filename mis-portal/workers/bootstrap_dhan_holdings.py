#!/usr/bin/env python3
"""
Bootstrap equity holdings from Dhan API for a given entity.

Calls get_holdings(), upserts into equity_holding + security_master.
Safe to re-run — uses ON CONFLICT DO UPDATE.

Usage:
  python workers/bootstrap_dhan_holdings.py --entity "Rajani Corp"
  python workers/bootstrap_dhan_holdings.py --entity HHR --dry-run
"""
import os
import sys
import argparse
import logging
from pathlib import Path
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def _get_or_create_security(cur, isin, symbol, exchange, dry):
    if isin:
        cur.execute("SELECT id FROM security_master WHERE isin = %s", (isin,))
        row = cur.fetchone()
        if row:
            return row["id"]
    cur.execute(
        "SELECT id FROM security_master WHERE security_name = %s AND security_type = 'EQUITY'",
        (symbol,)
    )
    row = cur.fetchone()
    if row:
        return row["id"]
    if dry:
        return f"<new:{symbol or isin}>"
    cur.execute("""
        INSERT INTO security_master (isin, security_name, security_type, asset_class, currency, exchange, created_at)
        VALUES (%s, %s, 'EQUITY', 'EQUITY', 'INR', %s, NOW())
        RETURNING id
    """, (isin or None, symbol, exchange))
    return cur.fetchone()["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", required=True, help="entity_name e.g. 'Rajani Corp' or HHR")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    cur = conn.cursor()

    # Resolve entity
    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (args.entity,))
    ent = cur.fetchone()
    if not ent:
        sys.exit(f"Unknown entity: {args.entity}")
    entity_id = ent["id"]

    # Load Dhan credentials from DB
    cur.execute("""
        SELECT credentials, access_token FROM broker_api_credentials
        WHERE broker = 'dhan' AND entity_id = %s AND is_active = TRUE
    """, (entity_id,))
    cred_row = cur.fetchone()
    if not cred_row:
        sys.exit(f"No active Dhan credentials in DB for entity {args.entity} (id={entity_id})")

    client_id    = cred_row["credentials"]["client_id"]
    access_token = cred_row["access_token"]
    if not access_token:
        sys.exit("Dhan access_token is empty — bootstrap the token first via web.dhan.co")

    # Connect to Dhan and fetch holdings
    from dhanhq import dhanhq
    from dhanhq.dhan_context import DhanContext
    dhan = dhanhq(DhanContext(client_id, access_token))

    logger.info("Fetching holdings from Dhan for %s (entity_id=%d)...", args.entity, entity_id)
    resp = dhan.get_holdings()
    if not resp or resp.get("status") != "success":
        sys.exit(f"Dhan get_holdings() failed: {resp}")

    holdings = resp.get("data") or []
    logger.info("Dhan returned %d holdings", len(holdings))

    if args.dry_run:
        for h in holdings:
            print(h)
        conn.close()
        return

    upserted = 0
    for h in holdings:
        symbol   = h.get("tradingSymbol") or h.get("customSymbol") or ""
        isin     = h.get("isin") or None
        exchange = (h.get("exchange") or "NSE").upper()
        qty      = Decimal(str(h.get("totalQty") or 0))
        avg_cost = Decimal(str(h.get("avgCostPrice") or 0))
        cost     = Decimal(str(h.get("costValue") or 0)) or (qty * avg_cost)
        ltp      = Decimal(str(h.get("lastTradedPrice") or 0))
        cmv      = Decimal(str(h.get("currentMarketValue") or 0)) or (qty * ltp)

        if qty <= 0:
            logger.info("Skipping %s — zero quantity", symbol)
            continue

        _get_or_create_security(cur, isin, symbol, exchange, dry=False)

        cur.execute("""
            INSERT INTO equity_holding
                (entity_id, broker, symbol, isin, exchange,
                 quantity, avg_cost, cost,
                 current_price, current_market_value,
                 updated_at)
            VALUES (%s, 'dhan', %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, broker, symbol) DO UPDATE SET
                isin                  = EXCLUDED.isin,
                exchange              = EXCLUDED.exchange,
                quantity              = EXCLUDED.quantity,
                avg_cost              = EXCLUDED.avg_cost,
                cost                  = EXCLUDED.cost,
                current_price         = EXCLUDED.current_price,
                current_market_value  = EXCLUDED.current_market_value,
                updated_at            = NOW()
        """, (entity_id, symbol, isin, exchange, qty, avg_cost, cost, ltp, cmv))

        logger.info("  %-20s  qty=%-8s  avg=%-10s  cmv=%s", symbol, qty, avg_cost, cmv)
        upserted += 1

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Done — upserted %d holdings for %s.", upserted, args.entity)


if __name__ == "__main__":
    main()
