#!/usr/bin/env python3
"""
DB migration — property.market_value -> property.market_land_value.

The old `market_value` was ambiguous: nothing said whether the figure you typed
covered the land only or the whole property. The register adds the summed floor
costings on top of it either way, so a whole-property figure silently
double-counted the building.

Renaming makes the contract explicit — the column now holds the LAND value only,
exactly like the RRR-derived fair value it falls back to, and the displayed total
is always `land + floors`. Existing values are carried over untouched: they were
already land-only for every row entered under the RRR convention, and any row
that wasn't needs a human to restate it (see the audit query printed at the end).

Idempotent: safe to re-run, and a no-op once the column is already renamed.

Run once:  python -m workers.db_migrate_property_market_land_value
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'property' AND column_name = 'market_value')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'property' AND column_name = 'market_land_value')
    THEN
        ALTER TABLE property RENAME COLUMN market_value TO market_land_value;
        RAISE NOTICE 'renamed property.market_value -> market_land_value';
    END IF;
END $$;

-- Covers a fresh database that never had the old column.
ALTER TABLE property ADD COLUMN IF NOT EXISTS market_land_value NUMERIC(16, 2);
"""

# Rows worth a second look after the rename: a hand-entered land value on a
# property that also carries priced floors is exactly the case where the old
# ambiguity could have hidden a double-count.
AUDIT = """
SELECT p.id, p.name, p.market_land_value,
       SUM(COALESCE(f.built_up_area, f.area) * f.rate_per_unit) AS floors_value
FROM   property p
JOIN   property_floor f ON f.property_id = p.id AND f.rate_per_unit IS NOT NULL
WHERE  p.market_land_value IS NOT NULL
GROUP  BY p.id, p.name, p.market_land_value
ORDER  BY p.name
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
        with conn.cursor() as cur:
            cur.execute(AUDIT)
            rows = cur.fetchall()
        print("property.market_land_value ready.")
        if rows:
            print(f"\n{len(rows)} propert{'y' if len(rows) == 1 else 'ies'} have both a "
                  f"land value and priced floors — confirm the land figure excludes "
                  f"the building:\n")
            for pid, name, land, floors in rows:
                print(f"  #{pid:<4} {name[:38]:<38} land {land:>14,.2f}   "
                      f"floors {floors:>14,.2f}   total {land + floors:>14,.2f}")
        else:
            print("No properties combine a land value with priced floors — nothing to review.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
