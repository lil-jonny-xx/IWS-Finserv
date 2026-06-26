#!/usr/bin/env python3
"""
DB migration — manual-asset attachments + art details (Phase 2).

Adds:
  * `manual_attachment` — uploaded files (artwork images, property deeds / sale
    deeds / plans / documents) for a manual asset. Stored on the filesystem under
    the uploads root; only the path + metadata live in the DB.
  * `art_detail` — painter name / about text for an Art entry (cost & current
    value reuse the manual_input row).

IMPORTANT — keying. manual_input is INSERT-ONLY/VERSIONED: every save inserts a new
row and the latest is read via DISTINCT ON (entity_id, category, label). So a
manual_input row id is NOT stable across edits. Attachments and art details are
therefore keyed by the STABLE logical identity (entity_id, category, label) /
(entity_id, label) — editing an asset's value never orphans its files.

Idempotent — safe to run repeatedly.

  /var/www/.venv/bin/python -m workers.db_migrate_manual_attachments
"""
import os
import sys
import logging
import psycopg2

from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DDL = [
    (
        "create manual_attachment",
        """
        CREATE TABLE IF NOT EXISTS manual_attachment (
            id            SERIAL PRIMARY KEY,
            entity_id     INTEGER NOT NULL REFERENCES entity(id),
            category      VARCHAR(40) NOT NULL,
            label         TEXT        NOT NULL,
            kind          VARCHAR(20) NOT NULL DEFAULT 'document',  -- art_image | deed | plan | document
            original_name TEXT,
            stored_path   TEXT        NOT NULL,   -- path relative to the uploads root
            thumb_path    TEXT,                   -- relative path to a thumbnail (images only)
            mime          VARCHAR(120),
            size_bytes    BIGINT,
            uploaded_by   INTEGER REFERENCES users(id),
            uploaded_at   TIMESTAMP DEFAULT NOW()
        );
        """,
    ),
    (
        "manual_attachment: index on (entity_id, category, label)",
        "CREATE INDEX IF NOT EXISTS idx_manual_attachment_asset "
        "ON manual_attachment(entity_id, category, label);",
    ),
    (
        "create art_detail",
        """
        CREATE TABLE IF NOT EXISTS art_detail (
            id            SERIAL PRIMARY KEY,
            entity_id     INTEGER NOT NULL REFERENCES entity(id),
            label         TEXT        NOT NULL,
            painter_name  TEXT,
            painter_about TEXT,
            updated_by    INTEGER REFERENCES users(id),
            updated_at    TIMESTAMP DEFAULT NOW(),
            UNIQUE (entity_id, label)
        );
        """,
    ),
]


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def main():
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    for name, sql in DDL:
        try:
            cur.execute(sql)
            conn.commit()
            logger.info(f"✅  {name}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌  {name}: {e}")
            sys.exit(1)

    # Ensure the uploads root exists (path also defined in main.py).
    uploads_root = os.getenv("UPLOADS_DIR", "/var/www/uploads")
    try:
        os.makedirs(os.path.join(uploads_root, "manual"), exist_ok=True)
        logger.info(f"✅  uploads dir ready: {uploads_root}/manual")
    except Exception as e:
        logger.warning(f"⚠️  could not create uploads dir {uploads_root}: {e}")

    cur.close()
    conn.close()
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
