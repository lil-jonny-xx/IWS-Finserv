#!/usr/bin/env python3
"""
DB Migration — add the `charts` column to assistant_message (Jarvis Phase 3).

Stores the chart specs (render_chart) emitted for an assistant turn so the thread
re-renders its charts when a conversation is reopened. Idempotent; safe to re-run.
Run once: python workers/db_migrate_assistant_charts.py
"""
import os, sys
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def main():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    cur  = conn.cursor()

    try:
        cur.execute("""
            ALTER TABLE assistant_message
                ADD COLUMN IF NOT EXISTS charts JSONB
        """)
        print("assistant_message.charts column: OK")

        conn.commit()
        print("\n✅  Migration complete.")

    except Exception as e:
        conn.rollback()
        print(f"❌  Migration failed: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
