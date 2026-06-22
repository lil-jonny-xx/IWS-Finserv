"""
Equity History Backfill

Fetches historical closing prices from Kite Connect for key dates:
  - Last completed Friday  (for prev_week_value / weekly_change)
  - Jan 1 of current FY    (for pnl_ytd / returns_ytd_pct)
  - Earliest available     (for CAGR when first_invested_date is missing)

Writes rows to equity_holding_history, then re-runs compute_metrics so all
NULL columns in equity_holding get populated.

Usage:
    python equity/equity_history_backfill.py
"""
import logging
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.brokers import zerodha
from equity.equity_sync_worker import (
    compute_metrics,
    fetch_history_value,
    BROKER_ENTITY_MAP,
)
from equity.models import EquityHolding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def last_friday(ref: date) -> date:
    days_back = (ref.weekday() - 4) % 7 or 7
    return ref - timedelta(days=days_back)


def fy_start(ref: date) -> date:
    return date(ref.year if ref.month >= 4 else ref.year - 1, 4, 1)


def get_db():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        dbname=os.environ["DB_NAME"],
        user=os.environ.get("DB_USER", os.environ["DB_NAME"]),
        password=os.environ["DB_PASSWORD"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def build_token_map(kite) -> dict[str, int]:
    insts = kite.instruments("NSE")
    return {i["tradingsymbol"]: i["instrument_token"]
            for i in insts if i["instrument_type"] == "EQ"}


def fetch_close_on(kite, token: int, target: date) -> float | None:
    """Return the closing price on or nearest before target date."""
    from_d = target - timedelta(days=7)
    try:
        candles = kite.historical_data(token, from_d, target, "day")
        if candles:
            return float(candles[-1]["close"])
    except Exception as e:
        logger.warning(f"historical_data failed for token {token}: {e}")
    return None


def backfill_entity_broker(conn, entity_id: int, entity_code: str,
                            broker_label: str, kite, token_map: dict,
                            target_dates: list[date]) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM equity_holding WHERE entity_id = %s AND broker = %s",
        (entity_id, broker_label),
    )
    holdings = cur.fetchall()
    if not holdings:
        logger.info(f"[{entity_code}/{broker_label}] No holdings, skipping backfill")
        return

    logger.info(f"[{entity_code}/{broker_label}] Backfilling {len(holdings)} holdings "
                f"for dates: {target_dates}")

    for h in holdings:
        symbol = h["symbol"]
        token  = token_map.get(symbol)
        if not token:
            logger.warning(f"  {symbol}: no instrument token, skipping")
            continue

        for target in target_dates:
            close = fetch_close_on(kite, token, target)
            if close is None:
                logger.warning(f"  {symbol} @ {target}: no price data")
                continue

            qty          = float(h["quantity"])
            market_value = close * qty
            cost         = float(h["cost"])
            pnl          = market_value - cost

            cur.execute("""
                INSERT INTO equity_holding_history
                    (entity_id, broker, symbol, isin, snapshot_date,
                     quantity, close_price, market_value, cost, pnl)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, broker, symbol, snapshot_date) DO UPDATE
                    SET isin         = EXCLUDED.isin,
                        close_price  = EXCLUDED.close_price,
                        market_value = EXCLUDED.market_value,
                        pnl          = EXCLUDED.pnl
            """, (entity_id, broker_label, symbol, h["isin"], target,
                  qty, close, market_value, cost, pnl))
            logger.info(f"  {symbol} @ {target}: ₹{close:.2f} → value ₹{market_value:,.0f}")

    conn.commit()
    logger.info(f"[{entity_code}/{broker_label}] History backfill done")


