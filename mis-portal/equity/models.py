"""
Equity portfolio data models and DB schema definitions.

Table: equity_holding
  Stores latest snapshot of each equity position per entity per broker.
  Upserted on every sync run (keyed on entity_id + broker + symbol).

Table: equity_holding_history
  Daily snapshots for week-over-week / YTD / inception calculations.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


# ---------------------------------------------------------------------------
# Broker identifiers
# ---------------------------------------------------------------------------

ZERODHA   = "zerodha"
ANGEL_ONE = "angel_one"
DHAN      = "dhan"

BROKERS = [ZERODHA, ANGEL_ONE, DHAN]


# ---------------------------------------------------------------------------
# Core holding record — one row per symbol per entity per broker
# ---------------------------------------------------------------------------

@dataclass
class EquityHolding:
    entity_id:   int
    broker:      str            # zerodha | angel_one | dhan
    symbol:      str            # exchange symbol e.g. "RELIANCE"
    isin:        str
    exchange:    str            # NSE | BSE

    quantity:    Decimal
    avg_cost:    Decimal        # average cost price per share
    cost:        Decimal        # total cost = quantity × avg_cost

    current_price:       Decimal
    current_market_value: Decimal   # quantity × current_price

    prev_week_value:     Optional[Decimal] = None   # value as of last Friday close
    market_value_as_on:  Optional[Decimal] = None   # same as current_market_value; date stored separately

    # Computed fields — populated by metrics layer, not broker API
    exposure_pct:        Optional[Decimal] = None   # holding / total portfolio value × 100
    weekly_change:       Optional[Decimal] = None   # current_market_value - prev_week_value

    pnl_ytd:             Optional[Decimal] = None
    pnl_inception:       Optional[Decimal] = None
    pnl_weekly_change:   Optional[Decimal] = None

    returns_ytd_pct:     Optional[Decimal] = None
    returns_inception_pct: Optional[Decimal] = None
    cagr_inception_pct:  Optional[Decimal] = None

    remarks:             Optional[str] = None

    as_of_date:          Optional[date] = None      # trading date of price data


# ---------------------------------------------------------------------------
# DB DDL (run once via migration)
# ---------------------------------------------------------------------------

CREATE_EQUITY_HOLDING_SQL = """
CREATE TABLE IF NOT EXISTS equity_holding (
    id                    SERIAL PRIMARY KEY,
    entity_id             INTEGER NOT NULL REFERENCES entity(id),
    broker                VARCHAR(20) NOT NULL,
    symbol                VARCHAR(50) NOT NULL,
    isin                  VARCHAR(20),
    exchange              VARCHAR(10),

    quantity              NUMERIC(18, 4)  NOT NULL,
    avg_cost              NUMERIC(18, 4)  NOT NULL,
    cost                  NUMERIC(18, 2)  NOT NULL,

    current_price         NUMERIC(18, 4),
    current_market_value  NUMERIC(18, 2),
    prev_week_value       NUMERIC(18, 2),
    market_value_as_on    NUMERIC(18, 2),

    exposure_pct          NUMERIC(8, 4),
    weekly_change         NUMERIC(18, 2),

    pnl_ytd               NUMERIC(18, 2),
    pnl_inception         NUMERIC(18, 2),
    pnl_weekly_change     NUMERIC(18, 2),

    returns_ytd_pct       NUMERIC(8, 4),
    returns_inception_pct NUMERIC(8, 4),
    cagr_inception_pct    NUMERIC(8, 4),

    remarks               TEXT,
    as_of_date            DATE,
    updated_at            TIMESTAMP DEFAULT NOW(),

    UNIQUE (entity_id, broker, symbol)
);
"""

CREATE_EQUITY_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS equity_holding_history (
    id                    SERIAL PRIMARY KEY,
    entity_id             INTEGER NOT NULL REFERENCES entity(id),
    broker                VARCHAR(20) NOT NULL,
    symbol                VARCHAR(50) NOT NULL,
    snapshot_date         DATE NOT NULL,

    quantity              NUMERIC(18, 4),
    close_price           NUMERIC(18, 4),
    market_value          NUMERIC(18, 2),
    cost                  NUMERIC(18, 2),

    UNIQUE (entity_id, broker, symbol, snapshot_date)
);
"""
