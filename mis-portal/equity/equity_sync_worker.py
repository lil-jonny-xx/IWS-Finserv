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
  - first_invested_date: preserved from existing equity_holding row; left NULL on first insert
    (real dates are backfilled from the broker tradebook via zerodha_tradebook_import.py)
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

from equity import fx, finmath
from equity.brokers import zerodha, angel_one, dhan, ibkr, vested, dbs
from equity.models import EquityHolding

# Foreign (multi-currency) brokers are stored in their own tables so the Equity
# page stays India-only and the Foreign Equity page can show native currency.
# Keep this in sync with INTERNATIONAL_BROKERS below and the migration script
# equity/db_migrate_foreign_equity.py.
FOREIGN_BROKER_LABELS = {"ibkr", "vested", "dbs"}


def _holding_table(foreign: bool) -> str:
    return "foreign_equity_holding" if foreign else "equity_holding"


def _history_table(foreign: bool) -> str:
    return "foreign_equity_holding_history" if foreign else "equity_holding_history"

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
    foreign: bool = False,
) -> Optional[Decimal]:
    """
    Return the market_value from the holding-history table at or before snapshot_date
    (nearest earlier snapshot if the exact date is missing).

    Matches on ISIN first — the stable identifier — so a ticker rename (e.g.
    SGBAUG28V -> SGBAUG28V-GB, or Angel/Zerodha series suffix changes) doesn't
    orphan the prior-week snapshots and zero out weekly_change. Falls back to a
    symbol match for legacy rows written before the isin column existed.
    """
    hist = _history_table(foreign)
    cur = conn.cursor()
    if isin:
        cur.execute(
            f"""
            SELECT market_value
            FROM   {hist}
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
        f"""
        SELECT market_value
        FROM   {hist}
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


def fetch_first_invested_date(conn, entity_id: int, broker: str, symbol: str,
                              foreign: bool = False) -> Optional[date]:
    """Preserve the existing first_invested_date so CAGR anchor doesn't drift."""
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT first_invested_date
        FROM   {_holding_table(foreign)}
        WHERE  entity_id = %s AND broker = %s AND symbol = %s
        """,
        (entity_id, broker, symbol),
    )
    row = cur.fetchone()
    cur.close()
    return row["first_invested_date"] if row else None


def ledger_metrics(conn, entity_id: int, broker: str, symbol: str,
                   current_mv_native: Optional[Decimal], today: date):
    """
    (xirr_inception_pct, first_buy_date) from the equity_trade_ledger, or
    (None, None) if the holding has no ledger (e.g. snapshot-only brokers, or
    before the one-time backfill has run).

    XIRR is money-weighted in the security's NATIVE currency: the ledger's dated
    cash flows plus the current market value (native) as the final inflow. This
    is recomputed every sync so it tracks the live market value, without
    re-pulling broker transaction history.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT trade_date, side, cash_flow_native
            FROM   equity_trade_ledger
            WHERE  entity_id = %s AND broker = %s AND symbol = %s
            ORDER  BY trade_date
            """,
            (entity_id, broker, symbol),
        )
        rows = cur.fetchall()
    except psycopg2.errors.UndefinedTable:
        conn.rollback()   # ledger table not migrated yet — silently no-op
        return None, None
    finally:
        cur.close()

    if not rows:
        return None, None

    buys      = [r["trade_date"] for r in rows if r["side"] == "BUY"]
    first_buy = min(buys) if buys else min(r["trade_date"] for r in rows)

    xirr_pct = None
    # Like CAGR, XIRR is an annualised ("p.a.") figure — only meaningful once held
    # ≥1 year. Below that the UI shows the absolute return instead, so leave it NULL.
    # (first_buy is still returned; it anchors first_invested_date regardless.)
    if current_mv_native is not None and (today - first_buy).days >= 365:
        flows = [(r["trade_date"], float(r["cash_flow_native"])) for r in rows]
        flows.append((today, float(current_mv_native)))
        rate = finmath.xirr(flows)
        if rate is not None:
            xirr_pct = Decimal(str(round(rate * 100, 4)))
    return xirr_pct, first_buy


# ---------------------------------------------------------------------------
# Currency conversion (international brokers → INR)
# ---------------------------------------------------------------------------

def apply_fx(conn, h: EquityHolding, today: date) -> EquityHolding:
    """
    Convert a holding's money fields to INR in-place, preserving the originals
    in the *_native fields. INR holdings are returned untouched (fx_rate=1).

    Raises if no FX rate is available for a foreign currency, so the caller can
    skip the holding rather than store a bogus 0/native-as-INR value.
    """
    ccy = (h.currency or "INR").upper()
    if ccy == "INR":
        h.fx_rate = Decimal("1")
        return h

    rate = fx.get_rate(conn, ccy, today)
    if rate is None:
        raise RuntimeError(f"no FX rate for {ccy}→INR on/before {today}")

    h.fx_rate = rate
    h.avg_cost_native             = h.avg_cost
    h.cost_native                 = h.cost
    h.current_price_native        = h.current_price
    h.current_market_value_native = h.current_market_value

    h.avg_cost             = (h.avg_cost * rate).quantize(FOUR, ROUND_HALF_UP)
    h.cost                 = (h.cost * rate).quantize(TWO, ROUND_HALF_UP)
    h.current_price        = (h.current_price * rate).quantize(FOUR, ROUND_HALF_UP)
    h.current_market_value = (h.current_market_value * rate).quantize(TWO, ROUND_HALF_UP)
    return h


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

    # Keep an adapter-supplied snapshot date (intl scrapers run only a few times a
    # week → show the real last-snapshot date, not every daily sync's date).
    if h.as_of_date is None:
        h.as_of_date = today
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

    # CAGR inception — only annualise once held ≥1 year. Below that, annualising a
    # partial-year return is misleading, so the UI shows the absolute return
    # (returns_inception_pct) instead and CAGR stays NULL until the 1-year mark.
    if first_invested_date and h.cost > 0:
        years = (today - first_invested_date).days / 365.25
        if years >= 1.0:
            ratio = float(h.current_market_value / h.cost)
            if ratio > 0:
                cagr = (ratio ** (1.0 / years) - 1.0) * 100
                h.cagr_inception_pct = Decimal(str(round(cagr, 4)))

    return h


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def upsert_equity_holding(cur, h: EquityHolding, first_invested_date: Optional[date],
                          foreign: bool = False):
    tbl = _holding_table(foreign)
    # If symbol was previously stored as empty string (before ISIN fallback was added),
    # remove the stale row so the new upsert doesn't create a duplicate.
    if h.isin:
        cur.execute(
            f"""
            DELETE FROM {tbl}
            WHERE entity_id = %s AND broker = %s AND symbol = '' AND isin = %s
            """,
            (h.entity_id, h.broker, h.isin),
        )
    cur.execute(
        f"""
        INSERT INTO {tbl} (
            entity_id, broker, symbol, isin, exchange, sector,
            quantity, avg_cost, cost, current_price, current_market_value,
            currency, fx_rate,
            avg_cost_native, cost_native, current_price_native, current_market_value_native,
            prev_week_value, market_value_as_on, as_of_date,
            exposure_pct, weekly_change,
            pnl_ytd, pnl_inception, pnl_weekly_change,
            returns_ytd_pct, returns_inception_pct, cagr_inception_pct,
            xirr_inception_pct,
            first_invested_date, remarks, angel_one_token, updated_at
        ) VALUES (
            %(entity_id)s, %(broker)s, %(symbol)s, %(isin)s, %(exchange)s, %(sector)s,
            %(quantity)s, %(avg_cost)s, %(cost)s, %(current_price)s, %(current_market_value)s,
            %(currency)s, %(fx_rate)s,
            %(avg_cost_native)s, %(cost_native)s, %(current_price_native)s, %(current_market_value_native)s,
            %(prev_week_value)s, %(market_value_as_on)s, %(as_of_date)s,
            %(exposure_pct)s, %(weekly_change)s,
            %(pnl_ytd)s, %(pnl_inception)s, %(pnl_weekly_change)s,
            %(returns_ytd_pct)s, %(returns_inception_pct)s, %(cagr_inception_pct)s,
            %(xirr_inception_pct)s,
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
            currency              = EXCLUDED.currency,
            fx_rate               = EXCLUDED.fx_rate,
            avg_cost_native             = EXCLUDED.avg_cost_native,
            cost_native                 = EXCLUDED.cost_native,
            current_price_native        = EXCLUDED.current_price_native,
            current_market_value_native = EXCLUDED.current_market_value_native,
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
            xirr_inception_pct    = EXCLUDED.xirr_inception_pct,
            first_invested_date   = EXCLUDED.first_invested_date,
            remarks               = EXCLUDED.remarks,
            angel_one_token       = COALESCE(EXCLUDED.angel_one_token, {tbl}.angel_one_token),
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
            "currency":              h.currency or "INR",
            "fx_rate":               float(h.fx_rate) if h.fx_rate is not None else 1,
            "avg_cost_native":             float(h.avg_cost_native)             if h.avg_cost_native             is not None else None,
            "cost_native":                 float(h.cost_native)                 if h.cost_native                 is not None else None,
            "current_price_native":        float(h.current_price_native)        if h.current_price_native        is not None else None,
            "current_market_value_native": float(h.current_market_value_native) if h.current_market_value_native is not None else None,
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
            "xirr_inception_pct":    float(h.xirr_inception_pct)    if h.xirr_inception_pct is not None else None,
            "first_invested_date":   first_invested_date,
            "remarks":               h.remarks,
            "angel_one_token":       h.angel_one_token or None,
        },
    )


