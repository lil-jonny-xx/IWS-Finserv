#!/usr/bin/env python3
"""
Rebuild the mf_transaction uniqueness key on balance_units instead of description.

The old key was (entity_id, security_id, folio_number, transaction_date, description,
units). `description` carries a registrar reference number that is not stable between
CAS generations, so the same transaction re-parsed with a reference appended reads as
a new row and the parser's ON CONFLICT DO NOTHING lets it in. A redemption landed
twice in production that way — identical units, NAV, amount and running balance,
differing only by a trailing reference — which threw the holding's ledger
reconciliation off by exactly one redemption.

balance_units is the better discriminator: a genuine pair of same-day transactions
with identical units and amount still carries two *different* running balances, so
they survive, while a re-parse of one transaction collides and is correctly dropped.
Descriptions stay stored, they just no longer decide identity.

Verified against live data before writing: zero rows collide under the new key.

Idempotent. Run before deploying the cas_parser_worker that relies on it.

    /var/www/.venv/bin/python /var/www/mis-portal/workers/db_migrate_mf_txn_dedup_key.py
"""
import logging
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

OLD_CONSTRAINT = "mf_transaction_entity_id_security_id_folio_number_transacti_key"
NEW_CONSTRAINT = "mf_transaction_dedup_key"


def main() -> int:
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    try:
        # Refuse rather than destroy: if live data collides under the new key, adding
        # it would fail mid-transaction anyway, and silently dropping rows to force it
        # through is never the right call for a transaction ledger.
        cur.execute("""
            SELECT count(*) FROM (
                SELECT 1 FROM mf_transaction
                GROUP BY entity_id, security_id, folio_number,
                         transaction_date, units, amount, balance_units
                HAVING count(*) > 1
            ) dupes
        """)
        collisions = cur.fetchone()[0]
        if collisions:
            logger.error(
                f"ABORT: {collisions} group(s) collide under the new key. "
                f"Resolve those duplicates first — this migration will not delete rows."
            )
            conn.rollback()
            return 1

        cur.execute(f'ALTER TABLE mf_transaction DROP CONSTRAINT IF EXISTS "{OLD_CONSTRAINT}"')
        logger.info(f"Dropped old constraint {OLD_CONSTRAINT} (if present)")

        cur.execute(f'ALTER TABLE mf_transaction DROP CONSTRAINT IF EXISTS "{NEW_CONSTRAINT}"')
        cur.execute(f"""
            ALTER TABLE mf_transaction
            ADD CONSTRAINT "{NEW_CONSTRAINT}"
            UNIQUE NULLS NOT DISTINCT
                (entity_id, security_id, folio_number,
                 transaction_date, units, amount, balance_units)
        """)
        logger.info(f"Added {NEW_CONSTRAINT} (NULLS NOT DISTINCT)")

        conn.commit()
        logger.info("Migration complete.")
        return 0

    except Exception as e:
        conn.rollback()
        logger.error(f"Migration FAILED, rolled back: {e}")
        return 1
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
