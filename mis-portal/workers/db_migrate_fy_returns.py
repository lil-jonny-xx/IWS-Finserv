#!/usr/bin/env python3
"""
DB migration — financial-year returns for Equity + Mutual Funds.

Adds the storage behind the "FY growth" columns: a completed-FY return per
holding (FY23-24, FY24-25, FY25-26 …) alongside the existing current-year
pnl_ytd / returns_ytd_pct, which stays exactly as it is.

  security_price_history
    Daily closes keyed by the RESOLVED Yahoo ticker, not our broker symbol —
    the same stock arrives as GOLDBEES (zerodha) and GOLDBEES-EQ (angel_one),
    and both resolve to GOLDBEES.NS, so keying on the ticker dedups the rows
    and one fetch serves every broker. Only the 31-Mar boundary closes are
    needed today, but the table is a plain daily series so it can widen later
    without a migration.

    Why Yahoo and not our own equity_holding_history: that table only starts
    2026-04-01 and even then covers 119 of 225 holdings, so it can anchor the
    current FY and nothing before it. Yahoo covers every boundary uniformly
    (verified: 163/170 symbols resolve; the 7 that don't are SME-board listings
    and a Sovereign Gold Bond, ~2.55% of Indian equity value, which report NULL
    rather than a guess).

  equity_holding.fy_returns          JSONB
  holding.fy_returns                 JSONB
  foreign_equity_holding.fy_returns  JSONB
    {"2025-26": {"pnl": 12345.67, "pct": 8.9}, "2024-25": {...}}
    JSONB rather than flat columns because the set of years rolls forward every
    April — pnl_fy1/pnl_fy2 would need a migration (and a backfill of meaning)
    every year, and the labels would silently shift under stored data.
    NULL / missing year = we cannot compute it honestly (holding didn't exist,
    ledger doesn't reach back, or no price at the boundary) — never 0, which
    would read as "flat".

Idempotent. Run:  python -m workers.db_migrate_fy_returns
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS security_price_history (
    yahoo_symbol  TEXT    NOT NULL,
    price_date    DATE    NOT NULL,
    close         NUMERIC(18, 4) NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'yahoo',
    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (yahoo_symbol, price_date)
);
CREATE INDEX IF NOT EXISTS idx_sph_date ON security_price_history (price_date);

-- Corporate actions, needed as a CORRECTNESS GATE rather than for maths.
-- Yahoo restates every historical close for later splits/bonuses; our ledger
-- stores fills at the raw price they executed at (while its quantities are on
-- the current post-split basis — an inconsistent basis of our own). Comparing a
-- raw in-year fill against an adjusted anchor manufactures nonsense: Canara Bank
-- read -79.7% for FY23-24, which is purely its 1:5 split. Any FY with an in-year
-- lot older than a split is therefore reported NULL instead.
CREATE TABLE IF NOT EXISTS security_split (
    yahoo_symbol  TEXT    NOT NULL,
    split_date    DATE    NOT NULL,
    ratio         NUMERIC(12, 6) NOT NULL,
    PRIMARY KEY (yahoo_symbol, split_date)
);

-- Resolution cache: broker symbol -> Yahoo ticker. Kept so the nightly job
-- doesn't re-probe Yahoo for tickers it already knows, and so an unresolvable
-- symbol is remembered as unresolvable (resolved_symbol NULL) instead of
-- being retried on every run.
CREATE TABLE IF NOT EXISTS security_symbol_map (
    symbol           TEXT PRIMARY KEY,
    exchange         TEXT,
    resolved_symbol  TEXT,
    checked_at       TIMESTAMP NOT NULL DEFAULT NOW()
);

ALTER TABLE equity_holding ADD COLUMN IF NOT EXISTS fy_returns JSONB;
ALTER TABLE holding        ADD COLUMN IF NOT EXISTS fy_returns JSONB;
-- foreign_equity_holding is NOT fed by fy_returns_worker yet, so this stays NULL and
-- the FY columns read blank on the Foreign Equity page. The column still has to EXIST:
-- main._EQUITY_HOLDING_COLS selects eh.fy_returns and is shared by the equity, foreign
-- equity and gold/silver (UNION of both tables) queries. Without it, the foreign and
-- gold/silver endpoints raise UndefinedColumn and 500 — which is exactly what they did
-- between the fy-returns feature landing and 2026-07-17, leaving both tabs blank.
ALTER TABLE foreign_equity_holding ADD COLUMN IF NOT EXISTS fy_returns JSONB;
"""


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM security_price_history")
            prices = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM security_symbol_map")
            maps = cur.fetchone()[0]
        print(f"FY returns ready; security_price_history={prices} rows, "
              f"security_symbol_map={maps} rows, fy_returns column on equity_holding + holding.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