def snapshot_history(cur, h: EquityHolding, today: date, foreign: bool = False):
    """Insert today's snapshot — skipped silently if already exists (ON CONFLICT DO NOTHING)."""
    cur.execute(
        f"""
        INSERT INTO {_history_table(foreign)}
            (entity_id, broker, symbol, isin, snapshot_date, quantity, close_price, market_value, cost, pnl,
             currency, fx_rate, close_price_native, market_value_native)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_id, broker, symbol, snapshot_date) DO NOTHING
        """,
        (
            h.entity_id, h.broker, h.symbol, h.isin or None, today,
            float(h.quantity),
            float(h.current_price),
            float(h.current_market_value),
            float(h.cost),
            float(h.pnl_inception) if h.pnl_inception else None,
            h.currency or "INR",
            float(h.fx_rate) if h.fx_rate is not None else 1,
            float(h.current_price_native)        if h.current_price_native        is not None else None,
            float(h.current_market_value_native) if h.current_market_value_native is not None else None,
        ),
    )


# ---------------------------------------------------------------------------
# PMS write (broker holdings that represent a PMS account)
# ---------------------------------------------------------------------------

def sync_pms_broker(
    conn,
    entity_id: int,
    entity_code: str,
    broker_module,
    broker_label: str,
    pms_source: str,
    today: date,
) -> int:
    """
    Fetch a broker's holdings for an entity and store them in pms_holding as a
    PMS portfolio (instead of equity_holding). Full-replace per (entity, source),
    mirroring nuvama_pms_worker, so sold positions disappear.

    The equity-grade metrics (XIRR/CAGR/PnL-inception/weekly) are not computed —
    the PMS view shows holdings, cost, market value and weight only.

    If the broker exposes fetch_cash_balance(), the available cash is stored as a
    'cash' row so the PMS view shows an equity total, a cash total and a combined
    total (like the Nuvama PMS). weight_pct is over the combined (equity + cash).
    """
    raw      = broker_module.fetch_holdings(entity_code)
    holdings = broker_module.normalise(entity_id, entity_code, raw)

    cash_balance = Decimal("0")
    if hasattr(broker_module, "fetch_cash_balance"):
        try:
            cash_balance = broker_module.fetch_cash_balance(entity_code) or Decimal("0")
        except Exception as e:
            logger.warning(f"  [{entity_code}/{broker_label}→PMS] cash balance fetch failed: {e}")

    equity_value = sum((h.current_market_value for h in holdings), Decimal("0"))
    total_value  = equity_value + cash_balance

    cur = conn.cursor()

    # One-time/idempotent migration: drop any rows this entity+broker previously
    # wrote to equity_holding so they no longer show on the Equity page.
    cur.execute(
        "DELETE FROM equity_holding WHERE entity_id = %s AND broker = %s",
        (entity_id, _broker_name(broker_module)),
    )

    # Full-replace this PMS source for the entity.
    cur.execute(
        "DELETE FROM pms_holding WHERE entity_id = %s AND source = %s",
        (entity_id, pms_source),
    )

    def _weight(mv: Decimal) -> Optional[float]:
        if total_value <= 0:
            return None
        return float((mv / total_value * 100).quantize(FOUR, ROUND_HALF_UP))

    def _insert(holding_type, security_name, isin, quantity, avg_cost, cost,
                current_price, market_value, weight_pct):
        cur.execute(
            """
            INSERT INTO pms_holding (
                entity_id, as_on_date, holding_type, security_name, isin,
                quantity, avg_cost, cost, current_price, market_value,
                weight_pct, source, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, NOW()
            )
            ON CONFLICT (entity_id, holding_type, security_name) DO UPDATE SET
                as_on_date    = EXCLUDED.as_on_date,
                isin          = EXCLUDED.isin,
                quantity      = EXCLUDED.quantity,
                avg_cost      = EXCLUDED.avg_cost,
                cost          = EXCLUDED.cost,
                current_price = EXCLUDED.current_price,
                market_value  = EXCLUDED.market_value,
                weight_pct    = EXCLUDED.weight_pct,
                source        = EXCLUDED.source,
                updated_at    = NOW()
            """,
            (
                entity_id, today, holding_type, security_name, isin,
                quantity, avg_cost, cost, current_price, market_value,
                weight_pct, pms_source,
            ),
        )

    count = 0
    for h in holdings:
        _insert(
            "equity", h.symbol, h.isin or None,
            float(h.quantity), float(h.avg_cost), float(h.cost),
            float(h.current_price), float(h.current_market_value),
            _weight(h.current_market_value),
        )
        count += 1

    # Cash balance as a 'cash' row (cost == value, so no P&L).
    if cash_balance > 0:
        _insert(
            "cash", "Cash Balance", None,
            None, None, float(cash_balance),
            None, float(cash_balance),
            _weight(cash_balance),
        )
        count += 1

    conn.commit()
    cur.close()
    logger.info(
        f"  [{entity_code}/{broker_label}→PMS] Upserted {count} rows into pms_holding "
        f"(equity ₹{equity_value} + cash ₹{cash_balance})"
    )
    return count


