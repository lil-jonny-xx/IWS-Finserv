"""
Equity Sync Worker

For each entity + broker pair:
  1. Fetch live holdings from broker API
  2. Normalise to EquityHolding objects
  3. Compute all metrics (exposure, weekly change, P&L, returns, CAGR)
  4. Upsert into equity_holding
  5. Write daily snapshot into equity_holding_history

Metric sources:
  - prev_week_value   : equity_holding_history for last Friday
  - pnl_ytd           : equity_holding_history for Jan 1 (or earliest available)
  - first_invested_date: preserved from existing equity_holding row; set to today on first insert
  - exposure_pct      : current_market_value / sum(ALL broker holdings for entity) × 100

Schedule: Daily at 7:00 AM IST (01:30 UTC) — after token_refresh_worker
Cron:     30 1 * * * /var/www/.venv/bin/python /var/www/mis-portal/equity/equity_sync_worker.py >> /var/log/mis-portal-equity-sync.log 2>&1
"""
import logging
import os
import sys
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.brokers import zerodha, angel_one, dhan
from equity.models import EquityHolding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    # File persistence handled by cron_wrapper stdout -> crontab log redirect
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

TWO   = Decimal("0.01")
FOUR  = Decimal("0.0001")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(
        host     = os.environ["DB_HOST"],
        dbname   = os.environ["DB_NAME"],
        user     = os.environ["DB_USER"],
        password = os.environ["DB_PASSWORD"],
        cursor_factory = psycopg2.extras.RealDictCursor,
    )


