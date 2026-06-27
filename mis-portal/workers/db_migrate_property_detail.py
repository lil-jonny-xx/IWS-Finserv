#!/usr/bin/env python3
"""
DB migration — property_detail table.

Real-estate entries live in manual_input (category 'properties') like every other
manual asset, but a property's value is driven by a hand-entered Ready Reckoner
(circle / guidance) rate rather than a single typed current value. This side-table
holds the admin-entered inputs that produce that value, keyed exactly like
art_detail — the STABLE logical identity (entity_id, label), independent of the
append-only manual_input versioning.

  location            — free-text locality the admin types ("Panaji, Goa")
  area_sqft           — built-up / saleable area
  ready_reckoner_rate — official RRR in ₹ per sq ft

From these the portal derives a value range (RRR x area x 1.5 .. x 2.0; the
midpoint x 1.75 feeds the portfolio total) — see /api/v1/property-detail. Only
the raw inputs are stored; the range is computed at read time so the multipliers
can be tuned in one place.

Run once:  python -m workers.db_migrate_property_detail
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS property_detail (
    id                  SERIAL PRIMARY KEY,
    entity_id           INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    label               TEXT        NOT NULL,
    location            TEXT,
    area_sqft           NUMERIC(14, 2),
    ready_reckoner_rate NUMERIC(14, 2),     -- INR per sq ft
    updated_by          INTEGER REFERENCES users(id),
    updated_at          TIMESTAMP DEFAULT NOW(),
    UNIQUE (entity_id, label)
);
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
        print("property_detail table ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