def _broker_name(broker_module) -> str:
    """Map a broker module to its equity_holding.broker label (e.g. 'zerodha')."""
    return broker_module.__name__.rsplit(".", 1)[-1]


def refresh_broker_cash(conn, emap: Optional[dict] = None) -> int:
    """
    Fetch available cash for every (entity, broker) in BROKER_ENTITY_MAP and
    upsert into broker_cash. One API call per account; failures are isolated so
    one bad token doesn't drop the others. Returns count of accounts updated.

    Rajani Corp's Zerodha cash is intentionally NOT here — that account is a PMS
    (PMS_BROKER_MAP), so its cash is stored in pms_holding instead.
    """
    if emap is None:
        emap = load_entity_map(conn)

    today   = date.today()
    cur     = conn.cursor()
    updated = 0
    # TEMPORARY (2026-06-24): mirror the IBKR_SYNC_PAUSED guard in main() so the
    # cash refresh doesn't poke the locked-out IBKR token either. Remove with it.
    ibkr_paused = os.environ.get("IBKR_SYNC_PAUSED", "").strip().lower() in ("1", "true", "yes")
    for entity_code, broker_module, broker_label in active_broker_map(emap):
        if ibkr_paused and broker_label == "ibkr":
            continue
        entity_id = emap.get(entity_code)
        if entity_id is None or not hasattr(broker_module, "fetch_cash_balance"):
            continue
        try:
            bal = broker_module.fetch_cash_balance(entity_code) or Decimal("0")
        except Exception as e:
            logger.warning(f"  [{entity_code}/{broker_label}] cash fetch failed: {e}")
            continue

        # Convert foreign-currency cash to INR (stored in `balance`); keep the
        # original in `balance_native`. The cash currency for IBKR is the account
        # base currency; Vested→USD, DBS→SGD via the module's CURRENCY constant.
        if hasattr(broker_module, "cash_currency"):
            ccy = broker_module.cash_currency(entity_code)
        else:
            ccy = getattr(broker_module, "CURRENCY", "INR")
        ccy = (ccy or "INR").upper()

        if ccy == "INR":
            inr_bal, native_bal, rate = bal, None, Decimal("1")
        else:
            rate = fx.get_rate(conn, ccy, today)
            if rate is None:
                logger.warning(f"  [{entity_code}/{broker_label}] cash FX skip — no {ccy} rate")
                continue
            inr_bal, native_bal = (bal * rate).quantize(TWO, ROUND_HALF_UP), bal

        cur.execute(
            """
            INSERT INTO broker_cash
                (entity_id, broker, balance, currency, fx_rate, balance_native, as_of_date, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, broker) DO UPDATE SET
                balance        = EXCLUDED.balance,
                currency       = EXCLUDED.currency,
                fx_rate        = EXCLUDED.fx_rate,
                balance_native = EXCLUDED.balance_native,
                as_of_date     = EXCLUDED.as_of_date,
                updated_at     = NOW()
            """,
            (entity_id, broker_label, float(inr_bal), ccy, float(rate),
             float(native_bal) if native_bal is not None else None, today),
        )
        updated += 1

    conn.commit()
    cur.close()
    logger.info(f"Broker cash refreshed for {updated} account(s)")
    return updated


