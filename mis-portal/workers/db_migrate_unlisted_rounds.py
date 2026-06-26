#!/usr/bin/env python3
"""
DB migration — unlisted / startup funding rounds + corporate events (Phase 3).

Adds:
  * `unlisted_round` — one row per funding round for an unlisted-equity / startup
    holding: round name, date, price-per-share, shares acquired and amount
    invested. The aggregate cost / current value still live on the latest
    `manual_input` row (written by the rounds-save endpoint) so Overview and the
    manual list keep working unchanged.
  * `unlisted_event` — corporate actions (stock split / bonus issue) that adjust
    the share count. `factor` is the share multiplier (2:1 split or 1:1 bonus →
    2.0; 3:2 → 1.5). Per-share price divides by the same factor, so total value
    is unchanged — we only track these to keep share counts and per-share figures
    correct in the breakdown.

Keying mirrors manual_attachment / art_detail: the STABLE logical identity
(entity_id, category, label) — manual_input ids are not stable across edits.
category is one of {unlisted, startup}.

Idempotent — safe to run repeatedly.

  /var/www/mis-portal/venv/bin/python -m workers.db_migrate_unlisted_rounds
"""
import os
import sys
import logging
import psycopg2

from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DDL = [
    (
        "create unlisted_round",
        """
        CREATE TABLE IF NOT EXISTS unlisted_round (
            id              SERIAL PRIMARY KEY,
            entity_id       INTEGER NOT NULL REFERENCES entity(id),
            category        VARCHAR(40) NOT NULL,          -- unlisted | startup
            label           TEXT        NOT NULL,          -- company name
            round_name      TEXT,                          -- Seed / Series A / ...
            round_date      DATE,
            price_per_share NUMERIC(20,6),
            shares          NUMERIC(20,4),
            amount_invested NUMERIC(20,2),
            notes           TEXT,
            sort_order      INTEGER DEFAULT 0,
            created_by      INTEGER REFERENCES users(id),
            created_at      TIMESTAMP DEFAULT NOW()
        );
        """,
    ),
    (
        "unlisted_round: index on (entity_id, category, label)",
        "CREATE INDEX IF NOT EXISTS idx_unlisted_round_asset "
        "ON unlisted_round(entity_id, category, label);",
    ),
    (
        "create unlisted_event",
        """
        CREATE TABLE IF NOT EXISTS unlisted_event (
            id          SERIAL PRIMARY KEY,
            entity_id   INTEGER NOT NULL REFERENCES entity(id),
            category    VARCHAR(40) NOT NULL,              -- unlisted | startup
            label       TEXT        NOT NULL,
            event_type  VARCHAR(10) NOT NULL,              -- split | bonus
            event_date  DATE,
            factor      NUMERIC(12,6) NOT NULL,            -- share multiplier (2:1 -> 2.0)
            ratio_text  VARCHAR(20),                       -- display, e.g. '2:1'
            notes       TEXT,
            sort_order  INTEGER DEFAULT 0,
            created_by  INTEGER REFERENCES users(id),
            created_at  TIMESTAMP DEFAULT NOW()
        );
        """,
    ),
    (
        "unlisted_event: index on (entity_id, category, label)",
        "CREATE INDEX IF NOT EXISTS idx_unlisted_event_asset "
        "ON unlisted_event(entity_id, category, label);",
    ),
]


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def main():
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    for name, sql in DDL:
        try:
            cur.execute(sql)
            conn.commit()
            logger.info(f"✅  {name}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌  {name}: {e}")
            sys.exit(1)

    cur.close()
    conn.close()
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
