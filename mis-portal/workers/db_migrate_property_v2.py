#!/usr/bin/env python3
"""
DB migration — property register v2 (rich property record + gallery).

Builds on db_migrate_properties.py. All additive / idempotent:

  property (new scalars)
    address, built_up_area, survey_no, village, gps_link (manual Maps override),
    tenure ('freehold'|'leasehold'), is_old_lease (statutory pre-1990 rent-
    controlled lease — routed to the page's Lease section, market value halved),
    has_parking + parking_count, seller_name/seller_address, stamp_value,
    lawyer_fees. property_type CHECK widened to land/building/apartment/godown/
    shop ("building-like" = everything but land: gets floors + building docs).

  property_nature_type / property_nature
    Nature (industrial, orchard, agriculture, commercial, residential,
    settlement, non-settlement, + admin-added customs) is multi-valued with a
    per-nature AREA split — mirrors property_owner's pct split and the Parent
    Companies custom-entity pattern.

  property_floor (new cols)
    rate_per_unit, built_up_area, carpet_area, is_rented, rent_amount, tenant.
    Floor value = COALESCE(built_up_area, area) x rate_per_unit.

  property_image
    Airbnb-style gallery, separate from the document checklist. thumb_path via
    the same PIL path as manual_attachment; is_hero marks the cover.

  property_document.custom_label
    Optional admin-supplied name; the UI labels docs by class (+ index) or this,
    never the real filename.

Run once:  python -m workers.db_migrate_property_v2
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
-- ---- property: new scalar columns -------------------------------------------
ALTER TABLE property ADD COLUMN IF NOT EXISTS address        TEXT;
ALTER TABLE property ADD COLUMN IF NOT EXISTS built_up_area  NUMERIC(14, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS survey_no      TEXT;
ALTER TABLE property ADD COLUMN IF NOT EXISTS village        TEXT;
ALTER TABLE property ADD COLUMN IF NOT EXISTS gps_link       TEXT;
ALTER TABLE property ADD COLUMN IF NOT EXISTS tenure         TEXT;
ALTER TABLE property ADD COLUMN IF NOT EXISTS is_old_lease   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE property ADD COLUMN IF NOT EXISTS has_parking    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE property ADD COLUMN IF NOT EXISTS parking_count  INTEGER;
ALTER TABLE property ADD COLUMN IF NOT EXISTS seller_name    TEXT;
ALTER TABLE property ADD COLUMN IF NOT EXISTS seller_address TEXT;
ALTER TABLE property ADD COLUMN IF NOT EXISTS stamp_value    NUMERIC(16, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS lawyer_fees    NUMERIC(16, 2);

-- tenure enum (nullable — freehold/leasehold; NULL = unspecified)
ALTER TABLE property DROP CONSTRAINT IF EXISTS property_tenure_check;
ALTER TABLE property ADD  CONSTRAINT property_tenure_check
    CHECK (tenure IS NULL OR tenure IN ('freehold', 'leasehold'));

-- widen property_type: land + four building-like types
ALTER TABLE property DROP CONSTRAINT IF EXISTS property_property_type_check;
ALTER TABLE property ADD  CONSTRAINT property_property_type_check
    CHECK (property_type IN ('land', 'building', 'apartment', 'godown', 'shop'));

-- ---- nature (multi-value + area split + custom types) -----------------------
CREATE TABLE IF NOT EXISTS property_nature_type (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    is_custom  BOOLEAN NOT NULL DEFAULT FALSE,   -- admin-added at runtime
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS property_nature (
    id          SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES property(id) ON DELETE CASCADE,
    nature_id   INTEGER NOT NULL REFERENCES property_nature_type(id),
    area        NUMERIC(14, 2),                  -- in the property's area_unit
    UNIQUE (property_id, nature_id)
);
CREATE INDEX IF NOT EXISTS idx_property_nature_pid ON property_nature(property_id);

-- ---- property_floor: economics + tenancy ------------------------------------
ALTER TABLE property_floor ADD COLUMN IF NOT EXISTS rate_per_unit NUMERIC(16, 2);
ALTER TABLE property_floor ADD COLUMN IF NOT EXISTS built_up_area NUMERIC(14, 2);
ALTER TABLE property_floor ADD COLUMN IF NOT EXISTS carpet_area   NUMERIC(14, 2);
ALTER TABLE property_floor ADD COLUMN IF NOT EXISTS is_rented     BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE property_floor ADD COLUMN IF NOT EXISTS rent_amount   NUMERIC(16, 2);
ALTER TABLE property_floor ADD COLUMN IF NOT EXISTS tenant        TEXT;

-- ---- property_image: gallery ------------------------------------------------
CREATE TABLE IF NOT EXISTS property_image (
    id          SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES property(id) ON DELETE CASCADE,
    stored_path TEXT NOT NULL,
    thumb_path  TEXT,
    caption     TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_hero     BOOLEAN NOT NULL DEFAULT FALSE,
    mime        TEXT,
    size_bytes  BIGINT,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_property_image_pid ON property_image(property_id);

-- ---- property_document: optional custom name --------------------------------
ALTER TABLE property_document ADD COLUMN IF NOT EXISTS custom_label TEXT;
"""

# The seven fixed natures (custom ones are appended by admins at runtime).
NATURE_SEED = [
    ("Industrial",      10),
    ("Orchard",         20),
    ("Agriculture",     30),
    ("Commercial",      40),
    ("Residential",     50),
    ("Settlement",      60),
    ("Non-settlement",  70),
]


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
            for name, order in NATURE_SEED:
                cur.execute(
                    """INSERT INTO property_nature_type (name, is_custom, sort_order)
                       VALUES (%s, FALSE, %s) ON CONFLICT (name) DO NOTHING""",
                    (name, order),
                )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM property_nature_type")
            print(f"property v2 ready; {cur.fetchone()[0]} nature types seeded.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
