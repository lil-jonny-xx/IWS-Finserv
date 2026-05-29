#!/usr/bin/env python3
"""
DB Migration — Add new PANs and entities for Harsh and Stuti.
Run once: python workers/db_migrate_entities.py

New pan_groups: PAN_2 (Harsh), PAN_3 (Stuti)
New entities:
  - HHR  (***REMOVED***)  → PAN_2
  - IWSFC (IWS Fincorp)         → PAN_2
  - SDR  (***REMOVED***)   → PAN_3
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env")

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
        # --- pan_group ---
        cur.execute("""
            INSERT INTO pan_group (pan_name)
            VALUES ('PAN_2'), ('PAN_3')
            ON CONFLICT (pan_name) DO NOTHING
            RETURNING id, pan_name
        """)
        inserted_groups = cur.fetchall()
        print(f"pan_group rows inserted: {inserted_groups}")

        # Fetch IDs (may already exist if re-run)
        cur.execute("SELECT id, pan_name FROM pan_group WHERE pan_name IN ('PAN_2','PAN_3')")
        groups = {r["pan_name"]: r["id"] for r in cur.fetchall()}
        pan2_id = groups["PAN_2"]
        pan3_id = groups["PAN_3"]
        print(f"PAN_2 id={pan2_id}, PAN_3 id={pan3_id}")

        # --- entities ---
        new_entities = [
            ("HHR",   "***REMOVED***", pan2_id),
            ("IWSFC", "IWS Fincorp",         pan2_id),
            ("SDR",   "***REMOVED***",  pan3_id),
        ]

        for code, full_name, pg_id in new_entities:
            cur.execute("""
                INSERT INTO entity (entity_name, pan_group_id)
                VALUES (%s, %s)
                ON CONFLICT (entity_name) DO NOTHING
                RETURNING id, entity_name
            """, (code, pg_id))
            row = cur.fetchone()
            if row:
                print(f"Entity added: id={row['id']} code={code} full_name={full_name}")
            else:
                cur.execute("SELECT id FROM entity WHERE entity_name = %s", (code,))
                existing = cur.fetchone()
                print(f"Entity already exists: code={code} id={existing['id']}")

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
