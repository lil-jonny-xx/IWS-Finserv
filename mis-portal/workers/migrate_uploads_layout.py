#!/usr/bin/env python3
"""
One canonical uploads tree, organized to mirror the portal's schema.

Before                                     After
------                                     -----
uploads/property/<pid>/<uuid>.pdf          uploads/properties/<pid>/<doc_type>/<uuid>.pdf
uploads/manual/<eid>/<uuid>.jpg            uploads/manual/<eid>/<category>/<uuid>.jpg
mis-portal/bank_statements/<acct>/…        uploads/bank-statements/<acct>/…   (dir was empty; constant moved)

The DB stays the source of truth (stored_path / thumb_path / original_path are
updated in the same transaction as each file move), so the API serves files
correctly throughout. Safe to re-run: rows already at their target path are
skipped — run it again after deploying the matching main.py to sweep any file
uploaded between migration and restart.

Run:  python -m workers.migrate_uploads_layout
"""
import os
import shutil
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

UPLOADS_ROOT = os.getenv("UPLOADS_DIR", "/var/www/uploads")


def _move(rel_old: str, rel_new: str) -> bool:
    """Move one file inside UPLOADS_ROOT; True if the target now exists."""
    src = os.path.join(UPLOADS_ROOT, rel_old)
    dst = os.path.join(UPLOADS_ROOT, rel_new)
    if os.path.exists(dst):
        return True
    if not os.path.exists(src):
        print(f"  !! missing on disk, skipped: {rel_old}")
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return True


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    moved = 0
    try:
        cur = conn.cursor()

        # -- property documents → properties/<pid>/<doc_type>/ ---------------
        cur.execute("SELECT id, property_id, doc_type, stored_path, original_path FROM property_document")
        for r in cur.fetchall():
            target_dir = os.path.join("properties", str(r["property_id"]), r["doc_type"])
            updates = {}
            for col in ("stored_path", "original_path"):
                rel = r[col]
                if not rel:
                    continue
                new_rel = os.path.join(target_dir, os.path.basename(rel))
                if rel == new_rel:
                    continue
                if _move(rel, new_rel):
                    updates[col] = new_rel
            if updates:
                sets = ", ".join(f"{c} = %s" for c in updates)
                cur.execute(f"UPDATE property_document SET {sets} WHERE id = %s",
                            (*updates.values(), r["id"]))
                conn.commit()
                moved += len(updates)

        # -- manual attachments → manual/<eid>/<category>/ --------------------
        cur.execute("SELECT id, entity_id, category, stored_path, thumb_path FROM manual_attachment")
        for r in cur.fetchall():
            target_dir = os.path.join("manual", str(r["entity_id"]), r["category"])
            updates = {}
            for col in ("stored_path", "thumb_path"):
                rel = r[col]
                if not rel:
                    continue
                new_rel = os.path.join(target_dir, os.path.basename(rel))
                if rel == new_rel:
                    continue
                if _move(rel, new_rel):
                    updates[col] = new_rel
            if updates:
                sets = ", ".join(f"{c} = %s" for c in updates)
                cur.execute(f"UPDATE manual_attachment SET {sets} WHERE id = %s",
                            (*updates.values(), r["id"]))
                conn.commit()
                moved += len(updates)

        # -- bank statements dir (no rows/files yet — just ensure it exists) --
        os.makedirs(os.path.join(UPLOADS_ROOT, "bank-statements"), exist_ok=True)

        # -- sweep now-empty legacy dirs --------------------------------------
        legacy = os.path.join(UPLOADS_ROOT, "property")
        for root, dirs, files in os.walk(legacy, topdown=False):
            if not os.listdir(root):
                os.rmdir(root)
        print(f"done — {moved} file paths migrated.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
