#!/usr/bin/env python3
"""
Migration for ledger ingestion:
  • one `account` row per (entity, broker) so cash_ledger has a stable account_id
    (cash_ledger is UNIQUE on (entity_id, account_id, balance_date)).
  • `external_cashflow` — classified non-trade cash movements parsed from broker
    ledgers: external deposits/withdrawals (for portfolio money-weighted return) and
    dividends/interest (to enrich holding XIRR). Trade settlements and pure charges
    are NOT stored here (trades live in stock_transaction; charges are noise for XIRR).
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

BROKERS = ["zerodha", "angel_one", "dhan", "vested", "ibkr", "dbs"]


def main():
    conn = psycopg2.connect(host=os.getenv("DB_HOST", "localhost"),
                            database=os.getenv("DB_NAME", "mis_portal"),
                            user=os.getenv("DB_USER", "postgres"),
                            password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""))
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS external_cashflow (
            id           BIGSERIAL PRIMARY KEY,
            entity_id    INTEGER NOT NULL REFERENCES entity(id),
            broker       VARCHAR(20) NOT NULL,
            flow_date    DATE NOT NULL,
            flow_type    VARCHAR(20) NOT NULL,      -- DEPOSIT | WITHDRAWAL | DIVIDEND | INTEREST
            symbol       VARCHAR(50),               -- set for DIVIDEND/INTEREST when stock-specific
            amount_native NUMERIC(18,2) NOT NULL,   -- +inflow to investor / -outflow, in account ccy
            currency     VARCHAR(3) NOT NULL DEFAULT 'INR',
            source       VARCHAR(20),
            source_ref   VARCHAR(80) UNIQUE,        -- dedupe key
            notes        TEXT,
            created_at   TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_extcf_entity_broker ON external_cashflow(entity_id, broker, flow_date);")

    # One account per (entity, broker)
    cur.execute("SELECT id, entity_name FROM entity")
    for eid, name in cur.fetchall():
        for broker in BROKERS:
            cur.execute("""
                INSERT INTO account (entity_id, account_type, account_name, account_number, currency, created_at)
                SELECT %s, 'BROKER', %s, %s, 'INR', NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM account WHERE entity_id=%s AND account_type='BROKER' AND account_name=%s)
            """, (eid, broker, f"{name}-{broker}", eid, broker))

    conn.commit()
    cur.execute("SELECT count(*) FROM account")
    print(f"account rows: {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM external_cashflow")
    print(f"external_cashflow rows: {cur.fetchone()[0]}")
    cur.close(); conn.close()
    print("migration done.")


if __name__ == "__main__":
    main()
