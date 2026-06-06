#!/usr/bin/env python3
"""
DB Migration — manual_input and generated_report tables.
Run once: python workers/db_migrate_manual_reports.py
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
            CREATE TABLE IF NOT EXISTS manual_input (
                id              SERIAL PRIMARY KEY,
                entity_id       INTEGER NOT NULL REFERENCES entity(id),
                category        VARCHAR(60)  NOT NULL,
                label           VARCHAR(200) NOT NULL,
                cost            NUMERIC(20,2),
                current_value   NUMERIC(20,2),
                prev_week_value NUMERIC(20,2),
                currency        VARCHAR(10) NOT NULL DEFAULT 'INR',
                raw_amount      NUMERIC(20,4),
                fx_rate         NUMERIC(15,6),
                inception_date  DATE,
                notes           TEXT,
                updated_by      INTEGER REFERENCES users(id),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        print("manual_input table: OK")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_manual_input_entity
                ON manual_input (entity_id, category, label)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS generated_report (
                id              SERIAL PRIMARY KEY,
                report_type     VARCHAR(20) NOT NULL,
                entity_id       INTEGER REFERENCES entity(id),
                entity_name     VARCHAR(100),
                filename        VARCHAR(300) NOT NULL,
                filepath        VARCHAR(500) NOT NULL,
                as_of_date      DATE NOT NULL,
                generated_by    INTEGER REFERENCES users(id),
                generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        print("generated_report table: OK")

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
