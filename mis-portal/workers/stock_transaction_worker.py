#!/usr/bin/env python3
"""
Stock Transaction Worker — IWS MIS Portal

Pulls executed equity trades from each active broker account (Zerodha / Angel One /
Dhan) into stock_transaction, so equity realised gains can be computed.  Reuses the
broker adapters + credential loading from equity_price_worker, and the security
get-or-create + de-dupe from import_tradebook.

NOTE: Broker trade APIs typically return only the current day's (or recent) trades,
so run this daily after market close; historical backfill uses import_tradebook.py.

  30 16 * * 1-5 /var/www/mis-portal/venv/bin/python -m workers.stock_transaction_worker
"""
import os
import sys
import logging
import hashlib
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Allow running as a bare script (cron_wrapper) as well as `python -m workers.…`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv("/var/www/mis-portal/.env", override=True)

from workers.equity_price_worker import load_credentials, ADAPTER_MAP
from workers.import_tradebook import _get_or_create_security, _parse_date

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def _norm_trades(broker: str, adapter) -> list[dict]:
    """Return broker trades normalised to {symbol, isin, date, side, qty, price, trade_id, exchange}."""
    out = []
    if broker == "zerodha":
        for t in (adapter.kite.trades() or []):
            out.append({
                "symbol":  t.get("tradingsymbol"), "isin": t.get("isin"),
                "date":    _parse_date(t.get("fill_timestamp") or t.get("order_timestamp")),
                "side":    (t.get("transaction_type") or "").upper(),
                "qty":     t.get("quantity"), "price": t.get("average_price") or t.get("price"),
                "trade_id": str(t.get("trade_id") or t.get("order_id") or ""),
                "exchange": t.get("exchange"),
            })
    elif broker == "angel_one":
        resp = adapter.smart.getTradeBook() or {}
        for t in (resp.get("data") or []):
            out.append({
                "symbol":  t.get("tradingsymbol"), "isin": t.get("isin"),
                "date":    _parse_date(t.get("filltime") or t.get("exchtime")),
                "side":    (t.get("transactiontype") or "").upper(),
                "qty":     t.get("fillsize"), "price": t.get("fillprice"),
                "trade_id": str(t.get("tradeid") or t.get("orderid") or ""),
                "exchange": t.get("exchange"),
            })
    elif broker == "dhan":
        resp = adapter.dhan.get_trade_book() or {}
        data = resp.get("data") if isinstance(resp, dict) else resp
        for t in (data or []):
            out.append({
                "symbol":  t.get("tradingSymbol") or t.get("customSymbol"), "isin": t.get("isin"),
                "date":    _parse_date(t.get("exchangeTime") or t.get("createTime")),
                "side":    (t.get("transactionType") or "").upper(),
                "qty":     t.get("tradedQuantity"), "price": t.get("tradedPrice"),
                "trade_id": str(t.get("orderId") or t.get("exchangeOrderId") or ""),
                "exchange": t.get("exchangeSegment"),
            })
    return out


def main():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    # Per-(broker, entity) credential rows.
    cur.execute("""
        SELECT broker, entity_id, credentials, access_token, token_expiry
        FROM broker_api_credentials WHERE is_active = TRUE
    """)
    creds = cur.fetchall()

    total = 0
    for cred in creds:
        broker, entity_id = cred["broker"], cred["entity_id"]
        if broker not in ADAPTER_MAP:
            continue
        try:
            adapter = ADAPTER_MAP[broker](cred)
            trades  = _norm_trades(broker, adapter)
        except Exception as e:
            logger.error("Trade fetch failed for %s/entity %s: %s", broker, entity_id, e)
            continue

        for t in trades:
            side = "BUY" if t["side"].startswith("B") else ("SELL" if t["side"].startswith("S") else None)
            try:
                qty = abs(float(t["qty"])); price = abs(float(t["price"]))
            except (TypeError, ValueError):
                qty = price = None
            if not (t["symbol"] and t["date"] and side and qty and price):
                continue
            ref = f"{broker}:" + (t["trade_id"] or hashlib.md5(
                f"{entity_id}|{t['symbol']}|{t['date']}|{side}|{qty}|{price}".encode()).hexdigest()[:16])
            cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref = %s", (ref,))
            if cur.fetchone():
                continue
            sec_id = _get_or_create_security(cur, (t["isin"] or None), t["symbol"], t["exchange"], dry=False)
            amount = qty * price
            cur.execute("""
                INSERT INTO stock_transaction
                    (entity_id, security_id, transaction_date, transaction_type, quantity, price,
                     amount, amount_inr, currency, exchange, source, source_ref, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'INR',%s,%s,%s,NOW())
            """, (entity_id, sec_id, t["date"], side, qty, price, amount, amount,
                  t["exchange"], broker, ref))
            total += 1
        logger.info("%s/entity %s: processed %d trades", broker, entity_id, len(trades))

    conn.commit()
    cur.close(); conn.close()
    logger.info("Stock transaction worker done; inserted %d new trade(s).", total)


if __name__ == "__main__":
    main()