# ---------------------------------------------------------------------------
# Per-entity sync
# ---------------------------------------------------------------------------

# (entity_code, broker_module, broker_label)
BROKER_ENTITY_MAP = [
    ("DHR",         zerodha,   "zerodha"),
    ("DHR",         angel_one, "angel_one"),
    ("HHR",         zerodha,   "zerodha"),
    ("HHR",         angel_one, "angel_one"),
    ("HHR",         dhan,      "dhan"),
    ("SDR",         zerodha,   "zerodha"),
    ("Rajani Corp", dhan,      "dhan"),
]

# International (multi-currency) brokers. These are not hard-wired to entities —
# an (entity, broker) pair becomes active as soon as its credentials/feed are
# present in .env, so adding an account is a pure config change (no code edit):
#   IBKR   → IBKR_{CODE}_FLEX_TOKEN + IBKR_{CODE}_QUERY_ID  (automated Flex pull)
#   Vested → VESTED_{CODE}_HOLDINGS or VESTED_{CODE}_HOLDINGS_FILE
#   DBS    → DBS_{CODE}_HOLDINGS    or DBS_{CODE}_HOLDINGS_FILE
# Holdings land in equity_holding (Equity page), converted to INR via apply_fx.
INTERNATIONAL_BROKERS = [
    (ibkr,   "ibkr"),
    (vested, "vested"),
    (dbs,    "dbs"),
]


