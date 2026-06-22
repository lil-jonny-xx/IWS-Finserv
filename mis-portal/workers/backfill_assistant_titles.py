#!/usr/bin/env python3
"""
One-off backfill: title every untitled Jarvis conversation from its first user
message, using the same logic as the live auto-titling in main.assistant_chat.

Idempotent — only touches non-archived conversations whose title is NULL/blank
and that have at least one user message. Safe to re-run. --dry-run previews
without writing.

Run:
  /var/www/mis-portal/venv/bin/python -m workers.backfill_assistant_titles --dry-run
  /var/www/mis-portal/venv/bin/python -m workers.backfill_assistant_titles
"""
import argparse
import os
import sys
import time
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv("/var/www/mis-portal/.env", override=True)

from assistant import engine as assistant_engine
from assistant import persistence as assistant_persistence


def get_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=RealDictCursor,
        connect_timeout=5,
    )


def first_user_message(conn, conversation_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT content FROM assistant_message
        WHERE conversation_id = %s AND role = 'user' AND content IS NOT NULL
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        (conversation_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row["content"] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="preview titles, write nothing")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — cannot generate titles.")
        sys.exit(1)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM assistant_conversation
        WHERE (title IS NULL OR btrim(title) = '')
          AND archived_at IS NULL
        ORDER BY id
        """
    )
    ids = [r["id"] for r in cur.fetchall()]
    cur.close()

    print(f"{len(ids)} untitled conversation(s) to consider"
          + (" (dry run — no writes)" if args.dry_run else ""))

    titled = skipped = failed = 0
    for cid in ids:
        msg = first_user_message(conn, cid)
        if not msg:
            skipped += 1
            print(f"  conv {cid}: no user message — skipped")
            continue
        title = assistant_engine.generate_title(msg)
        if not title:
            failed += 1
            print(f"  conv {cid}: title generation failed — left untitled")
            continue
        if args.dry_run:
            print(f"  conv {cid}: would set -> {title!r}")
            titled += 1
            continue
        assistant_persistence.update_title(conn, cid, title)
        conn.commit()
        titled += 1
        print(f"  conv {cid}: set -> {title!r}")
        time.sleep(0.2)  # gentle on rate limits

    conn.close()
    print(f"Done. titled={titled} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
