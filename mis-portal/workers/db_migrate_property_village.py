#!/usr/bin/env python3
"""
DB migration — fold property.location into property.village ("City/Village").

Why: the register grew `village` in v2 (for the Bhunaksha land-record lookup) on
top of the original free-text `location`, and the two ended up meaning the same
thing to whoever fills the form. In practice only one was ever used: every row
had `location` filled and `village` empty. Two fields for one idea is what made
the form confusing, so `location` retires and `village` is the survivor, shown
first and labelled "City/Village".

Data: `location` holds the real values, so it is COPIED into `village` before the
column is dropped — village only wins where it is actually empty, so any row
someone did fill in by hand is left alone.

Note the values are localities ("BAINA, VASCO", "MACHADOS COVE , DONA PAULA"),
not strict revenue-village names. That is fine for display, but the Bhunaksha
link keys off `village`, so a lookup may not resolve until the value is tidied to
the revenue village. Nothing breaks either way — the link is a search hint.

NOT touched (different tables, same column name — do not confuse them):
  art_detail.location       — where an Art/Collectibles piece is kept
  property_detail.location  — free-text locality on the Ready-Reckoner inputs

Idempotent: safe to re-run; the copy is a no-op once `location` is gone.

Run once:  python -m workers.db_migrate_property_village
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

# Guarded on the column still existing so a re-run after the drop is a clean no-op.
MIGRATE = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'property' AND column_name = 'location'
    ) THEN
        UPDATE property
           SET village = location
         WHERE (village IS NULL OR btrim(village) = '')
           AND location IS NOT NULL AND btrim(location) <> '';

        ALTER TABLE property DROP COLUMN location;
    END IF;
END $$;
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
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name='property' AND column_name='location'"
            )
            had_location = cur.fetchone()[0] > 0
            if had_location:
                cur.execute(
                    "SELECT COUNT(*) FROM property "
                    "WHERE (village IS NULL OR btrim(village)='') "
                    "  AND location IS NOT NULL AND btrim(location) <> ''"
                )
                to_copy = cur.fetchone()[0]
            else:
                to_copy = 0

            cur.execute(MIGRATE)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(village) FROM property")
            total, with_village = cur.fetchone()
        if had_location:
            print(f"property.location -> village: copied {to_copy} value(s), column dropped. "
                  f"{with_village}/{total} rows now have a City/Village.")
        else:
            print(f"property.location already retired; {with_village}/{total} rows have a City/Village.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
