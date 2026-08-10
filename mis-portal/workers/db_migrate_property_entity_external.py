#!/usr/bin/env python3
"""
DB migration — allow grp='external' on property_entity.

Why: the register supports three holder groups. 'main' is the family's own
holders, 'parent' the group companies (folded in behind a toggle), and
'external' a third party outside the organisation who co-owns a building with
us — recorded only so a jointly-held property's ownership split can total 100%,
and never counted as our asset.

The application learned about 'external' but the CHECK constraint never did:

    CHECK (grp = ANY (ARRAY['main', 'parent']))

So POST /api/v1/property-entities with grp='external' — which is exactly what
the "Outside co-owner's name…" box on the properties page sends — failed the
constraint, fell through to the generic 500 handler, and surfaced in the UI as
"Could not add co-owner." The percentage box for that co-owner never appeared
either, because the owner row is only created once the holder exists, which
made a single broken constraint look like two separate missing features.

_property_holder_rows() already filters `pe.grp NOT IN ('parent','external')`,
so no aggregate changes meaning once these rows can exist.

Idempotent: safe to re-run; the constraint is dropped and recreated to the same
definition each time.

Run once:  python -m workers.db_migrate_property_entity_external
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

CONSTRAINT = "property_entity_grp_check"

MIGRATE = f"""
ALTER TABLE property_entity DROP CONSTRAINT IF EXISTS {CONSTRAINT};
ALTER TABLE property_entity ADD CONSTRAINT {CONSTRAINT}
    CHECK (grp = ANY (ARRAY['main'::text, 'parent'::text, 'external'::text]));
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
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='property_entity'::regclass AND conname=%s",
                (CONSTRAINT,),
            )
            row = cur.fetchone()
            print(f"before: {row[0] if row else '(no constraint)'}")

            cur.execute(MIGRATE)

            cur.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='property_entity'::regclass AND conname=%s",
                (CONSTRAINT,),
            )
            print(f"after : {cur.fetchone()[0]}")
        conn.commit()
        print("✅ grp='external' is now accepted.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
