#!/usr/bin/env python3
"""
Live trade daemon — sub-second order-fill capture via broker order-update WebSockets.

One process per (entity, broker) account — brokers issue a separate login per entity
(DHR / HHR / SDR are distinct Zerodha accounts), and KiteTicker runs a singleton
Twisted reactor, so a process-per-account model keeps each connection isolated and
avoids event-loop clashes. systemd runs one instance per active account.

On every fill the daemon does two things (Phase 1 = the first; the SSE endpoint in
Phase 2 consumes the second):
  1. INSERT the fill into stock_transaction (source='{broker}', real fill price +
     exchange timestamp), deduped on source_ref '{broker}:live:{order_id}'. The
     post-close broker_txn_sync reconcile remains the source of truth and supersedes
     these live rows (same machinery as the snapshot supersede).
  2. PUBLISH the fill as JSON to the Redis channel 'live_trades' for the live UI.

Run (foreground, for testing):
  python -m workers.live_trade_daemon --broker zerodha --entity DHR [--dry-run]

Order-update events only arrive during market hours and only for real executions, so
outside a session the process simply stays connected and idle.
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

import psycopg2
import psycopg2.extras
import redis

from workers.import_tradebooks_multi import get_or_create_security

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] live_trade: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

LIVE_CHANNEL = "live_trades"


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------
def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_redis():
    return redis.Redis(host="localhost", port=6379, db=0,
                       password=os.getenv("REDIS_PASSWORD", ""), decode_responses=True)


def entity_id_for(conn, entity_code: str) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id FROM entity WHERE entity_name = %s", (entity_code,))
    row = cur.fetchone()
    cur.close()
    if not row:
        raise SystemExit(f"Entity '{entity_code}' not found")
    return row["id"]


def record_fill(ctx, fill: dict):
    """Persist one fill and publish it live. `fill` is the broker-agnostic dict:
    {symbol, isin, side, qty, price, order_id, ts (datetime|None), exchange}.

    Idempotent on source_ref, so a duplicate order-update event (or a reconnect
    replay) is a no-op. `ctx` carries conn / redis / broker / entity ids / flags."""
    order_id = str(fill.get("order_id") or "")
    side     = (fill.get("side") or "").upper()
    qty      = float(fill.get("qty") or 0)
    price    = float(fill.get("price") or 0)
    if side not in ("BUY", "SELL") or qty <= 0 or price <= 0 or not order_id:
        logger.warning(f"skipping incomplete fill: {fill}")
        return

    ts   = fill.get("ts") or datetime.now()
    tdate = ts.date() if isinstance(ts, datetime) else date.today()
    sref = f"{ctx['broker']}:live:{order_id}"

    conn = ctx["conn"]
    cur  = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref = %s", (sref,))
        if cur.fetchone():
            logger.info(f"dup fill {sref} — skip")
            cur.close()
            return
        amount = qty * price
        if not ctx["dry_run"]:
            sec_id = get_or_create_security(cur, fill.get("isin"), fill.get("symbol"),
                                            fill.get("exchange"), "INR", True)
            cur.execute(
                """INSERT INTO stock_transaction
                   (entity_id, security_id, transaction_date, transaction_type, quantity,
                    price, amount, amount_inr, currency, exchange, source, source_ref, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'INR',%s,%s,%s,NOW())""",
                (ctx["entity_id"], sec_id, tdate, side, qty, price, amount, amount,
                 fill.get("exchange"), ctx["broker"], sref),
            )
            conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        logger.error(f"record_fill DB error for {sref}: {e}")
        return

    event = {
        "entity": ctx["entity_code"], "entity_id": ctx["entity_id"], "broker": ctx["broker"],
        "symbol": fill.get("symbol"), "side": side, "qty": qty, "price": price,
        "amount": round(qty * price, 2), "date": str(tdate),
        "ts": ts.isoformat() if isinstance(ts, datetime) else None, "order_id": order_id,
    }
    try:
        ctx["redis"].publish(LIVE_CHANNEL, json.dumps(event))
    except Exception as e:
        logger.error(f"redis publish failed for {sref}: {e}")
    logger.info(f"FILL {ctx['entity_code']}/{ctx['broker']} {side} {fill.get('symbol')} "
                f"{qty} @ {price} (order {order_id})"
                + ("  [dry-run: not written]" if ctx["dry_run"] else "  [recorded + published]"))


# ---------------------------------------------------------------------------
# Zerodha (KiteTicker on_order_update) — fully implemented
# ---------------------------------------------------------------------------
def run_zerodha(ctx):
    from kiteconnect import KiteTicker
    from equity.brokers import zerodha as z

    api_key = z._env(ctx["entity_code"], "API_KEY")
    from equity import tokens as _tok
    access_token = (os.environ.get(f"ZERODHA_{ctx['entity_code']}_ACCESS_TOKEN")
                    or _tok.get(f"zerodha_{ctx['entity_code']}"))
    if not access_token:
        raise SystemExit(f"No Zerodha access token for {ctx['entity_code']}")

    kws = KiteTicker(api_key, access_token)

    def on_order_update(ws, data):
        try:
            # Kite pushes an event per status change; the fill is final on COMPLETE.
            if (data.get("status") or "").upper() != "COMPLETE":
                return
            ts = data.get("exchange_timestamp") or data.get("order_timestamp")
            if isinstance(ts, str):
                try:
                    ts = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    ts = None
            record_fill(ctx, {
                "symbol":   data.get("tradingsymbol"),
                "isin":     None,  # not in the stream; holdings/reconcile backfill it
                "side":     data.get("transaction_type"),
                "qty":      data.get("filled_quantity"),
                "price":    data.get("average_price"),
                "order_id": data.get("order_id"),
                "ts":       ts,
                "exchange": data.get("exchange"),
            })
        except Exception as e:
            logger.error(f"on_order_update error: {e}")

    def on_connect(ws, response):
        logger.info(f"connected: zerodha/{ctx['entity_code']} — listening for order updates")

    def on_close(ws, code, reason):
        logger.warning(f"closed zerodha/{ctx['entity_code']}: {code} {reason}")

    def on_error(ws, code, reason):
        logger.error(f"error zerodha/{ctx['entity_code']}: {code} {reason}")

    kws.on_order_update = on_order_update
    kws.on_connect      = on_connect
    kws.on_close        = on_close
    kws.on_error        = on_error
    # KiteTicker has built-in auto-reconnect; connect() runs the Twisted reactor (blocks).
    kws.connect()


# ---------------------------------------------------------------------------
# Angel One (SmartWebSocketOrderUpdate) — connection wired; needs live-session verify
# ---------------------------------------------------------------------------
def run_angel(ctx):
    from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate
    from equity.brokers import angel_one as a
    from equity import tokens as _tok

    api_key     = a._env(ctx["entity_code"], "API_KEY")
    client_code = a._env(ctx["entity_code"], "CLIENT_ID")
    auth_token  = (_tok.get(f"angel_one_{ctx['entity_code']}")
                   or os.environ.get(f"ANGEL_{ctx['entity_code']}_ACCESS_TOKEN"))
    if not auth_token:
        raise SystemExit(f"No Angel token for {ctx['entity_code']} — run token_refresh_worker")
    # feed_token isn't stored; fetch it from an authed SmartConnect.
    obj = a._smart_client(ctx["entity_code"])
    feed_token = obj.getfeedToken()

    sws = SmartWebSocketOrderUpdate(auth_token, api_key, client_code, feed_token)

    def on_data(wsapp, message):
        try:
            data = json.loads(message) if isinstance(message, str) else message
            payload = data.get("orderData", data) if isinstance(data, dict) else {}
            if (payload.get("orderstatus") or payload.get("status") or "").lower() not in ("complete", "filled"):
                return
            record_fill(ctx, {
                "symbol":   payload.get("tradingsymbol"),
                "isin":     None,
                "side":     payload.get("transactiontype"),
                "qty":      payload.get("filledshares") or payload.get("fillsize"),
                "price":    payload.get("averageprice") or payload.get("fillprice"),
                "order_id": payload.get("orderid"),
                "ts":       None,
                "exchange": payload.get("exchange"),
            })
        except Exception as e:
            logger.error(f"angel on_data error: {e}")

    sws.on_open  = lambda w: logger.info(f"connected: angel_one/{ctx['entity_code']}")
    sws.on_data  = on_data
    sws.on_error = lambda w, e: logger.error(f"angel error: {e}")
    sws.on_close = lambda w, *a_: logger.warning("angel closed")
    sws.connect()


# ---------------------------------------------------------------------------
# Dhan (dhanhq OrderUpdate) — connection wired; needs live-session verify
# ---------------------------------------------------------------------------
def run_dhan(ctx):
    import asyncio
    from dhanhq.orderupdate import OrderUpdate
    from equity.brokers import dhan as d

    client_id, access_token = d._creds(ctx["entity_code"]) if hasattr(d, "_creds") else (
        os.environ.get(f"DHAN_{ctx['entity_code']}_CLIENT_ID"),
        os.environ.get(f"DHAN_{ctx['entity_code']}_ACCESS_TOKEN"),
    )
    if not (client_id and access_token):
        raise SystemExit(f"No Dhan credentials for {ctx['entity_code']}")

    from dhanhq.dhan_context import DhanContext
    ou = OrderUpdate(DhanContext(client_id, access_token))

    async def handler(msg):
        try:
            data = msg.get("Data", msg) if isinstance(msg, dict) else {}
            if (data.get("status") or "").lower() not in ("traded", "complete", "filled", "executed"):
                return
            record_fill(ctx, {
                "symbol":   data.get("tradingSymbol") or data.get("customSymbol"),
                "isin":     data.get("isin"),
                "side":     data.get("transactionType"),
                "qty":      data.get("tradedQuantity") or data.get("filledQty"),
                "price":    data.get("tradedPrice") or data.get("avgPrice"),
                "order_id": data.get("orderId"),
                "ts":       None,
                "exchange": data.get("exchangeSegment"),
            })
        except Exception as e:
            logger.error(f"dhan handler error: {e}")

    ou.on_update = handler          # callback name confirmed against dhanhq at wire-up
    logger.info(f"connecting: dhan/{ctx['entity_code']}")
    asyncio.get_event_loop().run_until_complete(ou.connect_order_update())


RUNNERS = {"zerodha": run_zerodha, "angel_one": run_angel, "dhan": run_dhan}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", required=True, choices=list(RUNNERS))
    ap.add_argument("--entity", required=True, help="entity code, e.g. DHR")
    ap.add_argument("--dry-run", action="store_true", help="log + publish but don't write to DB")
    args = ap.parse_args()

    conn = get_conn()
    ctx = {
        "broker": args.broker, "entity_code": args.entity,
        "entity_id": entity_id_for(conn, args.entity),
        "conn": conn, "redis": get_redis(), "dry_run": args.dry_run,
    }
    logger.info(f"starting live_trade_daemon broker={args.broker} entity={args.entity} "
                f"(id={ctx['entity_id']}) dry_run={args.dry_run}")
    try:
        RUNNERS[args.broker](ctx)
    except KeyboardInterrupt:
        logger.info("stopped")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
