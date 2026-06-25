#!/usr/bin/env python3
"""
DB migration — bank_account + bank_statement tables.

Bank accounts are pure cash holdings in a native currency — no securities, no
price feed. Unlike brokers (Vested/IBKR/DBS Wealth), retail/private banks expose
no usable API and their portals sit behind per-login OTP/MFA, so balances are
NOT scraped. Instead an admin uploads a downloaded statement (PDF / CSV / Excel),
the parser extracts a best-guess closing balance, the admin confirms it, and the
balance is stored here. A plain manual balance entry (no file) also works.

Ownership is strictly one entity per account (a jointly-held "UK HSBC (DHR & HHR)"
becomes two separate rows — one per entity), matching broker_cash.

  bank_account   — one row per (entity, bank, account_type); the current balance.
  bank_statement — upload history; each row is one uploaded file + what we parsed
                   from it. Kept for audit / re-parsing; the committed balance
                   lives on bank_account.

Native → INR conversion happens at read time via the fx_rate table (same as
foreign equity / manual inputs), so only the native balance is stored.

Run once:  python -m workers.db_migrate_bank_accounts
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS bank_account (
    id            SERIAL PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    bank_name     VARCHAR(120) NOT NULL,            -- "UK HSBC", "First Abu Dhabi Bank"
    account_type  VARCHAR(20)  NOT NULL DEFAULT 'savings',  -- savings | current | nre | other
    currency      VARCHAR(3)   NOT NULL,            -- GBP | AED | SGD | USD | INR ...
    balance       NUMERIC(20, 2) NOT NULL DEFAULT 0,  -- native currency
    balance_as_of DATE,                             -- statement / entry date
    notes         TEXT,
    created_at    TIMESTAMP DEFAULT NOW(),
    updated_at    TIMESTAMP DEFAULT NOW(),
    updated_by    INTEGER REFERENCES users(id),
    UNIQUE (entity_id, bank_name, account_type)
);
CREATE INDEX IF NOT EXISTS idx_bank_account_entity ON bank_account (entity_id);

CREATE TABLE IF NOT EXISTS bank_statement (
    id              SERIAL PRIMARY KEY,
    bank_account_id INTEGER NOT NULL REFERENCES bank_account(id) ON DELETE CASCADE,
    filename        VARCHAR(255) NOT NULL,          -- original upload name
    filepath        TEXT NOT NULL,                  -- stored path on disk
    file_kind       VARCHAR(10),                    -- pdf | csv | xlsx
    parsed_balance  NUMERIC(20, 2),                 -- best-guess from the file (native ccy)
    parsed_as_of    DATE,                           -- best-guess statement date
    parse_status    VARCHAR(20) NOT NULL DEFAULT 'pending',  -- parsed | needs_review | failed
    parse_note      TEXT,                           -- why it failed / what we matched
    committed       BOOLEAN NOT NULL DEFAULT FALSE, -- did the admin confirm this into bank_account.balance?
    uploaded_at     TIMESTAMP DEFAULT NOW(),
    uploaded_by     INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_bank_statement_account ON bank_statement (bank_account_id);
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
        print("bank_account + bank_statement tables ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
