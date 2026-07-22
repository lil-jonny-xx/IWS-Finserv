#!/usr/bin/env python3
"""
Dividend tracking — computed per holding, validated against a broker report.

WHY A DEDICATED TABLE (and not external_cashflow)
--------------------------------------------------
external_cashflow records cash that MOVED THROUGH THE BROKER. Indian dividends do
not: since DDT was abolished in 2020 the company credits the shareholder's bank
account directly, so an 8-year Zerodha ledger contains literally zero dividend rows
(verified 2026-07-21) and Kite Connect exposes no dividend endpoint. There is nothing
to read from the broker, so the amount has to be DERIVED — ex-date and rate per share
from a market-data feed, multiplied by the quantity the ledger says was held on that
date. That is a computed estimate with its own provenance and its own error modes, so
it gets its own table rather than being mixed into recorded cash movements.
(Vested/US dividends DO settle inside the brokerage account and keep living in
external_cashflow — they are recorded fact, not derived.)

TWO SOURCES, ONE TABLE
----------------------
  source='computed' — nightly, from market data x ledger replay. Automatic, complete
                      as far back as the ledger goes, but GROSS and only as good as
                      both inputs.
  source='broker'   — a periodically imported broker dividend report (Zerodha Console).
                      Authoritative for the period it covers.
A UNIQUE key on (entity, security, ex_date, source) lets both coexist for the same
event so they can be compared; `variance_pct` on the computed row is filled in when a
broker row contradicts it, which is the whole point of the monthly validation pass.

KNOWN ERROR MODES — deliberately recorded, not hidden
-----------------------------------------------------
  * GROSS, not received. Dividends over Rs 5,000/yr attract 10% TDS, so the bank
    credit is smaller than the computed figure.
  * Ledger-dependent. Early years understate wherever trade history is incomplete
    (HDR pre-2024, the NRI off-market transfers) — the same limitation that the
    'reconstructed' plugs paper over for cost basis.
  * Feed coverage is partial. SME scrips, SGBs (which pay interest, not dividends)
    and renamed tickers have no Yahoo data; `dividend_coverage` records exactly which
    securities resolved and which did not, so the gap is visible on the page instead
    of silently reading as "no dividends".

    python -m workers.db_migrate_dividends            # dry-run
    python -m workers.db_migrate_dividends --commit
"""
import os
import sys
import argparse
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS dividend (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity(id),
    security_id     INTEGER NOT NULL REFERENCES security_master(id),
    ex_date         DATE    NOT NULL,
    pay_date        DATE,
    -- Dividends accrue to the shareholder, not to a demat account, so this is NOT
    -- split per broker: quantity is the entity's whole position on the ex-date.
    quantity        NUMERIC(18,4)  NOT NULL,
    rate_per_share  NUMERIC(18,6)  NOT NULL,
    amount          NUMERIC(18,2)  NOT NULL,
    currency        VARCHAR(3)     NOT NULL DEFAULT 'INR',
    fy              VARCHAR(7)     NOT NULL,   -- Indian FY label, e.g. '2026-27'
    source          VARCHAR(16)    NOT NULL,   -- 'computed' | 'broker' | 'manual'
    feed            VARCHAR(24),               -- e.g. 'yfinance', 'zerodha_console'
    variance_pct    NUMERIC(10,4),             -- computed vs broker, set by validation
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    CONSTRAINT dividend_natural_key UNIQUE (entity_id, security_id, ex_date, source)
);
CREATE INDEX IF NOT EXISTS idx_dividend_entity_fy ON dividend (entity_id, fy);
CREATE INDEX IF NOT EXISTS idx_dividend_exdate    ON dividend (ex_date);

-- Which securities the market-data feed could and could not resolve. Without this a
-- missing ticker is indistinguishable from a company that simply pays no dividend,
-- and the page would under-report with no way to tell.
CREATE TABLE IF NOT EXISTS dividend_coverage (
    security_id     INTEGER PRIMARY KEY REFERENCES security_master(id),
    symbol          VARCHAR(50),
    yahoo_ticker    VARCHAR(40),               -- resolved ticker, NULL if unresolved
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    events_found    INTEGER NOT NULL DEFAULT 0,
    last_checked    TIMESTAMP,
    note            TEXT                       -- why it failed / manual override reason
);
"""


def main():
    ap = argparse.ArgumentParser(description="Create dividend + dividend_coverage tables.")
    ap.add_argument("--commit", action="store_true", help="apply (default dry-run)")
    args = ap.parse_args()

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
    )
    cur = conn.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
                    WHERE table_name IN ('dividend','dividend_coverage')""")
    existing = {r[0] for r in cur.fetchall()}
    print(f"existing: {sorted(existing) or 'none'}")

    if not args.commit:
        print("\nDRY RUN — would run:\n" + DDL)
        return

    cur.execute(DDL)
    conn.commit()
    cur.execute("""SELECT table_name FROM information_schema.tables
                    WHERE table_name IN ('dividend','dividend_coverage')""")
    print("created/verified:", sorted(r[0] for r in cur.fetchall()))
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
