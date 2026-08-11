#!/usr/bin/env python3
"""
DB migration — SDR ornaments register (Jewellery / Gold / Silver).

A private, single-entity register for physical ornaments. Deliberately NOT part
of manual_input: those rows are versioned and keyed by (entity_id, category,
label), which suits a hand-typed valuation but not a per-piece inventory whose
items get renamed, photographed and weighed. Ornaments are real rows with stable
ids, so photos hang off a foreign key and vanish with the piece (ON DELETE
CASCADE) instead of being orphaned by a rename.

Also deliberately NOT wired into manual_input / the Overview totals: the register
is private to its owner (see ORNAMENTS_ENTITY_ID in main.py), and folding the
values into the shared net-worth figures would work against that.

  ornament        one row per piece, across the three categories.
  ornament_photo  images for a piece; files live under
                  /var/www/uploads/ornaments/<entity_id>/<category>/ and the
                  row holds path + metadata only (same scheme as
                  manual_attachment — see db_migrate_manual_attachments.py).

Run once:  python -m workers.db_migrate_ornaments
Re-running is safe (IF NOT EXISTS throughout).
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

UPLOADS_ROOT = os.getenv("UPLOADS_DIR", "/var/www/uploads")

DDL = """
CREATE TABLE IF NOT EXISTS ornament (
    id                SERIAL PRIMARY KEY,
    entity_id         INTEGER      NOT NULL REFERENCES entity(id),
    category          VARCHAR(16)  NOT NULL,   -- jewellery | gold | silver
    metal             VARCHAR(16)  DEFAULT 'gold',  -- gold | silver | platinum | other

    -- identification
    serial_no         TEXT,
    code              TEXT,
    given_name        TEXT,                    -- the owner's own name for the piece
    declared_name     TEXT,                    -- name as declared on paperwork
    item_type         TEXT,                    -- ring / bangle / coin / bar / ...

    -- physical
    gross_weight_g    NUMERIC(12,3),
    metal_weight_g    NUMERIC(12,3),           -- gold (or silver) content weight
    purity            TEXT,                    -- 22K / 916 / 999 / 925 ...
    stones_carat      NUMERIC(10,3),           -- precious + semi-precious, total ct
    stones_note       TEXT,                    -- which stones, setting notes

    -- bullion-specific (coins / bars); null for jewellery
    quantity          INTEGER      DEFAULT 1,  -- identical pieces on one row
    mint              TEXT,                    -- MMTC-PAMP, PAMP Suisse, Royal Mint...
    year_minted       INTEGER,
    assay_no          TEXT,                    -- assay-card / certificate serial
    denomination      TEXT,                    -- face value, for sovereign coins
    sealed            BOOLEAN,                 -- still in its assay packaging

    -- valuation (hand-entered; authoritative)
    valuation         NUMERIC(16,2),
    valuation_remark  TEXT,
    valuation_date    DATE,

    -- purchase
    purchased_from    TEXT,
    invoice_no        TEXT,
    purchase_date     DATE,
    purchase_price    NUMERIC(16,2),

    notes             TEXT,
    sort_order        INTEGER      DEFAULT 0,
    created_by        INTEGER,
    created_at        TIMESTAMP    DEFAULT NOW(),
    updated_by        INTEGER,
    updated_at        TIMESTAMP    DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ornament_entity_cat
    ON ornament (entity_id, category, sort_order, id);

CREATE TABLE IF NOT EXISTS ornament_photo (
    id            SERIAL PRIMARY KEY,
    ornament_id   INTEGER NOT NULL REFERENCES ornament(id) ON DELETE CASCADE,
    original_name TEXT,
    stored_path   TEXT    NOT NULL,
    thumb_path    TEXT,
    mime          TEXT,
    size_bytes    BIGINT,
    uploaded_by   INTEGER,
    uploaded_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ornament_photo_ornament
    ON ornament_photo (ornament_id, uploaded_at);
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
        os.makedirs(os.path.join(UPLOADS_ROOT, "ornaments"), exist_ok=True)
        print("ornament + ornament_photo ready; uploads/ornaments/ created.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
