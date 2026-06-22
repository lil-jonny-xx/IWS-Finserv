"""
Conversation + message persistence for Jarvis.

All reads and writes are scoped to the authenticated `user_id`. A user can only see and
mutate their own conversations; ownership is enforced in SQL on every call. These are the
only tables Jarvis writes to — financial tables are read-only everywhere else.

Callers pass in a live DB connection (from main.get_db_connection) and own commit/rollback,
matching the transaction style of the surrounding endpoints.
"""

from __future__ import annotations

import json
from typing import Optional


def create_conversation(conn, user_id: int, title: Optional[str],
                        scope_entity_id: Optional[int]) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO assistant_conversation (user_id, title, scope_entity_id)
        VALUES (%s, %s, %s)
        RETURNING id, user_id, title, scope_entity_id, created_at, updated_at
        """,
        (user_id, title, scope_entity_id),
    )
    row = cur.fetchone()
    cur.close()
    return _conv_out(row)


def list_conversations(conn, user_id: int, include_archived: bool = False) -> list[dict]:
    cur = conn.cursor()
    archived_clause = "" if include_archived else "AND archived_at IS NULL"
    cur.execute(
        f"""
        SELECT id, user_id, title, scope_entity_id, created_at, updated_at, archived_at
        FROM assistant_conversation
        WHERE user_id = %s {archived_clause}
        ORDER BY updated_at DESC
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return [_conv_out(r) for r in rows]


def get_conversation(conn, user_id: int, conversation_id: int) -> Optional[dict]:
    """Return the conversation iff it belongs to user_id, else None."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user_id, title, scope_entity_id, created_at, updated_at, archived_at
        FROM assistant_conversation
        WHERE id = %s AND user_id = %s
        """,
        (conversation_id, user_id),
    )
    row = cur.fetchone()
    cur.close()
    return _conv_out(row) if row else None


def get_messages(conn, conversation_id: int) -> list[dict]:
    """Messages for a conversation. Caller MUST have verified ownership first."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, role, content, tool_calls, citations, charts, created_at
        FROM assistant_message
        WHERE conversation_id = %s
        ORDER BY created_at ASC, id ASC
        """,
        (conversation_id,),
    )
    rows = cur.fetchall()
    cur.close()
    return [{
        "id": r["id"],
        "role": r["role"],
        "content": r["content"],
        "tool_calls": r["tool_calls"],
        "citations": r["citations"],
        "charts": r["charts"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]


def add_message(conn, conversation_id: int, role: str, content: Optional[str],
                tool_calls: Optional[list] = None,
                citations: Optional[list] = None,
                charts: Optional[list] = None) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO assistant_message
            (conversation_id, role, content, tool_calls, citations, charts)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            conversation_id, role, content,
            json.dumps(tool_calls) if tool_calls is not None else None,
            json.dumps(citations) if citations is not None else None,
            json.dumps(charts) if charts is not None else None,
        ),
    )
    mid = cur.fetchone()["id"]
    cur.execute(
        "UPDATE assistant_conversation SET updated_at = NOW() WHERE id = %s",
        (conversation_id,),
    )
    cur.close()
    return mid


def update_title(conn, conversation_id: int, title: str) -> None:
    """Set a conversation's title. Caller decides when (e.g. only when untitled)."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE assistant_conversation SET title = %s WHERE id = %s",
        (title, conversation_id),
    )
    cur.close()


def archive_conversation(conn, user_id: int, conversation_id: int) -> bool:
    """Archive iff owned by user_id. Returns True if a row was archived."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE assistant_conversation
        SET archived_at = NOW(), updated_at = NOW()
        WHERE id = %s AND user_id = %s AND archived_at IS NULL
        """,
        (conversation_id, user_id),
    )
    changed = cur.rowcount
    cur.close()
    return changed > 0


def _conv_out(row) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "scope_entity_id": row["scope_entity_id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "archived_at": (row["archived_at"].isoformat()
                        if row.get("archived_at") else None),
    }
