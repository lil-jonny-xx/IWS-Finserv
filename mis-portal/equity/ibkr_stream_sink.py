"""
Persistence sink for the IBKR real-time streaming daemon (workers/ibkr_stream_daemon.py).

Keeps writes NARROW so the live stream never fights the two other writers of
foreign_equity_holding (keyed on (entity_id, broker='ibkr', symbol)):
  • the daily Flex sync owns quantity / cost / isin / first_invested_date (authoritative)
  • foreign_price_worker owns current_price* / current_market_value* / pnl_inception

The stream therefore does PARTIAL updates only:
  • upsert_position  -> quantity, avg_cost*, cost*, currency, exchange  (the "book")
  • update_quote     -> current_price*, current_market_value*, pnl_inception  (live price)
  • publish_fill     -> SSE only (the durable trade LEDGER stays with the daily Flex Trades
                        reconcile; positionEvent already moves the holding on a fill)

All functions are fail-soft: a DB/redis error is logged, never raised into the asyncio
event loop. Currency->INR uses equity.fx.get_rate (same source as the price worker).
"""
import json
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from equity import fx

logger = logging.getLogger("ibkr_stream.sink")

BROKER = "ibkr"
LIVE_CHANNEL = "live_trades"          # same channel main.py's /api/v1/live/trades relays
TWO = Decimal("0.01")
FOUR = Decimal("0.0001")

# IB execution side codes -> our transaction_type
_SIDE = {"BOT": "BUY", "SLD": "SELL", "BUY": "BUY", "SELL": "SELL"}

_fx_cache: dict[str, Decimal] = {}


def _fx_rate(conn, ccy: str) -> Decimal:
    ccy = (ccy or "USD").upper()
    if ccy == "INR":
        return Decimal("1")
    if ccy not in _fx_cache:
        r = fx.get_rate(conn, ccy, date.today())
        _fx_cache[ccy] = r if r is not None else Decimal("1")
    return _fx_cache[ccy]


def _sym(contract) -> str:
    return (contract.localSymbol or contract.symbol or "").strip()


def upsert_position(conn, entity_id: int, contract, qty, avg_cost_native) -> None:
    """Live 'book': set quantity + cost basis for a held name. qty==0 -> position
    closed, remove the row (the daily sync prunes sold holdings the same way)."""
    sym = _sym(contract)
    if not sym:
        return
    try:
        cur = conn.cursor()
        if not qty:
            cur.execute("DELETE FROM foreign_equity_holding "
                        "WHERE entity_id=%s AND broker=%s AND symbol=%s",
                        (entity_id, BROKER, sym))
            conn.commit(); cur.close()
            return
        ccy = (getattr(contract, "currency", None) or "USD").upper()
        fxr = _fx_rate(conn, ccy)
        acn = Decimal(str(avg_cost_native or 0))
        q = Decimal(str(qty))
        cost_native = (q * acn).quantize(TWO, ROUND_HALF_UP)
        avg_cost = (acn * fxr).quantize(FOUR, ROUND_HALF_UP)
        cost = (cost_native * fxr).quantize(TWO, ROUND_HALF_UP)
        exch = getattr(contract, "primaryExchange", None) or getattr(contract, "exchange", None)
        cur.execute("""
            INSERT INTO foreign_equity_holding
                (entity_id, broker, symbol, exchange, quantity,
                 avg_cost, cost, avg_cost_native, cost_native, currency, fx_rate,
                 as_of_date, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (entity_id, broker, symbol) DO UPDATE SET
                quantity        = EXCLUDED.quantity,
                avg_cost        = EXCLUDED.avg_cost,
                cost            = EXCLUDED.cost,
                avg_cost_native = EXCLUDED.avg_cost_native,
                cost_native     = EXCLUDED.cost_native,
                currency        = EXCLUDED.currency,
                exchange        = COALESCE(EXCLUDED.exchange, foreign_equity_holding.exchange),
                fx_rate         = EXCLUDED.fx_rate,
                updated_at      = NOW()
        """, (entity_id, BROKER, sym, exch, float(q),
              avg_cost, cost, acn, cost_native, ccy, fxr, date.today()))
        conn.commit(); cur.close()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.error("upsert_position failed (%s/%s): %s", entity_id, sym, e)


def update_quote(conn, contract, price_native) -> None:
    """Live price: refresh price/value/pnl for every ibkr row of this symbol (the native
    price is entity-independent; per-row qty/cost do the rest via SQL arithmetic)."""
    sym = _sym(contract)
    if not sym or price_native in (None, 0):
        return
    try:
        ccy = (getattr(contract, "currency", None) or "USD").upper()
        fxr = float(_fx_rate(conn, ccy))
        pn = float(price_native)
        cur = conn.cursor()
        cur.execute("""
            UPDATE foreign_equity_holding SET
                current_price_native        = %(pn)s,
                current_price               = %(pn)s * %(fx)s,
                current_market_value_native = quantity * %(pn)s,
                current_market_value        = quantity * %(pn)s * %(fx)s,
                pnl_inception               = quantity * %(pn)s * %(fx)s - COALESCE(cost,0),
                fx_rate                     = %(fx)s,
                updated_at                  = NOW()
            WHERE broker=%(b)s AND symbol=%(s)s
        """, {"pn": pn, "fx": fxr, "b": BROKER, "s": sym})
        conn.commit(); cur.close()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.error("update_quote failed (%s): %s", sym, e)


def publish_fill(redis_client, entity_code: str, entity_id: int, contract,
                 side: str, qty, price, exec_id: str, ts) -> None:
    """Push a fill to the live SSE channel in the exact shape main.py relays. No durable
    ledger write here — positionEvent updates the holding and the daily Flex Trades
    reconcile is the authoritative transaction record (Tier 2)."""
    if redis_client is None:
        return
    side = _SIDE.get((side or "").upper(), (side or "").upper())
    try:
        redis_client.publish(LIVE_CHANNEL, json.dumps({
            "entity": entity_code, "entity_id": entity_id, "broker": BROKER,
            "symbol": _sym(contract), "side": side,
            "qty": float(qty or 0), "price": float(price or 0),
            "amount": round(float(qty or 0) * float(price or 0), 2),
            "date": str(getattr(ts, "date", lambda: date.today())()),
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else None,
            "order_id": str(exec_id or ""),
        }))
    except Exception as e:
        logger.error("publish_fill failed (%s): %s", exec_id, e)
