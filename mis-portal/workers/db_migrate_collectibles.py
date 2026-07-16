#!/usr/bin/env python3
"""
DB migration — Art/Collectibles split.

The existing "Art" holdings are really collectibles. This migration:

  1. Extends art_detail with location / seller_name / seller_address (shared by
     both Art and Collectibles; Art keeps painter_*, Collectibles leave it null).
     manual_attachment.kind gains 'bill' / 'authentication_certificate' — no
     schema change needed (kind is free-text TEXT validated in app code).
  2. Moves every manual_input + manual_attachment row from category 'art' to
     'collectibles'. art_detail is keyed by (entity_id, label) with no category,
     so it needs no move — it simply follows the relabelled asset. Stored files
     keep their on-disk path (served via stored_path, not category), so nothing
     moves on disk.

After this, the Art page starts empty (rebuilt for paintings) and everything
that was there shows under Collectibles.

Run once:  python -m workers.db_migrate_collectibles
Re-running is safe (idempotent: columns use IF NOT EXISTS; the category move is a
no-op once no 'art' rows remain).
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
ALTER TABLE art_detail ADD COLUMN IF NOT EXISTS location       TEXT;
ALTER TABLE art_detail ADD COLUMN IF NOT EXISTS seller_name    TEXT;
ALTER TABLE art_detail ADD COLUMN IF NOT EXISTS seller_address TEXT;
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
            cur.execute("UPDATE manual_input SET category = 'collectibles' WHERE category = 'art'")
            mi = cur.rowcount
            cur.execute("UPDATE manual_attachment SET category = 'collectibles' WHERE category = 'art'")
            ma = cur.rowcount
        conn.commit()
        print(f"collectibles ready; moved {mi} manual_input + {ma} manual_attachment rows art -> collectibles.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
