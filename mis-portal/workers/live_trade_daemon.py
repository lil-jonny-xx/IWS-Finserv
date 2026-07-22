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
import re
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

# WS-layer failures that mean "this token is dead", as opposed to a transient network
# drop worth retrying. Every broker library auto-reconnects using the token it was
# constructed with, so an invalidated token is retried forever from memory: on
# 2026-07-20 the daily 06:30 IST Zerodha re-login invalidated the running daemons'
# access_token and all five sat in a 403 loop for ~1,000 attempts across the entire
# session. systemd reported the units active, the processes were healthy, the Dhan
# heartbeat ticked — and not one fill could have been captured.
#
# Exiting is the fix: systemd's Restart=always gives us a NEW process, which re-reads
# the rotated token from the store. If the token is genuinely dead the unit burns its
# StartLimitBurst and stops, which check_live_capture() in staleness_monitor reports
# that evening as "launched but the socket never opened". Visibly down beats
# invisibly dead.
#
# Deliberately no bare "token" — it matches benign disconnect text. Keep signatures
# specific enough that a network blip is never treated as fatal.
_AUTH_FATAL_HINTS = ("403", "forbidden", "unauthor", "tokenexception",
                     "invalid access", "invalid api", "api_key", "invalid session")


def _is_auth_fatal(code, reason) -> bool:
    """True when a WS failure means the token is dead rather than the network blipped.

    Split out from the exit path so it can be tested against real log strings without
    killing the process — the false-positive cost is high (a network blip would kill a
    healthy daemon every time it hiccuped).
    """
    text = f"{code} {reason}".lower()
    return any(h in text for h in _AUTH_FATAL_HINTS)


def _exit_if_auth_fatal(ctx, code, reason) -> bool:
    """Kill the process on an auth-class WS failure so systemd relaunches it clean."""
    if not _is_auth_fatal(code, reason):
        return False
    logger.error(
        f"AUTH-FATAL {ctx['entity_code']}/{ctx['broker']}: {code} {reason} — the token this "
        f"process holds has been invalidated (most likely by the daily re-login). Exiting so "
        f"systemd restarts with the current token; reconnecting in-process would retry the "
        f"dead token all session."
    )
    # os._exit, not sys.exit: KiteTicker runs a Twisted reactor and Angel a
    # websocket-client loop, both of which swallow SystemExit inside their callbacks.
    os._exit(75)   # EX_TEMPFAIL


def note_order_event(ctx, status=None, symbol=None):
    """Log every order-update frame the socket delivers, before any fill filtering.

    Without this, 'no fills today' is ambiguous between "no trades were placed" and
    "the socket delivered nothing" — which is exactly why Angel and Dhan could not be
    signed off: both connect cleanly and have never produced a single fill, and the
    log could not tell us which. One greppable line per frame settles it:
        grep 'order-event .*/angel_one' /var/log/mis-portal-live-trade.log
    """
    ctx["events_seen"] = ctx.get("events_seen", 0) + 1
    logger.info(f"order-event {ctx['entity_code']}/{ctx['broker']} "
                f"#{ctx['events_seen']} status={status!r} symbol={symbol!r}")


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------
def get_conn():
    # keepalives so an idle-for-hours daemon connection isn't silently dropped by a
    # NAT/firewall between fills (record_fill also reconnects on a dead connection).
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
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


