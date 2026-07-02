#!/usr/bin/env python3
"""
Foreign Snapshot Worker — intraday position snapshots + trade detection for Vested.

Vested holdings are *scraped* (no trades API), so — unlike IBKR, whose Flex statement
reports exact executed trades straight into equity_trade_ledger — the only way to know
what Vested bought/sold is to snapshot positions and diff them. This worker runs every
~2 hours on weekdays: it pulls each entity's Vested positions (native USD via the same
fetch/normalise path the daily sync uses), writes them to equity_position_snapshot
(broker='vested'), and diffs against the previous tick:

  • quantity dropped  → a SELL of the difference, priced (native) at this tick
  • quantity rose / new symbol → a BUY of the difference

Detected trades are written to equity_trade_ledger (source='snapshot') in NATIVE USD —
the foreign realised-gains path converts each leg to INR at its own trade-date FX, so
realised P&L embeds the currency move. On an account's first snapshot we seed an opening
BUY per holding at its native avg cost (source='snapshot_open') so a later sell nets to a
correct realised P&L, skipping any symbol that already has ledger buy history (avoids
double-counting an IBKR-style import or a prior seed).

IBKR is deliberately NOT snapshotted here — ibkr_holdings_cash_worker.py already ingests
its exact Flex trades; see deploy/systemd for its pre/post-market schedule.

Run manually:  python -m workers.foreign_snapshot_worker
"""
import logging
import os
import sys
import zoneinfo
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.equity_sync_worker import (  # noqa: E402
    get_conn, load_entity_map, discover_international,
)
from workers.equity_snapshot_worker import _d, load_prev_snapshot  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

US_TZ   = zoneinfo.ZoneInfo("America/New_York")
QTY_EPS = Decimal("0.0001")


def is_weekday() -> bool:
    """Vested is a scraper cache (no live API cost), so we just skip weekends —
    every ~2h on a US weekday, diffing whatever the last scrape produced."""
    return datetime.now(US_TZ).weekday() < 5


# ---------------------------------------------------------------------------
# Pricing — the scrape feed gives quantities but NOT native price/avg cost; those
# live on foreign_equity_holding (qty from the daily sync, price from the foreign
# price worker). So we diff on the fresh scrape's quantities but price each leg from
# the synced holding row.
# ---------------------------------------------------------------------------
def load_foreign_holding_map(cur, entity_id: int, broker: str) -> dict:
    cur.execute(
        """
        SELECT symbol, isin, current_price_native, avg_cost_native, currency
        FROM   foreign_equity_holding
        WHERE  entity_id = %s AND broker = %s
        """,
        (entity_id, broker),
    )
    return {
        r["symbol"]: {
            "price":    r["current_price_native"],
            "avg":      r["avg_cost_native"],
            "isin":     r["isin"] or None,
            "currency": (r["currency"] or "USD").upper(),
        }
        for r in cur.fetchall()
    }


def enrich_holdings(holdings, hold_map):
    """Fill native price/avg/isin/currency on each live holding from the synced DB row."""
    for h in holdings:
        info = hold_map.get(h.symbol)
        if not info:
            continue
        if getattr(h, "current_price_native", None) in (None, 0):
            h.current_price_native = info["price"]
        if getattr(h, "avg_cost_native", None) in (None, 0):
            h.avg_cost_native = info["avg"]
        if not getattr(h, "isin", None):
            h.isin = info["isin"]
        if not getattr(h, "currency", None) or h.currency == "INR":
            h.currency = info["currency"]
    return holdings


# ---------------------------------------------------------------------------
# equity_trade_ledger writes (native currency)
# ---------------------------------------------------------------------------
def _ledger_exists(cur, broker: str, external_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM equity_trade_ledger WHERE broker = %s AND external_id = %s",
        (broker, external_id),
    )
    return cur.fetchone() is not None


def _ledger_has_buys(cur, entity_id: int, broker: str, symbol: str) -> bool:
    cur.execute(
        "SELECT 1 FROM equity_trade_ledger "
        "WHERE entity_id = %s AND broker = %s AND symbol = %s AND side = 'BUY' LIMIT 1",
        (entity_id, broker, symbol),
    )
    return cur.fetchone() is not None


def _insert_ledger(cur, entity_id, broker, symbol, isin, tdate, side,
                   qty, price_native, currency, source, external_id):
    qty   = float(qty)
    price = float(price_native)
    gross = qty * price
    # Signed native cash flow, net of commission (none for a snapshot leg): buy < 0, sell > 0.
    cash_flow = -gross if side == "BUY" else gross
    cur.execute(
        """
        INSERT INTO equity_trade_ledger
            (entity_id, broker, symbol, isin, trade_date, side, quantity,
             price_native, fees_native, currency, cash_flow_native, source, external_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s)
        ON CONFLICT (broker, external_id) WHERE external_id IS NOT NULL DO NOTHING
        """,
        (entity_id, broker, symbol, isin, tdate, side, qty,
         price, currency, cash_flow, source, external_id),
    )


