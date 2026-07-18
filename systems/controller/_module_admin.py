# systems/controller/_module_admin.py

"""
Module registry admin: enable/disable/list, and the elevated-access
approval flow for privileged modules (see
SELF_MODIFICATION_ARCHITECTURE.md's privilege-tier system). Split out
of system.py (2026-07-16).
"""

import re

from db.db import (
    get_module_registry_entry, set_module_status, list_module_registry,
    approve_elevated_access, fetch_requests_needing_access_approval
)
from config.logger_config import logger

from systems.controller._role_gates import require_privileged, require_creator
from core.text_utils import strip_trailing_punctuation, first_word
from core.phrasebook import get_phrase

# user_id -> request_id awaiting a yes/no confirmation. Module-level
# state (not session-scoped), same short-lived two-turn pattern
# systems/modules/system.py's pending_builds already uses reliably.
#
# 2026-07-16: this used to be a single-shot, fully-anchored regex that
# both detected AND committed the grant in one exact phrase. That kept
# breaking on real STT noise — three different transcripts failed for
# three different reasons in one session (dropped "for", added a
# trailing period, "approve" heard as "approved") — because it asked one
# fragile utterance to carry both intent and commitment at once. Split
# into propose-then-confirm instead: detection below is deliberately
# loose (STT noise on the connective words no longer matters, since it
# only produces a proposal), and the actual grant only commits on an
# explicit "yes" — the same mechanism module builds already prove is
# reliable. See SELF_MODIFICATION_ARCHITECTURE.md's session history.
_pending_access_approvals = {}

ACCESS_APPROVAL_TRIGGER_RE = re.compile(r"approved?.*?request\s+(\d+)")


async def handle(session, user_id: str, text: str, msg: str):
    """Returns a response dict if this category handled the message,
    None otherwise (caller tries the next category)."""

    # -------------------------
    # PENDING ELEVATED-ACCESS CONFIRMATION — checked first, independent
    # of everything else below, same reasoning as pending_builds: a bare
    # "yes"/"no" only means something if a proposal is actually pending,
    # and checking this first means it doesn't matter what shape the
    # reply text takes otherwise.
    # -------------------------
    if user_id in _pending_access_approvals:
        request_id = _pending_access_approvals[user_id]

        word = first_word(msg)

        if word in ("yes", "y", "yeah", "confirm"):
            denial = await require_creator(user_id, session, text)
            if denial:
                del _pending_access_approvals[user_id]
                return denial

            await approve_elevated_access(request_id)
            del _pending_access_approvals[user_id]
            logger.info(f"[ACTION] Elevated access approved for request #{request_id} (by {user_id})")

            return {
                "type": "response",
                "content": await get_phrase("access_approved", request_id=request_id)
            }

        if word in ("no", "n"):
            del _pending_access_approvals[user_id]
            logger.info(f"[ACTION] Elevated access approval declined for request #{request_id} (by {user_id})")

            return {"type": "response", "content": await get_phrase("access_declined")}

        # Neither yes nor no. 2026-07-16 (Craig) — used to leave this
        # pending for up to PENDING_TIMEOUT more seconds, meaning a
        # genuinely unrelated later "yes" could wrongly approve THIS
        # stale elevated-access request instead — a real, more serious
        # version of the same risk for a security-relevant grant. Clear
        # it now instead of leaving that window open. Also fixed in the
        # same pass: this whole block was still doing raw
        # msg.startswith(("yes","y",...)) — the exact false-positive
        # class ("You're just gonna say that..." matching bare "y")
        # already fixed in systems/modules/system.py earlier tonight, but
        # never actually carried over to this file. Now uses first_word()
        # like everywhere else.
        del _pending_access_approvals[user_id]
        return None

    # -------------------------
    # MODULE ENABLE/DISABLE/LIST (Phase 1 registry — durable, DB-backed,
    # not a session-scoped set like the systems/* toggles, since modules
    # are creator-built artifacts meant to persist)
    # -------------------------
    if msg.startswith("disable module"):
        denial = await require_privileged(user_id, session, text)
        if denial:
            return denial

        name = strip_trailing_punctuation(msg.replace("disable module", "").strip())

        if not name:
            return {"type": "response", "content": await get_phrase("module_name_missing")}

        entry = await get_module_registry_entry(name)
        if not entry:
            return {"type": "response", "content": await get_phrase("module_not_found", name=name)}

        await set_module_status(name, "disabled")
        logger.info(f"[ACTION] Module '{name}' disabled (by {user_id})")

        return {"type": "response", "content": await get_phrase("module_disabled", name=name)}

    if msg.startswith("enable module"):
        denial = await require_privileged(user_id, session, text)
        if denial:
            return denial

        name = strip_trailing_punctuation(msg.replace("enable module", "").strip())

        entry = await get_module_registry_entry(name)
        if not entry:
            return {"type": "response", "content": await get_phrase("module_not_found", name=name)}

        await set_module_status(name, "enabled")
        logger.info(f"[ACTION] Module '{name}' enabled (by {user_id})")

        return {"type": "response", "content": await get_phrase("module_enabled", name=name)}

    if msg.startswith("list modules"):
        denial = await require_privileged(user_id, session, text)
        if denial:
            return denial

        modules = await list_module_registry()

        if not modules:
            return {"type": "response", "content": await get_phrase("no_modules_built")}

        lines = [f"{m['name']} (v{m['version']}, {m['status']})" for m in modules]
        return {"type": "response", "content": "\n".join(lines)}

    # -------------------------
    # ELEVATED ACCESS APPROVAL (creator only) — the second, explicit
    # decision for a module Claude has flagged as needing real access
    # beyond the plain sandbox (OS/process, network, hardware). This is
    # deliberately separate from the original "yes, build this"
    # confirmation — Craig sees exactly what's being requested and why
    # before the grant takes effect.
    # -------------------------
    if msg.startswith("list access requests") or msg.startswith("list pending access"):
        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        pending = await fetch_requests_needing_access_approval()

        if not pending:
            return {"type": "response", "content": await get_phrase("no_access_requests_pending")}

        lines = [
            f"#{r['id']} {r['module_name']}: {r['requested_access']}"
            for r in pending
        ]
        return {"type": "response", "content": "\n".join(lines)}

    trigger_match = ACCESS_APPROVAL_TRIGGER_RE.search(msg)
    if trigger_match:
        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        request_id = int(trigger_match.group(1))

        pending = await fetch_requests_needing_access_approval()
        matching = next((r for r in pending if r["id"] == request_id), None)

        if not matching:
            return {
                "type": "response",
                "content": await get_phrase("access_request_not_pending", request_id=request_id)
            }

        _pending_access_approvals[user_id] = request_id
        logger.info(f"[ACTION] Elevated access approval proposed for request #{request_id} (by {user_id}, awaiting confirmation)")

        return {
            "type": "response",
            "content": await get_phrase(
                "access_approval_proposed", request_id=request_id,
                module_name=matching["module_name"], access_desc=matching["requested_access"]
            )
        }

    return None
