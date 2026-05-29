#!/usr/bin/env python3
"""
Seed real users and update entity emails for DHR, ADR, IWS.

Usage:
  python workers/seed_users.py

Default temporary passwords (change after first login):
  ***REMOVED***     → ***REMOVED***
  ***REMOVED***     → ***REMOVED***
  ***REMOVED***     → ***REMOVED***
  ***REMOVED***    → ***REMOVED***   (admin)
"""
import os
import sys
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv('/var/www/mis-portal/.env')

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

USERS = [
    {
        "email":     "***REMOVED***",
        "password":  "***REMOVED***",
        "full_name": "***REMOVED***",
        "role":      "member",
        "entity_id": 7,   # DHR
    },
    {
        "email":     "***REMOVED***",
        "password":  "***REMOVED***",
        "full_name": "***REMOVED***",
        "role":      "member",
        "entity_id": 8,   # ADR
    },
    {
        "email":     "***REMOVED***",
        "password":  "***REMOVED***",
        "full_name": "IWS Finserv",
        "role":      "member",
        "entity_id": 9,   # IWS
    },
    {
        "email":     "***REMOVED***",
        "password":  "***REMOVED***",
        "full_name": "IWS Admin",
        "role":      "admin",
        "entity_id": 9,   # Admin linked to IWS entity
    },
]

ENTITY_EMAILS = {
    7: "***REMOVED***",
    8: "***REMOVED***",
    9: "***REMOVED***",
}


def main():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    cur = conn.cursor()

    # Update entity emails
    print("Updating entity emails...")
    for entity_id, email in ENTITY_EMAILS.items():
        cur.execute(
            "UPDATE entity SET email = %s WHERE id = %s",
            (email, entity_id)
        )
        print(f"  entity {entity_id} → {email}")

    # Upsert real users
    print("\nCreating / updating users...")
    for u in USERS:
        pw_hash = bcrypt.hashpw(u["password"].encode(), bcrypt.gensalt(rounds=12)).decode()
        cur.execute("""
            INSERT INTO users (email, password_hash, full_name, role, entity_id, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (email) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                full_name     = EXCLUDED.full_name,
                role          = EXCLUDED.role,
                entity_id     = EXCLUDED.entity_id,
                is_active     = TRUE,
                failed_attempts = 0,
                locked_until    = NULL
        """, (u["email"], pw_hash, u["full_name"], u["role"], u["entity_id"]))
        print(f"   {u['full_name']:<25} {u['email']:<28} role={u['role']}")

    conn.commit()
    cur.close()
    conn.close()

    print("\n=== Credentials (temporary — change after first login) ===")
    for u in USERS:
        print(f"  {u['email']:<28} password: {u['password']}")
    print()


if __name__ == "__main__":
    main()
