#!/usr/bin/env python3
"""
DB migration — promote SHR from a property-register holder to a full system entity.

Until now SHR existed ONLY in `property_entity` (id 12, short_code SHR), seeded by
db_migrate_properties.py, which noted "SHR is not a system entity (not in the entity
table)". It therefore owned property but had no PAN group, no accounts, and never
appeared in the entity pills on Equity / MF / Cash / Realised Gains.

This adds the missing `entity` row. Nothing needs to link the two by hand: the
property rollup joins holders to system entities BY NAME —

    LEFT JOIN entity e ON e.entity_name = pe.name        (main.py, _property_positions)

— so the moment an entity named 'SHR' exists, the 4 properties SHR holds stop
reporting under a synthetic negative entity id and roll up under the real one.

PAN: SHR gets its own PAN group with pan_number left NULL (nullable column) —
fill it in when the number is to hand:
    UPDATE pan_group SET pan_number = '<PAN>' WHERE pan_name = 'PAN_SHR';

No login is created. Entities and users are separate — several entities (IWS
Fincorp, Rajani Corp, HDR) have no user row either. Add one via the admin user
screen if SHR should be able to sign in.

Idempotent — safe to run repeatedly.
Run once:  python -m workers.db_migrate_shr_entity
"""
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

ENTITY_NAME = "SHR"
PAN_NAME = "PAN_SHR"
PAN_DESC = "SHR"

# Kept out of source — no mailbox address belongs in the repo. Already applied, so
# this is only needed if the migration is re-run against a fresh database.
ENTITY_EMAIL = os.getenv("SHR_ENTITY_EMAIL", "").strip()


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. PAN group. pan_number stays NULL until the real number is supplied.
            cur.execute(
                """INSERT INTO pan_group (pan_name, description)
                   VALUES (%s, %s)
                   ON CONFLICT (pan_name) DO NOTHING
                   RETURNING id""",
                (PAN_NAME, PAN_DESC),
            )
            row = cur.fetchone()
            if row:
                pan_id = row["id"]
                print(f"pan_group '{PAN_NAME}' created (id={pan_id}, pan_number NULL).")
            else:
                cur.execute("SELECT id FROM pan_group WHERE pan_name = %s", (PAN_NAME,))
                pan_id = cur.fetchone()["id"]
                print(f"pan_group '{PAN_NAME}' already present (id={pan_id}).")

            # 2. The entity itself. entity has no unique key on entity_name, so guard
            #    on the name explicitly rather than relying on ON CONFLICT.
            cur.execute("SELECT id FROM entity WHERE entity_name = %s", (ENTITY_NAME,))
            existing = cur.fetchone()
            if existing:
                print(f"entity '{ENTITY_NAME}' already exists (id={existing['id']}) — nothing to do.")
                entity_id = existing["id"]
            else:
                if not ENTITY_EMAIL:
                    raise SystemExit(
                        "SHR_ENTITY_EMAIL is not set — refusing to create the entity with a "
                        "blank contact address. Export it and re-run."
                    )
                cur.execute(
                    """INSERT INTO entity (pan_group_id, entity_name, email)
                       VALUES (%s, %s, %s) RETURNING id""",
                    (pan_id, ENTITY_NAME, ENTITY_EMAIL),
                )
                entity_id = cur.fetchone()["id"]
                print(f"entity '{ENTITY_NAME}' created (id={entity_id}, email={ENTITY_EMAIL}).")

            # 3. Report the link that the name-join now makes live.
            cur.execute(
                """SELECT pe.id, pe.short_code, COUNT(o.id) AS props
                   FROM property_entity pe
                   LEFT JOIN property_owner o ON o.holder_id = pe.id
                   WHERE pe.name = %s
                   GROUP BY pe.id, pe.short_code""",
                (ENTITY_NAME,),
            )
            holder = cur.fetchone()
            if holder:
                print(f"property_entity '{ENTITY_NAME}' (id={holder['id']}) now resolves to "
                      f"entity id {entity_id}; {holder['props']} property stake(s) re-home.")
            else:
                print(f"WARNING: no property_entity named '{ENTITY_NAME}' — the name-join "
                      f"will not link any property holdings.")

        conn.commit()
        print("Done.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