# ---------------------------------------------------------------------------
# Snapshot + diff
# ---------------------------------------------------------------------------
def write_snapshot(cur, entity_id, broker, holdings, captured_at):
    """One equity_position_snapshot row per holding, priced in NATIVE currency."""
    for h in holdings:
        qty   = _d(h.quantity)
        price = _d(h.current_price_native) if getattr(h, "current_price_native", None) is not None else None
        mv    = (qty * price) if price is not None else None
        cur.execute(
            """
            INSERT INTO equity_position_snapshot
                (entity_id, broker, symbol, isin, captured_at, snapshot_kind,
                 quantity, price, market_value, avg_cost)
            VALUES (%s, %s, %s, %s, %s, 'periodic', %s, %s, %s, %s)
            ON CONFLICT (entity_id, broker, symbol, captured_at) DO NOTHING
            """,
            (entity_id, broker, h.symbol, getattr(h, "isin", None) or None,
             captured_at, qty, price, mv, _d(getattr(h, "avg_cost_native", None))),
        )


def seed_opening_buys(cur, entity_id, broker, holdings, today):
    """First-ever snapshot for this account: opening BUY per holding at native avg cost,
    so realised P&L has a cost basis. Skipped when ledger buy history already exists."""
    seeded = 0
    for h in holdings:
        qty = _d(h.quantity)
        avg = _d(getattr(h, "avg_cost_native", None))
        if qty <= QTY_EPS or avg <= 0:
            continue
        symbol = h.symbol
        ext = f"snap_open:{entity_id}|{symbol}"
        if _ledger_exists(cur, broker, ext) or _ledger_has_buys(cur, entity_id, broker, symbol):
            continue
        tdate = getattr(h, "first_invested_date", None) or today
        _insert_ledger(cur, entity_id, broker, symbol, getattr(h, "isin", None) or None,
                       tdate, "BUY", qty, avg, (h.currency or "USD").upper(),
                       "snapshot_open", ext)
        seeded += 1
    return seeded


def detect_trades(cur, entity_id, broker, holdings, prev, captured_at, today):
    curr = {h.symbol: h for h in holdings}
    ts_iso = captured_at.isoformat()
    buys = sells = 0

    for symbol in set(curr) | set(prev):
        h = curr.get(symbol)
        curr_qty = _d(h.quantity) if h else Decimal("0")
        prev_qty = prev.get(symbol, {}).get("qty", Decimal("0"))
        delta = curr_qty - prev_qty
        if abs(delta) <= QTY_EPS:
            continue

        if delta > 0:
            side, qty = "BUY", delta
            price = _d(h.current_price_native) if getattr(h, "current_price_native", None) is not None else None
            isin  = getattr(h, "isin", None) or None
            ccy   = (h.currency or "USD").upper()
        else:
            side, qty = "SELL", -delta
            price = _d(h.current_price_native) if (h and getattr(h, "current_price_native", None) is not None) \
                else _d(prev.get(symbol, {}).get("price"))
            isin  = (getattr(h, "isin", None) if h else None) or prev.get(symbol, {}).get("isin") or None
            ccy   = (h.currency if h else None) or "USD"

        if price is None or price <= 0:
            logger.warning(f"[{entity_id}/{broker}] {symbol}: no native price for {side} — skipping")
            continue

        ext = f"snap:{entity_id}|{symbol}|{ts_iso}|{side}"
        if _ledger_exists(cur, broker, ext):
            continue
        _insert_ledger(cur, entity_id, broker, symbol, isin, today, side,
                       qty, price, ccy.upper(), "snapshot", ext)
        if side == "BUY":
            buys += 1
        else:
            sells += 1
    return buys, sells


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    if not is_weekday():
        logger.debug("Weekend — skipping foreign snapshot.")
        return

    captured_at = datetime.now(timezone.utc)
    today       = date.today()
    conn = get_conn()
    try:
        emap = load_entity_map(conn)
        vested = [(c, m, l) for (c, m, l) in discover_international(emap) if l == "vested"]
        if not vested:
            logger.info("No Vested account configured — nothing to snapshot.")
            return

        total_snap = total_seed = total_buy = total_sell = 0
        for entity_code, broker_module, broker_label in vested:
            entity_id = emap.get(entity_code)
            if entity_id is None:
                continue
            try:
                raw = broker_module.fetch_holdings(entity_code)
                holdings = broker_module.normalise(entity_id, entity_code, raw)
            except Exception as e:
                logger.error(f"[{entity_code}/{broker_label}] holdings fetch failed: {e}")
                conn.rollback()
                continue
            if not holdings:
                logger.info(f"[{entity_code}/{broker_label}] no holdings this tick")
                continue

            cur = conn.cursor()
            try:
                holdings = enrich_holdings(holdings, load_foreign_holding_map(cur, entity_id, broker_label))
                prev = load_prev_snapshot(cur, entity_id, broker_label, captured_at)
                write_snapshot(cur, entity_id, broker_label, holdings, captured_at)
                total_snap += len(holdings)
                if not prev:
                    total_seed += seed_opening_buys(cur, entity_id, broker_label, holdings, today)
                else:
                    b, s = detect_trades(cur, entity_id, broker_label, holdings,
                                         prev, captured_at, today)
                    total_buy += b
                    total_sell += s
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"[{entity_code}/{broker_label}] snapshot/diff failed: {e}")
            finally:
                cur.close()

        logger.info(
            f"Foreign snapshot: {total_snap} positions, seeded {total_seed}, "
            f"detected {total_buy} buys / {total_sell} sells"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    run()
