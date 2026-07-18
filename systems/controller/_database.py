# systems/controller/_database.py

"""
Gated database capability: list/show/edit/delete, creator-only.
Allowlist-based — db.py's DB_READ_EXCLUDE/DB_WRITE_ALLOWLIST decide what's
actually touchable, independent of what's asked here, so a new sensitive
table added later is excluded by default rather than accidentally exposed.
See SELF_MODIFICATION_ARCHITECTURE.md's Component 1 entry for why. Split
out of system.py (2026-07-16).
"""

import re

from db.db import list_db_tables, get_db_table_rows, update_db_row, delete_db_row
from config.logger_config import logger

from systems.controller._role_gates import require_creator
from core.text_utils import strip_trailing_punctuation
from core.phrasebook import get_phrase

# Deterministic phrase shapes rather than a classifier — a DB write is
# exactly the kind of thing where a misread command (false positive) is
# far worse than having to rephrase (false negative). The real safety net
# is db.py's DB_WRITE_ALLOWLIST regardless of how the command is parsed.
DB_EDIT_ROW_RE = re.compile(r"^edit database row (\d+) in (\w+) set (\w+) to (.+)$")
DB_DELETE_ROW_RE = re.compile(r"^delete database row (\d+) in (\w+)$")


async def handle(session, user_id: str, text: str, msg: str):
    """Returns a response dict if this category handled the message,
    None otherwise (caller tries the next category)."""

    if msg.startswith("list database tables"):
        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        tables = await list_db_tables()
        logger.info(f"[ACTION] Database tables listed (by {user_id})")

        return {"type": "response", "content": ", ".join(tables)}

    if msg.startswith("show database table"):
        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        name = strip_trailing_punctuation(msg.replace("show database table", "").strip())

        if not name:
            return {"type": "response", "content": await get_phrase("db_table_name_missing")}

        rows = await get_db_table_rows(name)

        if rows is None:
            return {
                "type": "response",
                "content": await get_phrase("db_table_not_readable", name=name)
            }

        logger.info(f"[ACTION] Database table '{name}' viewed (by {user_id})")

        if not rows:
            return {"type": "response", "content": await get_phrase("db_table_empty", name=name)}

        preview = rows[:10]
        more = f" (+{len(rows) - 10} more)" if len(rows) > 10 else ""

        return {"type": "response", "content": "\n".join(str(r) for r in preview) + more}

    db_edit_match = DB_EDIT_ROW_RE.match(msg)
    if db_edit_match:
        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        rowid, table, column, _ = db_edit_match.groups()
        # Extract from text.strip() (same length/positions as msg, since
        # msg is text.lower().strip() — lower() doesn't shift character
        # offsets) rather than msg itself, so the stored value keeps its
        # original casing.
        value = text.strip()[db_edit_match.start(4):]

        ok, reason = await update_db_row(table, int(rowid), column, value)
        logger.info(
            f"[ACTION] Database row edit: {table}#{rowid}.{column} "
            f"(by {user_id}): {'ok' if ok else reason}"
        )

        return {"type": "response", "content": await get_phrase("db_row_updated") if ok else reason}

    db_delete_match = DB_DELETE_ROW_RE.match(msg)
    if db_delete_match:
        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        rowid, table = db_delete_match.groups()

        ok, reason = await delete_db_row(table, int(rowid))
        logger.info(
            f"[ACTION] Database row delete: {table}#{rowid} "
            f"(by {user_id}): {'ok' if ok else reason}"
        )

        return {"type": "response", "content": await get_phrase("db_row_deleted") if ok else reason}

    return None