def _intl_configured(broker_module, broker_label: str, entity_code: str) -> bool:
    """True if this entity has credentials/feed for an international broker."""
    prefix = {"Rajani Corp": "RAJANIRCORP"}.get(entity_code, entity_code)
    if broker_label == "ibkr":
        return bool(os.environ.get(f"IBKR_{prefix}_FLEX_TOKEN")
                    and os.environ.get(f"IBKR_{prefix}_QUERY_ID"))
    # Vested / DBS — active when scraper credentials are set OR a data source
    # (scraper cache file / manual feed) already exists. The adapter owns the
    # data-source check; the credentials check lets an account light up from a
    # fresh .env before the scraper's first successful run.
    env_prefix = broker_label.upper()   # VESTED / DBS
    if os.environ.get(f"{env_prefix}_{prefix}_USERNAME"):
        return True
    if hasattr(broker_module, "configured"):
        return broker_module.configured(entity_code)
    return bool(os.environ.get(f"{env_prefix}_{prefix}_HOLDINGS")
                or os.environ.get(f"{env_prefix}_{prefix}_HOLDINGS_FILE"))


def discover_international(emap: dict) -> list:
    """(entity_code, broker_module, broker_label) for every configured intl account."""
    found = []
    for entity_code in emap:
        for broker_module, broker_label in INTERNATIONAL_BROKERS:
            if _intl_configured(broker_module, broker_label, entity_code):
                found.append((entity_code, broker_module, broker_label))
    return found


