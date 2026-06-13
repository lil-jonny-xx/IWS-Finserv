#!/usr/bin/env python3
"""
DB Migration — Jarvis assistant tables (assistant_conversation, assistant_message).
Run once: python workers/db_migrate_assistant.py

These are the ONLY tables the assistant writes to. Financial tables stay read-only.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def main():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    cur  = conn.cursor()

    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS assistant_conversation (
                id              SERIAL PRIMARY KEY,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                title           VARCHAR(300),
                scope_entity_id INTEGER REFERENCES entity(id),   -- NULL = all entities
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                archived_at     TIMESTAMPTZ
            )
        """)
        print("assistant_conversation table: OK")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_assistant_conv_user
                ON assistant_conversation (user_id, archived_at)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS assistant_message (
                id              SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES assistant_conversation(id),
                role            VARCHAR(20) NOT NULL,
                content         TEXT,
                tool_calls      JSONB,
                citations       JSONB,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        print("assistant_message table: OK")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_assistant_msg_conv
                ON assistant_message (conversation_id, created_at)
        """)

        conn.commit()
        print("\n✅  Migration complete.")

    except Exception as e:
        conn.rollback()
        print(f"❌  Migration failed: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