def load_entity_map(conn) -> dict[str, int]:
    """entity_name → entity_id"""
    cur = conn.cursor()
    cur.execute("SELECT id, entity_name FROM entity ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return {r["entity_name"]: r["id"] for r in rows}


# ---------------------------------------------------------------------------
# History lookups
# ---------------------------------------------------------------------------

def last_friday(today: date) -> date:
    """Return the most recent Friday on or before today."""
    days_since_friday = (today.weekday() - 4) % 7
    return today - timedelta(days=days_since_friday)


def jan1(today: date) -> date:
    return date(today.year, 1, 1)


def fetch_history_value(
    conn,
    entity_id: int,
    broker: str,
    symbol: str,
    snapshot_date: date,
    isin: Optional[str] = None,
) -> Optional[Decimal]:
    """
    Return the market_value from equity_holding_history at or before snapshot_date
    (nearest earlier snapshot if the exact date is missing).

    Matches on ISIN first — the stable identifier — so a ticker rename (e.g.
    SGBAUG28V -> SGBAUG28V-GB, or Angel/Zerodha series suffix changes) doesn't
    orphan the prior-week snapshots and zero out weekly_change. Falls back to a
    symbol match for legacy rows written before the isin column existed.
    """
    cur = conn.cursor()
    if isin:
        cur.execute(
            """
            SELECT market_value
            FROM   equity_holding_history
            WHERE  entity_id     = %s
              AND  broker        = %s
              AND  isin          = %s
              AND  snapshot_date <= %s
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            (entity_id, broker, isin, snapshot_date),
        )
        row = cur.fetchone()
        if row:
            cur.close()
            return Decimal(str(row["market_value"]))

    cur.execute(
        """
        SELECT market_value
        FROM   equity_holding_history
        WHERE  entity_id     = %s
          AND  broker        = %s
          AND  symbol        = %s
          AND  snapshot_date <= %s
        ORDER BY snapshot_date DESC
        LIMIT 1
        """,
        (entity_id, broker, symbol, snapshot_date),
    )
    row = cur.fetchone()
    cur.close()
    return Decimal(str(row["market_value"])) if row else None


def classify_sector(symbol: str, isin: str) -> str:
    """Classify a holding into a display sector based on symbol and ISIN prefix."""
    sym          = (symbol or '').upper()
    isin_prefix  = (isin or '')[:3].upper()

    if isin_prefix == 'IN0':
        return 'Sovereign Gold Bond'

    if isin_prefix == 'INF':
        if 'GOLD' in sym:
            return 'Gold ETF'
        if 'SILVER' in sym:
            return 'Silver ETF'
        return 'ETF'

    return 'Equity'


def fetch_first_invested_date(conn, entity_id: int, broker: str, symbol: str) -> Optional[date]:
    """Preserve the existing first_invested_date so CAGR anchor doesn't drift."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT first_invested_date
        FROM   equity_holding
        WHERE  entity_id = %s AND broker = %s AND symbol = %s
        """,
        (entity_id, broker, symbol),
    )
    row = cur.fetchone()
    cur.close()
    return row["first_invested_date"] if row else None


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def compute_metrics(
    h: EquityHolding,
    prev_week_value: Optional[Decimal],
    ytd_value: Optional[Decimal],
    total_portfolio_value: Decimal,
    today: date,
    first_invested_date: Optional[date],
) -> EquityHolding:

    h.as_of_date         = today
    h.market_value_as_on = h.current_market_value
    h.prev_week_value    = prev_week_value

    # Exposure
    if total_portfolio_value > 0:
        h.exposure_pct = (
            h.current_market_value / total_portfolio_value * 100
        ).quantize(FOUR, ROUND_HALF_UP)

    # Weekly change (value)
    if prev_week_value is not None:
        h.weekly_change = (h.current_market_value - prev_week_value).quantize(TWO)

    # P&L inception
    h.pnl_inception = (h.current_market_value - h.cost).quantize(TWO)

    # P&L YTD — uses Jan 1 snapshot as the cost base for the year
    if ytd_value is not None and ytd_value > 0:
        h.pnl_ytd = (h.current_market_value - ytd_value).quantize(TWO)

    # P&L weekly change — change in inception P&L over the week
    if prev_week_value is not None:
        prev_pnl        = (prev_week_value - h.cost).quantize(TWO)
        h.pnl_weekly_change = (h.pnl_inception - prev_pnl).quantize(TWO)

    # Returns inception %
    if h.cost > 0:
        h.returns_inception_pct = (
            h.pnl_inception / h.cost * 100
        ).quantize(FOUR, ROUND_HALF_UP)

    # Returns YTD %
    if ytd_value is not None and ytd_value > 0:
        h.returns_ytd_pct = (
            h.pnl_ytd / ytd_value * 100
        ).quantize(FOUR, ROUND_HALF_UP)

    # CAGR inception
    if first_invested_date and h.cost > 0:
        years = (today - first_invested_date).days / 365.25
        if years >= 0.08:  # at least ~1 month before computing CAGR
            ratio = float(h.current_market_value / h.cost)
            if ratio > 0:
                cagr = (ratio ** (1.0 / years) - 1.0) * 100
                h.cagr_inception_pct = Decimal(str(round(cagr, 4)))

    return h


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def upsert_equity_holding(cur, h: EquityHolding, first_invested_date: Optional[date]):
    # If symbol was previously stored as empty string (before ISIN fallback was added),
    # remove the stale row so the new upsert doesn't create a duplicate.
    if h.isin:
        cur.execute(
            """
            DELETE FROM equity_holding
            WHERE entity_id = %s AND broker = %s AND symbol = '' AND isin = %s
            """,
            (h.entity_id, h.broker, h.isin),
        )
    cur.execute(
        """
        INSERT INTO equity_holding (
            entity_id, broker, symbol, isin, exchange, sector,
            quantity, avg_cost, cost, current_price, current_market_value,
            prev_week_value, market_value_as_on, as_of_date,
            exposure_pct, weekly_change,
            pnl_ytd, pnl_inception, pnl_weekly_change,
            returns_ytd_pct, returns_inception_pct, cagr_inception_pct,
            first_invested_date, remarks, angel_one_token, updated_at
        ) VALUES (
            %(entity_id)s, %(broker)s, %(symbol)s, %(isin)s, %(exchange)s, %(sector)s,
            %(quantity)s, %(avg_cost)s, %(cost)s, %(current_price)s, %(current_market_value)s,
            %(prev_week_value)s, %(market_value_as_on)s, %(as_of_date)s,
            %(exposure_pct)s, %(weekly_change)s,
            %(pnl_ytd)s, %(pnl_inception)s, %(pnl_weekly_change)s,
            %(returns_ytd_pct)s, %(returns_inception_pct)s, %(cagr_inception_pct)s,
            %(first_invested_date)s, %(remarks)s, %(angel_one_token)s, NOW()
        )
        ON CONFLICT (entity_id, broker, symbol) DO UPDATE SET
            isin                  = EXCLUDED.isin,
            exchange              = EXCLUDED.exchange,
            sector                = EXCLUDED.sector,
            quantity              = EXCLUDED.quantity,
            avg_cost              = EXCLUDED.avg_cost,
            cost                  = EXCLUDED.cost,
            current_price         = EXCLUDED.current_price,
            current_market_value  = EXCLUDED.current_market_value,
            prev_week_value       = EXCLUDED.prev_week_value,
            market_value_as_on    = EXCLUDED.market_value_as_on,
            as_of_date            = EXCLUDED.as_of_date,
            exposure_pct          = EXCLUDED.exposure_pct,
            weekly_change         = EXCLUDED.weekly_change,
            pnl_ytd               = EXCLUDED.pnl_ytd,
            pnl_inception         = EXCLUDED.pnl_inception,
            pnl_weekly_change     = EXCLUDED.pnl_weekly_change,
            returns_ytd_pct       = EXCLUDED.returns_ytd_pct,
            returns_inception_pct = EXCLUDED.returns_inception_pct,
            cagr_inception_pct    = EXCLUDED.cagr_inception_pct,
            remarks               = EXCLUDED.remarks,
            angel_one_token       = COALESCE(EXCLUDED.angel_one_token, equity_holding.angel_one_token),
            updated_at            = NOW()
        """,
        {
            "entity_id":             h.entity_id,
            "broker":                h.broker,
            "symbol":                h.symbol,
            "isin":                  h.isin,
            "exchange":              h.exchange,
            "sector":                classify_sector(h.symbol, h.isin),
            "quantity":              float(h.quantity),
            "avg_cost":              float(h.avg_cost),
            "cost":                  float(h.cost),
            "current_price":         float(h.current_price),
            "current_market_value":  float(h.current_market_value),
            "prev_week_value":       float(h.prev_week_value)    if h.prev_week_value    else None,
            "market_value_as_on":    float(h.market_value_as_on) if h.market_value_as_on else None,
            "as_of_date":            h.as_of_date,
            "exposure_pct":          float(h.exposure_pct)          if h.exposure_pct          else None,
            "weekly_change":         float(h.weekly_change)         if h.weekly_change         else None,
            "pnl_ytd":               float(h.pnl_ytd)               if h.pnl_ytd               else None,
            "pnl_inception":         float(h.pnl_inception)         if h.pnl_inception         else None,
            "pnl_weekly_change":     float(h.pnl_weekly_change)     if h.pnl_weekly_change     else None,
            "returns_ytd_pct":       float(h.returns_ytd_pct)       if h.returns_ytd_pct       else None,
            "returns_inception_pct": float(h.returns_inception_pct) if h.returns_inception_pct else None,
            "cagr_inception_pct":    float(h.cagr_inception_pct)    if h.cagr_inception_pct    else None,
            "first_invested_date":   first_invested_date,
            "remarks":               h.remarks,
            "angel_one_token":       h.angel_one_token or None,
        },
    )


def snapshot_history(cur, h: EquityHolding, today: date):
    """Insert today's snapshot — skipped silently if already exists (ON CONFLICT DO NOTHING)."""
    cur.execute(
        """
        INSERT INTO equity_holding_history
            (entity_id, broker, symbol, isin, snapshot_date, quantity, close_price, market_value, cost, pnl)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_id, broker, symbol, snapshot_date) DO NOTHING
        """,
        (
            h.entity_id, h.broker, h.symbol, h.isin or None, today,
            float(h.quantity),
            float(h.current_price),
            float(h.current_market_value),
            float(h.cost),
            float(h.pnl_inception) if h.pnl_inception else None,
        ),
    )


# ---------------------------------------------------------------------------
# Per-entity sync
# ---------------------------------------------------------------------------

# (entity_code, broker_module, broker_label)
BROKER_ENTITY_MAP = [
    ("DHR", zerodha,   "zerodha"),
    ("DHR", angel_one, "angel_one"),
    ("HHR", zerodha,   "zerodha"),
    ("HHR", angel_one, "angel_one"),
    ("HHR", dhan,      "dhan"),
    ("SDR", zerodha,   "zerodha"),
]


def sync_entity_broker(
    conn,
    entity_id: int,
    entity_code: str,
    broker_module,
    broker_label: str,
    today: date,
) -> int:
    """Sync one entity + broker pair. Returns count of holdings upserted."""
    raw      = broker_module.fetch_holdings(entity_code)
    holdings = broker_module.normalise(entity_id, entity_code, raw)

    if not holdings:
        logger.info(f"  [{entity_code}/{broker_label}] No holdings returned")
        return 0

    # Total portfolio value for this entity+broker (needed for exposure_pct)
    total_value = sum(h.current_market_value for h in holdings)

    prev_friday = last_friday(today - timedelta(days=1))  # last completed Friday
    ytd_date    = jan1(today)

    cur = conn.cursor()
    count = 0

    for h in holdings:
        prev_week_val     = fetch_history_value(conn, entity_id, broker_label, h.symbol, prev_friday, h.isin)
        ytd_val           = fetch_history_value(conn, entity_id, broker_label, h.symbol, ytd_date, h.isin)
        first_invest_date = fetch_first_invested_date(conn, entity_id, broker_label, h.symbol)

        # On very first insert, anchor first_invested_date to today as a placeholder
        # Update this manually or via trade history API later for accurate CAGR
        if first_invest_date is None:
            first_invest_date = today

        h = compute_metrics(h, prev_week_val, ytd_val, total_value, today, first_invest_date)

        upsert_equity_holding(cur, h, first_invest_date)
        snapshot_history(cur, h, today)
        count += 1

    conn.commit()
    cur.close()
    logger.info(f"  [{entity_code}/{broker_label}] Upserted {count} holdings")
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("=== Equity sync starting ===")
    today   = date.today()
    conn    = get_conn()
    emap    = load_entity_map(conn)
    errors  = []
    total   = 0

    for entity_code, broker_module, broker_label in BROKER_ENTITY_MAP:
        entity_id = emap.get(entity_code)
        if entity_id is None:
            logger.error(f"Entity '{entity_code}' not found in DB — skipping")
            errors.append(f"{entity_code}:{broker_label}")
            continue

        logger.info(f"[{entity_code}/{broker_label}] Starting sync")
        try:
            n = sync_entity_broker(conn, entity_id, entity_code, broker_module, broker_label, today)
            total += n
        except NotImplementedError:
            logger.warning(f"[{entity_code}/{broker_label}] Broker not yet implemented — skipping")
        except Exception as e:
            logger.error(f"[{entity_code}/{broker_label}] Failed: {e}")
            conn.rollback()
            errors.append(f"{entity_code}:{broker_label}")

    # Recalculate exposure_pct across all brokers per entity now that all syncs are done.
    # Per-broker sync uses only that broker's total — this corrects it to entity-wide total.
    cur = conn.cursor()
    cur.execute("""
        UPDATE equity_holding eh
        SET    exposure_pct = ROUND(
                   eh.current_market_value
                   / NULLIF(totals.total_cmv, 0) * 100, 2
               )
        FROM (
            SELECT entity_id, SUM(current_market_value) AS total_cmv
            FROM   equity_holding
            WHERE  current_market_value IS NOT NULL
            GROUP  BY entity_id
        ) totals
        WHERE eh.entity_id = totals.entity_id
          AND eh.current_market_value IS NOT NULL
    """)
    conn.commit()
    cur.close()
    logger.info("Exposure % recalculated across all brokers per entity")

    conn.close()
    logger.info(f"=== Done. {total} holdings synced | Errors: {errors or 'none'} ===")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