def apply_fill_to_holding(cur, entity_id: int, broker: str, fill: dict,
                          side: str, qty: float, price: float) -> str:
    """Move `equity_holding` by one live fill, so the portal reflects a trade in
    sub-second rather than waiting on the 60s light-refresh or the daily reconcile.

    Returns a short outcome string for the log.

    A SELL that takes the position to zero DELETEs the row. That is the only path
    that removes a fully-exited holding: a broker feed simply stops listing a closed
    position, and both the light-refresh and the full sync are upsert-only and treat
    an absent symbol as "no update" (never wipe on a blank/flaky read). Without this
    the row would keep its last non-zero quantity and be repriced forever — a sold
    stock still showing market value. Driving it from the fill, not from feed
    absence, keeps that safety property intact: we only zero when the broker told us
    it sold.

    Matched on (entity, broker, ISIN) first, then symbol — same key order the
    light-refresh uses. A BUY with no existing row is left alone: creating one needs
    sector/asset-class classification, which the light-refresh already does within
    60s.
    """
    isin   = (fill.get("isin") or "").strip() or None
    symbol = fill.get("symbol")

    row = None
    if isin:
        cur.execute("SELECT id, quantity, avg_cost FROM equity_holding "
                    "WHERE entity_id=%s AND broker=%s AND isin=%s",
                    (entity_id, broker, isin))
        row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id, quantity, avg_cost FROM equity_holding "
                    "WHERE entity_id=%s AND broker=%s AND symbol=%s",
                    (entity_id, broker, symbol))
        row = cur.fetchone()

    if row is None:
        return "no holding row (light-refresh will create it)"

    hid      = row["id"] if not isinstance(row, tuple) else row[0]
    old_qty  = float((row["quantity"] if not isinstance(row, tuple) else row[1]) or 0)
    old_avg  = float((row["avg_cost"] if not isinstance(row, tuple) else row[2]) or 0)

    if side == "BUY":
        new_qty  = old_qty + qty
        new_cost = old_qty * old_avg + qty * price
        new_avg  = (new_cost / new_qty) if new_qty > 0 else 0.0
        cur.execute(
            """UPDATE equity_holding
                  SET quantity=%s, avg_cost=%s, cost=%s,
                      current_market_value = CASE WHEN current_price IS NOT NULL
                                                  THEN %s * current_price
                                                  ELSE current_market_value END,
                      updated_at=NOW()
                WHERE id=%s""",
            (new_qty, new_avg, new_cost, new_qty, hid),
        )
        return f"qty {old_qty:g} -> {new_qty:g}"

    # SELL — avg cost is unchanged by a sale; only quantity and total cost move.
    new_qty = old_qty - qty
    if new_qty <= 1e-6:
        cur.execute("DELETE FROM equity_holding WHERE id=%s", (hid,))
        return f"qty {old_qty:g} -> 0, position closed (row removed)"

    cur.execute(
        """UPDATE equity_holding
              SET quantity=%s, cost=%s,
                  current_market_value = CASE WHEN current_price IS NOT NULL
                                              THEN %s * current_price
                                              ELSE current_market_value END,
                  updated_at=NOW()
            WHERE id=%s""",
        (new_qty, new_qty * old_avg, new_qty, hid),
    )
    return f"qty {old_qty:g} -> {new_qty:g}"


# NSE series suffix that Angel One appends to the equity tradingsymbol ('AWFIS-EQ').
# ONLY '-EQ' is stripped, and only to LOOK UP an existing security — never to name a new
# one. Other trailing tokens are not safely strippable: '-SM' rows (ORIANA-SM,
# ALPEXSOLAR-SM) exist under that exact name with no bare twin, and '-N'/'-AUTO' are part
# of the real ticker (MCDOWELL-N is United Spirits; BAJAJ-AUTO). Stripping those would
# manufacture the very duplicate this guards against, in reverse.
_EQ_SUFFIX = re.compile(r"-EQ$")


def resolve_isin(cur, entity_id: int, broker: str, symbol: str) -> str | None:
    """Best-effort ISIN for a live fill whose stream didn't carry one.

    Angel One's (and Zerodha's) order-update stream gives a tradingsymbol and no ISIN.
    Identity in security_master is the ISIN, so without it get_or_create_security falls
    to a name match — and Angel's name ('AWFIS-EQ') does not equal the name the
    ISIN-bearing row already carries ('AWFIS'). It therefore CREATED a second security
    for the same instrument, and the same trade got counted twice: once live here, once
    when broker_txn_sync wrote the authoritative fills under the real security. The
    reconcile's supersede could not collapse them because it matches on security_id.

    Holdings first: Angel One's holdings feed carries BOTH the '-EQ' symbol and the
    ISIN, so (entity, broker, symbol) resolves it exactly, with no string surgery.
    Only if that misses (a first-ever buy, before the 60s light-refresh creates the
    row) do we fall back to matching an existing security by the de-suffixed name.
    Returns None when nothing resolves — the caller then behaves as before.
    """
    if not symbol:
        return None
    cur.execute(
        "SELECT isin FROM equity_holding "
        "WHERE entity_id=%s AND broker=%s AND symbol=%s AND isin IS NOT NULL",
        (entity_id, broker, symbol),
    )
    row = cur.fetchone()
    if row:
        return row["isin"] if not isinstance(row, tuple) else row[0]

    base = _EQ_SUFFIX.sub("", symbol)
    if base == symbol:
        return None
    # Adopt the ISIN of an existing security under the bare name. Restricted to rows
    # that HAVE an ISIN: a NULL-isin namesake tells us nothing and matching it would
    # re-run the collision that put us here.
    cur.execute(
        "SELECT isin FROM security_master "
        "WHERE security_name=%s AND security_type='EQUITY' AND isin IS NOT NULL",
        (base,),
    )
    row = cur.fetchone()
    return (row["isin"] if not isinstance(row, tuple) else row[0]) if row else None


