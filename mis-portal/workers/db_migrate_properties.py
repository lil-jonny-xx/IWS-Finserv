#!/usr/bin/env python3
"""
DB migration — property register (properties page rework).

The Properties page moves off the generic manual-assets backbone onto a
dedicated register, because property holders do NOT follow the portal's
entity table: most parcels/buildings sit in company, LLP or trust names
(IWS Finser LLP, Imperial Northstar, family trusts, parent companies…).

  property_entity  — the holder universe for properties ONLY. Seeded from
                     the system entity table plus the known company/trust
                     holders; grp='parent' rows appear as sub-tabs under
                     the "Parent Companies" tab; admins can append custom
                     holders ("Others" tab) at runtime (is_custom=TRUE).
  property         — one row per land parcel / building. Fair value is NOT
                     stored: it is derived at read time as
                     area x rrr x 1.75 (same 1.75x midpoint the old
                     property_detail band used — RRR *is* the circle rate).
  property_document — uploaded checklist documents. stored_path is the file
                     served to the browser (converted to PDF where we can);
                     original_path keeps the as-uploaded file when it
                     differs (docx kept when no converter, AutoCAD .dwg…).
                     The doc_type slugs live in property_docs.py.

Run once:  python -m workers.db_migrate_properties
Re-running is safe (IF NOT EXISTS + ON CONFLICT DO NOTHING seeds).
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS property_entity (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    short_code TEXT,                          -- compact tab label (SHR, HDR, DMC…)
    grp        TEXT NOT NULL DEFAULT 'main' CHECK (grp IN ('main', 'parent')),
    is_custom  BOOLEAN NOT NULL DEFAULT FALSE, -- added via the "Others" tab
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS property (
    id               SERIAL PRIMARY KEY,
    name             TEXT NOT NULL,
    property_type    TEXT NOT NULL CHECK (property_type IN ('land', 'building')),
    holder_id        INTEGER NOT NULL REFERENCES property_entity(id),
    location         TEXT,
    taluka           TEXT,
    area             NUMERIC(14, 2),
    area_unit        TEXT DEFAULT 'sq m',     -- RRR is per this same unit
    deed_no          TEXT,
    acquisition_date DATE,
    ownership        TEXT,
    rrr              NUMERIC(16, 2),          -- circle rate, INR per area_unit
    notes            TEXT,
    created_by       INTEGER REFERENCES users(id),
    updated_by       INTEGER REFERENCES users(id),
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_property_holder ON property(holder_id);

-- Sale tracking (2026-07-13): a sold property moves to the page's "Sold"
-- section, stops contributing fair value, and its sale price feeds the
-- Realised Gains page + the overview instead.
ALTER TABLE property ADD COLUMN IF NOT EXISTS sold       BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE property ADD COLUMN IF NOT EXISTS sale_price NUMERIC(16, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS sale_date  DATE;

-- Purchase cost + hand-entered market value (2026-07-13). Fair value stays
-- derived (area x RRR x 1.75); market_value, when entered, supersedes it in
-- the overview. purchase_price gives sold properties a real P&L.
ALTER TABLE property ADD COLUMN IF NOT EXISTS purchase_price NUMERIC(16, 2);
ALTER TABLE property ADD COLUMN IF NOT EXISTS market_value   NUMERIC(16, 2);

-- Joint ownership: a property can be held by several entities with a % split.
-- property.holder_id remains the primary holder (largest share) for ordering.
CREATE TABLE IF NOT EXISTS property_owner (
    id          SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES property(id) ON DELETE CASCADE,
    holder_id   INTEGER NOT NULL REFERENCES property_entity(id),
    pct         NUMERIC(6, 2) NOT NULL DEFAULT 100,
    UNIQUE (property_id, holder_id)
);

-- Building floors: label + area per floor; floor plans attach to a floor via
-- property_document.floor_id (kept when a doc is a floor_plan).
CREATE TABLE IF NOT EXISTS property_floor (
    id          SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES property(id) ON DELETE CASCADE,
    floor_label TEXT NOT NULL,
    area        NUMERIC(14, 2),
    sort_order  INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE property_document
    ADD COLUMN IF NOT EXISTS floor_id INTEGER REFERENCES property_floor(id) ON DELETE SET NULL;

-- 2026-07-13: "deed no." renamed to the government's term, property number.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'property' AND column_name = 'deed_no') THEN
        ALTER TABLE property RENAME COLUMN deed_no TO property_no;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS property_document (
    id            SERIAL PRIMARY KEY,
    property_id   INTEGER NOT NULL REFERENCES property(id) ON DELETE CASCADE,
    doc_type      TEXT NOT NULL,              -- slug from property_docs.DOC_TYPES
    original_name TEXT,
    stored_path   TEXT NOT NULL,              -- served file (PDF when converted)
    original_path TEXT,                       -- as-uploaded file when != stored
    mime          TEXT,
    size_bytes    BIGINT,
    converted     BOOLEAN NOT NULL DEFAULT FALSE,
    uploaded_by   INTEGER REFERENCES users(id),
    uploaded_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_property_document_pid ON property_document(property_id);
"""

# (name, short_code, grp, sort_order) — system entities are appended first at
# runtime so the register always covers whatever the portal already knows.
SEED = [
    # "IWS Finser LLP" (typo) and "IWS Finserv" were the same holder — merged into
    # one canonical row on 2026-07-14.
    ("IWS Finserv LLP",             None,  "main",   20),
    ("Imperial Northstar Pvt. Ltd.", None, "main",   22),
    ("Imperial Northstar Exim LLP", None,  "main",   23),
    ("Sharmila Harish Rajani",      "SHR", "main",   24),
    # HDR became a system entity (entity table) on 2026-07-13 — his holder row
    # was renamed to "HDR" and is now mirrored at runtime like DHR/HHR/SDR.
    ("Rajani Foundation Trust",     None,  "main",   26),
    ("Harish & Sharmila Trust",     None,  "main",   27),
    ("DMC",                         None,  "parent", 50),
    ("DMMC",                        None,  "parent", 51),
    ("Rajani Trading",              None,  "parent", 52),
    ("GIPL",                        None,  "parent", 53),
    ("RKDJ Trust",                  None,  "parent", 54),
    ("RME",                         None,  "parent", 55),
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
            # Mirror the system entities (DHR, ADR, IWS…) into the holder list.
            cur.execute("SELECT entity_name FROM entity ORDER BY id")
            system = [(r[0], None, "main", i) for i, r in enumerate(cur.fetchall())]
            for name, code, grp, order in system + SEED:
                cur.execute(
                    """INSERT INTO property_entity (name, short_code, grp, sort_order)
                       VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING""",
                    (name, code, grp, order),
                )
        with conn.cursor() as cur:
            # Backfill: every pre-ownership-table property is 100% its holder's.
            cur.execute("""
                INSERT INTO property_owner (property_id, holder_id, pct)
                SELECT p.id, p.holder_id, 100 FROM property p
                WHERE NOT EXISTS (SELECT 1 FROM property_owner o WHERE o.property_id = p.id)
            """)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM property_entity")
            print(f"property tables ready; {cur.fetchone()[0]} holder entities seeded.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