def active_broker_map(emap: dict) -> list:
    """Indian brokers (static) + any international accounts configured in .env."""
    return list(BROKER_ENTITY_MAP) + discover_international(emap)

# Entities whose holdings at a given broker are actually a PMS account and should
# land in pms_holding (shown on the PMS page) instead of equity_holding.
# Rajani Corp's Zerodha account is a discretionary PMS, so its Zerodha holdings
# are routed here. Its Dhan holdings stay in the regular equity flow above.
# (entity_code, broker_module, broker_label, pms_source)
PMS_BROKER_MAP = [
    ("Rajani Corp", zerodha, "zerodha", "zerodha_pms"),
]


def sync_entity_broker(
    conn,
    entity_id: int,
    entity_code: str,
    broker_module,
    broker_label: str,
    today: date,
    foreign: bool = False,
    raw: "Optional[list]" = None,
) -> int:
    """Sync one entity + broker pair. Returns count of holdings upserted.

    Foreign (multi-currency) brokers are written to foreign_equity_holding(+history)
    so the Equity page stays India-only; INR-converted columns are still populated
    so net-worth aggregators can union the foreign table.

    Pass `raw` (already-fetched holding dicts) to skip the broker fetch entirely — used
    by the paced IBKR worker so holdings come from the SAME statement it already pulled
    for cash, guaranteeing one Flex hit and never a re-fetch.
    """
    if raw is None:
        raw = broker_module.fetch_holdings(entity_code)
    holdings = broker_module.normalise(entity_id, entity_code, raw)

    if not holdings:
        logger.info(f"  [{entity_code}/{broker_label}] No holdings returned")
        return 0

    # Convert any foreign-currency holdings to INR in-place (originals kept in
    # the *_native fields). Skip a holding only if its FX rate is unavailable.
    converted = []
    for h in holdings:
        try:
            converted.append(apply_fx(conn, h, today))
        except Exception as e:
            logger.warning(f"  [{entity_code}/{broker_label}] {h.symbol}: FX skip — {e}")
    holdings = converted
    if not holdings:
        logger.info(f"  [{entity_code}/{broker_label}] No holdings after FX conversion")
        return 0

    # Total portfolio value for this entity+broker (needed for exposure_pct), in INR
    total_value = sum(h.current_market_value for h in holdings)

    prev_friday = last_friday(today - timedelta(days=1))  # last completed Friday
    ytd_date    = jan1(today)

    cur = conn.cursor()
    count = 0

    for h in holdings:
        prev_week_val     = fetch_history_value(conn, entity_id, broker_label, h.symbol, prev_friday, h.isin, foreign)
        ytd_val           = fetch_history_value(conn, entity_id, broker_label, h.symbol, ytd_date, h.isin, foreign)
        # Leave NULL until the real purchase date is known (backfilled from the broker
        # tradebook — see zerodha_tradebook_import.py). Defaulting to today stamped a wrong
        # "Since" date and produced a bogus ~0-year CAGR. Both compute_metrics and the upsert
        # handle None: "Since" renders "—" and CAGR is skipped until a real date is set.
        # Foreign brokers (Vested / DBS) derive it from the scraped transaction history and
        # set it on the holding — prefer that over the preserved DB value. The trade ledger
        # (IBKR inception backfill) supplies it too, ahead of the preserved DB value.
        ledger_xirr, ledger_first_buy = ledger_metrics(
            conn, entity_id, broker_label, h.symbol, h.current_market_value_native, today)
        first_invest_date = h.first_invested_date or ledger_first_buy or \
            fetch_first_invested_date(conn, entity_id, broker_label, h.symbol, foreign)

        h = compute_metrics(h, prev_week_val, ytd_val, total_value, today, first_invest_date)

        # Recompute XIRR from the ledger each run so it tracks live market value.
        # Adapter-supplied XIRR (if any) wins; otherwise use the ledger result.
        if h.xirr_inception_pct is None and ledger_xirr is not None:
            h.xirr_inception_pct = ledger_xirr

        upsert_equity_holding(cur, h, first_invest_date, foreign)
        snapshot_history(cur, h, today, foreign)
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

    # TEMPORARY (2026-06-24): the DHR IBKR Flex token is in a 1025 lockout. Each
    # failed sync attempt extends it, so skip the IBKR leg entirely while it rests
    # / the token is re-verified. Remove this guard (and IBKR_SYNC_PAUSED in .env)
    # once the token works again. Other brokers are unaffected.
    ibkr_paused = os.environ.get("IBKR_SYNC_PAUSED", "").strip().lower() in ("1", "true", "yes")

    for entity_code, broker_module, broker_label in active_broker_map(emap):
        if ibkr_paused and broker_label == "ibkr":
            logger.warning(f"[{entity_code}/ibkr] SKIPPED — IBKR_SYNC_PAUSED set "
                           f"(token in 1025 lockout; see .env note, remove when fixed)")
            continue
        entity_id = emap.get(entity_code)
        if entity_id is None:
            logger.error(f"Entity '{entity_code}' not found in DB — skipping")
            errors.append(f"{entity_code}:{broker_label}")
            continue

        foreign = broker_label in FOREIGN_BROKER_LABELS
        logger.info(f"[{entity_code}/{broker_label}] Starting sync{' (foreign)' if foreign else ''}")
        try:
            n = sync_entity_broker(conn, entity_id, entity_code, broker_module, broker_label, today, foreign)
            total += n
        except NotImplementedError:
            logger.warning(f"[{entity_code}/{broker_label}] Broker not yet implemented — skipping")
        except Exception as e:
            logger.error(f"[{entity_code}/{broker_label}] Failed: {e}")
            conn.rollback()
            errors.append(f"{entity_code}:{broker_label}")

    # PMS-routed brokers — holdings stored in pms_holding (shown on the PMS page)
    # rather than equity_holding. e.g. Rajani Corp's Zerodha discretionary PMS.
    for entity_code, broker_module, broker_label, pms_source in PMS_BROKER_MAP:
        entity_id = emap.get(entity_code)
        if entity_id is None:
            logger.error(f"Entity '{entity_code}' not found in DB — skipping")
            errors.append(f"{entity_code}:{broker_label}(pms)")
            continue

        logger.info(f"[{entity_code}/{broker_label}→PMS] Starting sync")
        try:
            n = sync_pms_broker(conn, entity_id, entity_code, broker_module, broker_label, pms_source, today)
            total += n
        except NotImplementedError:
            logger.warning(f"[{entity_code}/{broker_label}→PMS] Broker not yet implemented — skipping")
        except Exception as e:
            logger.error(f"[{entity_code}/{broker_label}→PMS] Failed: {e}")
            conn.rollback()
            errors.append(f"{entity_code}:{broker_label}(pms)")

    # Cash balances per (entity, broker) for the Equity page.
    try:
        refresh_broker_cash(conn, emap)
    except Exception as e:
        logger.error(f"Broker cash refresh failed: {e}")
        conn.rollback()

    # Recalculate exposure_pct across all brokers per entity now that all syncs are done.
    # Per-broker sync uses only that broker's total — this corrects it to entity-wide total.
    # Indian and foreign holdings are weighted within their own table (each page shows its
    # own 100%).
    cur = conn.cursor()
    for tbl in ("equity_holding", "foreign_equity_holding"):
        cur.execute(f"""
            UPDATE {tbl} eh
            SET    exposure_pct = ROUND(
                       eh.current_market_value
                       / NULLIF(totals.total_cmv, 0) * 100, 2
                   )
            FROM (
                SELECT entity_id, SUM(current_market_value) AS total_cmv
                FROM   {tbl}
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
