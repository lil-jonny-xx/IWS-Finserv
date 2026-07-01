#!/usr/bin/env python3
"""
DB migration — password_reset_request table.

Backs the admin-mediated "forgot password" flow (no email delivery): when a user
clicks "Forgot password?" at login and submits their email, a pending request is
recorded here (only if the email matches an active user — the API response is
always generic to avoid revealing which emails exist). An admin then sees the
pending requests on the /account page and resets that user's password.

Run once:  python -m workers.db_migrate_password_reset
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS password_reset_request (
    id           SERIAL PRIMARY KEY,
    email        VARCHAR(255) NOT NULL,
    user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
    status       VARCHAR(20) NOT NULL DEFAULT 'pending',   -- pending | resolved | dismissed
    requested_at TIMESTAMP NOT NULL DEFAULT NOW(),
    resolved_at  TIMESTAMP,
    resolved_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_prr_status ON password_reset_request (status);
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
        print("password_reset_request table ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
