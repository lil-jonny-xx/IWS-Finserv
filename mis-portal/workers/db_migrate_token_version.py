#!/usr/bin/env python3
"""
DB migration — users.token_version (session-revocation counter).

A monotonically increasing per-user counter. It is embedded in the JWT at login;
_require_auth rejects any token whose claimed version is behind the row's current
value. Bumping it (on self password change or admin reset) invalidates every
outstanding token for that user immediately, closing the window where a rotated
credential's old 8-hour session stayed live until its natural expiry.

Idempotent (ADD COLUMN IF NOT EXISTS). Existing tokens carry no token_version claim
and are treated as version 0, which matches the column default — so this migration
does NOT force a mass re-login; only a future bump invalidates a given user's old
sessions.

Run once:  python -m workers.db_migrate_token_version
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0;
"""


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        print("users.token_version column ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