def record_fill(ctx, fill: dict):
    """Persist one fill, move the holding, and publish it live. `fill` is the
    broker-agnostic dict:
    {symbol, isin, side, qty, price, order_id, ts (datetime|None), exchange}.

    `isin` may be None — several brokers' order streams omit it — in which case it is
    resolved from our own holdings before the security lookup (see resolve_isin).

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

    def _do_db():
        conn = ctx["conn"]           # re-read each call so a reconnect is picked up
        cur  = conn.cursor()
        try:
            cur.execute("SELECT 1 FROM stock_transaction WHERE source_ref = %s", (sref,))
            if cur.fetchone():
                return "dup"
            amount = qty * price
            if not ctx["dry_run"]:
                # Resolve the ISIN before the lookup: the stream may omit it, and
                # without one the security is matched by name, which forks a second
                # security_master row per instrument and double-counts the trade.
                isin = (fill.get("isin") or "").strip() or None
                if not isin:
                    isin = resolve_isin(cur, ctx["entity_id"], ctx["broker"], fill.get("symbol"))
                sec_id = get_or_create_security(cur, isin, fill.get("symbol"),
                                                fill.get("exchange"), "INR", True)
                cur.execute(
                    """INSERT INTO stock_transaction
                       (entity_id, security_id, transaction_date, transaction_type, quantity,
                        price, amount, amount_inr, currency, exchange, source, source_ref, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'INR',%s,%s,%s,NOW())""",
                    (ctx["entity_id"], sec_id, tdate, side, qty, price, amount, amount,
                     fill.get("exchange"), ctx["broker"], sref),
                )
                # Same transaction as the insert above, so the ledger and the holding
                # can never disagree, and the source_ref dup-check guards it from
                # being applied twice on a reconnect replay.
                holding_outcome = apply_fill_to_holding(
                    cur, ctx["entity_id"], ctx["broker"], fill, side, qty, price)
                conn.commit()
                ctx["_holding_outcome"] = holding_outcome
            return "inserted"
        finally:
            cur.close()

    # A daemon holds ONE connection for hours; without this a single idle drop / DB
    # restart would make every future fill fail silently. Reconnect once and retry on a
    # connection-level error; a real data error still rolls back and logs.
    outcome = None
    for attempt in (1, 2):
        try:
            outcome = _do_db()
            break
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"DB connection lost on {sref} ({e}); reconnecting (attempt {attempt})")
            try:
                ctx["conn"].close()
            except Exception:
                pass
            try:
                ctx["conn"] = get_conn()
            except Exception as ce:
                logger.error(f"record_fill reconnect failed for {sref}: {ce}")
                return
        except Exception as e:
            try:
                ctx["conn"].rollback()
            except Exception:
                pass
            logger.error(f"record_fill DB error for {sref}: {e}")
            return
    if outcome is None:
        logger.error(f"record_fill: gave up persisting {sref} after reconnect")
        return
    if outcome == "dup":
        logger.info(f"dup fill {sref} — skip")
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
    ctx["fills_recorded"] = ctx.get("fills_recorded", 0) + 1
    holding_outcome = ctx.pop("_holding_outcome", None)
    logger.info(f"FILL {ctx['entity_code']}/{ctx['broker']} {side} {fill.get('symbol')} "
                f"{qty} @ {price} (order {order_id})"
                + ("  [dry-run: not written]" if ctx["dry_run"]
                   else f"  [recorded + published; holding: {holding_outcome}]"))


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
            note_order_event(ctx, data.get("status"), data.get("tradingsymbol"))
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
        # Kite surfaces the rejected upgrade on both callbacks; whichever fires first wins.
        _exit_if_auth_fatal(ctx, code, reason)

    def on_error(ws, code, reason):
        logger.error(f"error zerodha/{ctx['entity_code']}: {code} {reason}")
        _exit_if_auth_fatal(ctx, code, reason)

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
    # feed_token MUST come from the same shared session as auth_token — persisted by
    # angel_one.refresh_access_token. A fresh login here would invalidate the token the
    # cash/holdings workers share (the AB1007 freeze); and getfeedToken() on a
    # session-less _smart_client returns None -> 403 handshake. So read the stored feed.
    feed_token  = _tok.get(f"angel_one_{ctx['entity_code']}_feed")
    if not (auth_token and feed_token):
        raise SystemExit(f"No Angel auth/feed token for {ctx['entity_code']} — run the "
                         f"feed-token-persisting refresh_access_token first")

    # Angel's order-update WS requires a Bearer-prefixed JWT in Authorization; the
    # stored token has the prefix stripped (REST uses it raw), so add it back here.
    auth_hdr = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
    sws = SmartWebSocketOrderUpdate(auth_hdr, api_key, client_code, feed_token)
    # The Angel lib's __init__ calls logzero.logfile(logs/<date>/app.log) and, on any REST
    # error, writes the FULL auth JWT + refresh token there — stop persisting secrets to
    # disk (console logging is unaffected).
    try:
        import logzero
        logzero.logfile(None)
    except Exception:
        pass

    def on_data(wsapp, message, *_):
        # websocket-client calls on_data(ws, msg, opcode, cont) — 4 args; accept the extras.
        try:
            # Angel's order-update WS sends a b'\x00' ping every ~10s to keep the socket
            # alive; it's not an order. Skip these (and any empty frame) before logging so
            # the heartbeat doesn't spam the logfile (~17k lines/day) and bury real fills.
            if not message or message in (b"\x00", "\x00"):
                return
            logger.info(f"angel raw order msg {ctx['entity_code']}: {str(message)[:800]}")
            data = json.loads(message) if isinstance(message, str) else message
            payload = data.get("orderData", data) if isinstance(data, dict) else {}
            status = payload.get("orderstatus") or payload.get("status")
            note_order_event(ctx, status, payload.get("tradingsymbol"))
            if (status or "").lower() not in ("complete", "filled"):
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
    def _angel_error(w, e):
        logger.error(f"angel error {ctx['entity_code']}: {e}")
        _exit_if_auth_fatal(ctx, "", e)
    sws.on_error = _angel_error
    sws.on_close = lambda w, *a_: logger.warning("angel closed")
    sws.connect()


# ---------------------------------------------------------------------------
# Dhan (dhanhq OrderUpdate) — verified against a live session 2026-07-22
# ---------------------------------------------------------------------------
_DHAN_SIDE = {"B": "BUY", "S": "SELL", "BUY": "BUY", "SELL": "SELL"}


def _dhan_event(order_update):
    """Normalise one Dhan order-update into (status, symbol, fill|None).

    Dhan wraps the payload in a `Data` envelope and sends **camelCase** keys inside
    it (status / symbol / txnType / tradedQty / avgTradedPrice / orderNo), not the
    PascalCase the docs show. Reading PascalCase made every lookup None, so the
    TRADED test never matched and the socket recorded 0 fills while looking
    perfectly healthy ("events 4, fills 0"). Keys are folded to lowercase and read
    case-blind so either casing works if Dhan ever changes it.

    Only a final 'Traded' yields a fill. The intermediate 'Part-Traded' events carry
    a RUNNING CUMULATIVE tradedQty (37 -> 88 -> 100 of a 100-lot order), so acting on
    the last one alone books the order exactly once at its average price; acting on
    each would triple-count it.

    `exchange` is the venue ('NSE'); `segment` is a one-letter code ('E') and is only
    a fallback — passing the segment as the exchange would mis-tag the security.
    """
    raw = order_update.get("Data", order_update) if isinstance(order_update, dict) else {}
    if not isinstance(raw, dict):
        return None, None, None
    d = {str(k).lower(): v for k, v in raw.items()}
    status, symbol = d.get("status"), d.get("symbol") or d.get("displayname")
    if (status or "").upper() != "TRADED":
        return status, symbol, None
    return status, symbol, {
        "symbol":   symbol,
        "isin":     d.get("isin"),
        "side":     _DHAN_SIDE.get((d.get("txntype") or "").upper()),
        "qty":      d.get("tradedqty"),
        "price":    d.get("avgtradedprice") or d.get("tradedprice"),
        "order_id": d.get("orderno") or d.get("exchorderno"),
        "ts":       None,
        "exchange": d.get("exchange") or d.get("segment"),
    }


def run_dhan(ctx):
    import asyncio
    from dhanhq.orderupdate import OrderUpdate
    from equity.brokers import dhan as d

    # Route through dhan._env so the _ENV_PREFIX ("Rajani Corp" -> RAJANIRCORP) applies —
    # the old raw os.environ fallback missed it. Dhan reads creds from env, not the token store.
    client_id    = d._env(ctx["entity_code"], "CLIENT_ID", required=False)
    access_token = d._env(ctx["entity_code"], "ACCESS_TOKEN", required=False)
    if not (client_id and access_token):
        raise SystemExit(f"No Dhan credentials for {ctx['entity_code']}")

    from dhanhq.dhan_context import DhanContext
    ou = OrderUpdate(DhanContext(client_id, access_token))

    def handler(order_update):   # dhanhq invokes on_update SYNCHRONOUSLY — must NOT be async
        try:
            logger.info(f"dhan raw order msg {ctx['entity_code']}: {str(order_update)[:800]}")
            status, symbol, fill = _dhan_event(order_update)
            note_order_event(ctx, status, symbol)
            if fill is not None:
                record_fill(ctx, fill)
        except Exception as e:
            logger.error(f"dhan handler error: {e}")

    ou.on_update = handler   # sync callback (dhanhq calls it without await)

    # dhanhq's connect_order_update() blocks in its receive loop and exposes no on-open
    # callback, so a silently connected daemon looked identical to a dead one (only the
    # one-shot "connecting:" line, then nothing). Run a periodic liveness tick alongside
    # it: while the WS receive loop is alive the tick keeps firing; when connect returns
    # (socket closed → Restart=always reconnects) we log that too. 5-min cadence so this
    # never becomes log spam the way a per-frame heartbeat would.
    async def _dhan_main():
        started = datetime.now()

        async def _heartbeat():
            while True:
                await asyncio.sleep(300)
                mins = int((datetime.now() - started).total_seconds() // 60)
                logger.info(f"dhan/{ctx['entity_code']} listening — alive {mins}m, "
                            f"events {ctx.get('events_seen', 0)}, "
                            f"fills {ctx.get('fills_recorded', 0)}")

        hb = asyncio.ensure_future(_heartbeat())
        try:
            await ou.connect_order_update()
        finally:
            hb.cancel()
        logger.warning(f"dhan/{ctx['entity_code']} connect_order_update returned — "
                       f"socket closed; systemd will reconnect")

    logger.info(f"connecting: dhan/{ctx['entity_code']}")
    asyncio.run(_dhan_main())


RUNNERS = {"zerodha": run_zerodha, "angel_one": run_angel, "dhan": run_dhan}

# systemd addresses one daemon per account by a filesystem-safe slug (the
# `mis-portal-live-trade@<slug>` template instance), resolved here to (broker, entity)
# so multi-word entity codes like "Rajani Corp" never touch a unit-instance string.
ACCOUNTS = {
    "zerodha-dhr":    ("zerodha",   "DHR"),
    "zerodha-hhr":    ("zerodha",   "HHR"),
    "zerodha-sdr":    ("zerodha",   "SDR"),
    "zerodha-rajani": ("zerodha",   "Rajani Corp"),
    "zerodha-hdr":    ("zerodha",   "HDR"),
    "angel-dhr":      ("angel_one", "DHR"),
    "angel-hhr":      ("angel_one", "HHR"),
    "dhan-hhr":       ("dhan",      "HHR"),
    "dhan-rajani":    ("dhan",      "Rajani Corp"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", choices=list(ACCOUNTS),
                    help="account slug (resolves to broker+entity); used by systemd")
    ap.add_argument("--broker", choices=list(RUNNERS))
    ap.add_argument("--entity", help="entity code, e.g. DHR")
    ap.add_argument("--dry-run", action="store_true", help="log + publish but don't write to DB")
    args = ap.parse_args()

    if args.account:
        args.broker, args.entity = ACCOUNTS[args.account]
    if not (args.broker and args.entity):
        ap.error("provide --account, or both --broker and --entity")

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
        # Printed on every exit, including the stop timer's SIGTERM, so each session
        # leaves a countable verdict per account rather than silence. "events 0" means
        # the socket delivered nothing at all — the distinction that Angel and Dhan
        # could never be signed off without.
        logger.info(f"session-summary {ctx['entity_code']}/{ctx['broker']}: "
                    f"events {ctx.get('events_seen', 0)}, fills recorded "
                    f"{ctx.get('fills_recorded', 0)}")
        conn.close()


if __name__ == "__main__":
    main()