def recompute_metrics(conn, entity_id: int, broker_label: str,
                      today: date, prev_friday: date, ytd_date: date) -> None:
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM equity_holding WHERE entity_id = %s AND broker = %s",
        (entity_id, broker_label),
    )
    holdings = cur.fetchall()

    total_value = Decimal(str(
        sum(float(h["current_market_value"] or 0) for h in holdings)
    ))

    updated = 0
    for h in holdings:
        symbol = h["symbol"]

        prev_val  = fetch_history_value(conn, entity_id, broker_label, symbol, prev_friday, h["isin"])
        ytd_val   = fetch_history_value(conn, entity_id, broker_label, symbol, ytd_date, h["isin"])
        first_inv = h["first_invested_date"]

        holding = EquityHolding(
            entity_id           = entity_id,
            broker              = broker_label,
            symbol              = symbol,
            isin                = h["isin"] or "",
            exchange            = h["exchange"] or "NSE",
            quantity            = Decimal(str(h["quantity"])),
            avg_cost            = Decimal(str(h["avg_cost"])),
            cost                = Decimal(str(h["cost"])),
            current_price       = Decimal(str(h["current_price"] or 0)),
            current_market_value= Decimal(str(h["current_market_value"] or 0)),
        )

        compute_metrics(holding, prev_val, ytd_val, total_value, today, first_inv)

        cur.execute("""
            UPDATE equity_holding SET
                prev_week_value      = %(prev_week_value)s,
                market_value_as_on   = %(market_value_as_on)s,
                as_of_date           = %(as_of_date)s,
                exposure_pct         = %(exposure_pct)s,
                weekly_change        = %(weekly_change)s,
                pnl_inception        = %(pnl_inception)s,
                pnl_ytd              = %(pnl_ytd)s,
                pnl_weekly_change    = %(pnl_weekly_change)s,
                returns_inception_pct= %(returns_inception_pct)s,
                returns_ytd_pct      = %(returns_ytd_pct)s,
                cagr_inception_pct   = %(cagr_inception_pct)s,
                updated_at           = NOW()
            WHERE entity_id = %(entity_id)s AND broker = %(broker)s AND symbol = %(symbol)s
        """, {
            "prev_week_value":       float(holding.prev_week_value) if holding.prev_week_value else None,
            "market_value_as_on":    float(holding.market_value_as_on) if holding.market_value_as_on else None,
            "as_of_date":            holding.as_of_date,
            "exposure_pct":          float(holding.exposure_pct) if holding.exposure_pct else None,
            "weekly_change":         float(holding.weekly_change) if holding.weekly_change else None,
            "pnl_inception":         float(holding.pnl_inception) if holding.pnl_inception else None,
            "pnl_ytd":               float(holding.pnl_ytd) if holding.pnl_ytd else None,
            "pnl_weekly_change":     float(holding.pnl_weekly_change) if holding.pnl_weekly_change else None,
            "returns_inception_pct": float(holding.returns_inception_pct) if holding.returns_inception_pct else None,
            "returns_ytd_pct":       float(holding.returns_ytd_pct) if holding.returns_ytd_pct else None,
            "cagr_inception_pct":    float(holding.cagr_inception_pct) if holding.cagr_inception_pct else None,
            "entity_id":             entity_id,
            "broker":                broker_label,
            "symbol":                symbol,
        })
        updated += 1

    conn.commit()
    logger.info(f"  Recomputed metrics for {updated} holdings")


def main():
    today       = date.today()
    prev_friday = last_friday(today)
    ytd_date    = fy_start(today)

    logger.info(f"Backfill dates — last Friday: {prev_friday}, FY start: {ytd_date}")

    conn = get_db()

    # Only backfill brokers that use Kite (Zerodha)
    kite_entities = [(ec, bl) for ec, bm, bl in BROKER_ENTITY_MAP if bl == "zerodha"]

    cur = conn.cursor()
    cur.execute("SELECT entity_name, id FROM entity")
    emap = {r["entity_name"]: r["id"] for r in cur.fetchall()}

    for entity_code, broker_label in kite_entities:
        entity_id = emap.get(entity_code)
        if not entity_id:
            logger.warning(f"Entity {entity_code} not found in DB")
            continue

        try:
            kite      = zerodha._kite_client(entity_code)
            token_map = build_token_map(kite)

            backfill_entity_broker(conn, entity_id, entity_code, broker_label,
                                   kite, token_map, [prev_friday, ytd_date])
            recompute_metrics(conn, entity_id, broker_label,
                              today, prev_friday, ytd_date)
        except Exception as e:
            logger.error(f"[{entity_code}/{broker_label}] Backfill failed: {e}")
            import traceback; traceback.print_exc()

    conn.close()
    logger.info("Backfill complete")


if __name__ == "__main__":
    main()
