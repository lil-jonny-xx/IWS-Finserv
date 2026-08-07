#!/usr/bin/env python3
"""
Seed portal users and entity emails from an out-of-repo roster file.

No email addresses, personal names or passwords live in this file — an earlier
version hardcoded all three and was committed to a public repository. The roster
now comes from a JSON file that is never tracked by git.

Usage:
  python workers/seed_users.py [--roster PATH] [--reset-passwords] [--dry-run]

Roster file (default: $SEED_ROSTER_FILE, else /var/www/mis-portal/seed_users.json,
chmod 600) — a JSON list:

  [
    {"email": "...", "full_name": "...", "role": "member",
     "entity_id": 7, "password": "..."},
    ...
  ]

`password` is optional: omit it and a 20-character random password is generated
and printed once, which is the preferred path — nothing is then written down.

By default an existing user's password is LEFT ALONE; only their profile fields
are updated. Pass --reset-passwords to overwrite credentials for users already in
the table. (The previous version always overwrote, so any re-run silently reset
every account to a password that was public.)
"""
import argparse
import json
import os
import secrets
import string
import sys

import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv('/var/www/mis-portal/.env', override=True)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

DEFAULT_ROSTER = os.getenv("SEED_ROSTER_FILE", "/var/www/mis-portal/seed_users.json")

_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"


def _generate_password(n: int = 20) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def load_roster(path: str) -> list[dict]:
    """Read and validate the roster file, failing loudly rather than seeding junk."""
    if not os.path.exists(path):
        sys.exit(
            f"Roster file not found: {path}\n"
            "Create it (chmod 600, outside git) or point --roster / $SEED_ROSTER_FILE at it.\n"
            "Format: [{\"email\":..., \"full_name\":..., \"role\":..., \"entity_id\":...}, ...]"
        )
    with open(path) as fh:
        roster = json.load(fh)
    if not isinstance(roster, list) or not roster:
        sys.exit(f"Roster file {path} must contain a non-empty JSON list.")
    for i, u in enumerate(roster):
        missing = [k for k in ("email", "full_name", "role", "entity_id") if not u.get(k)]
        if missing:
            sys.exit(f"Roster entry {i} is missing required field(s): {', '.join(missing)}")
        if u["role"] not in ("admin", "member"):
            sys.exit(f"Roster entry {i} ({u['email']}): role must be 'admin' or 'member'.")
    return roster


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", default=DEFAULT_ROSTER, help="path to the JSON roster file")
    ap.add_argument("--reset-passwords", action="store_true",
                    help="also overwrite the password of users that already exist")
    ap.add_argument("--dry-run", action="store_true", help="report actions, write nothing")
    args = ap.parse_args()

    roster = load_roster(args.roster)

    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    cur = conn.cursor()

    cur.execute("SELECT email FROM users")
    existing = {r["email"] for r in cur.fetchall()}

    # Entity contact emails come from the same roster, so there is no second list to
    # keep in sync. Several users can share an entity (e.g. an admin alongside the
    # entity's own login), so take the first roster entry per entity and say so
    # rather than letting the last one silently win.
    print("Updating entity emails...")
    seen_entities: set[int] = set()
    for u in roster:
        eid = u["entity_id"]
        if eid in seen_entities:
            print(f"  entity {eid} skipped — already set from an earlier roster entry")
            continue
        seen_entities.add(eid)
        if not args.dry_run:
            cur.execute("UPDATE entity SET email = %s WHERE id = %s", (u["email"], eid))
        print(f"  entity {eid} updated")

    print("\nCreating / updating users...")
    generated: list[tuple[str, str]] = []
    for u in roster:
        is_new = u["email"] not in existing

        if is_new or args.reset_passwords:
            pw = u.get("password") or _generate_password()
            if not u.get("password"):
                generated.append((u["email"], pw))
            pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()
            sql = """
                INSERT INTO users (email, password_hash, full_name, role, entity_id, is_active)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (email) DO UPDATE SET
                    password_hash   = EXCLUDED.password_hash,
                    full_name       = EXCLUDED.full_name,
                    role            = EXCLUDED.role,
                    entity_id       = EXCLUDED.entity_id,
                    is_active       = TRUE,
                    failed_attempts = 0,
                    locked_until    = NULL
            """
            params = (u["email"], pw_hash, u["full_name"], u["role"], u["entity_id"])
            action = "created" if is_new else "password reset"
        else:
            # Profile-only update — the existing credential is preserved.
            sql = """
                UPDATE users SET full_name = %s, role = %s, entity_id = %s, is_active = TRUE
                WHERE email = %s
            """
            params = (u["full_name"], u["role"], u["entity_id"], u["email"])
            action = "profile updated (password kept)"

        if not args.dry_run:
            cur.execute(sql, params)
        print(f"   entity {u['entity_id']:<3} role={u['role']:<7} {action}")

    if args.dry_run:
        conn.rollback()
        print("\n=== Dry run — nothing written. ===")
    else:
        conn.commit()
        print("\n=== Seeding complete. ===")

    if generated:
        print("\nGenerated passwords — shown once, store them in your password manager now:")
        for email, pw in generated:
            print(f"  {email:<28} {pw}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
