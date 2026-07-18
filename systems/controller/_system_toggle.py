# systems/controller/_system_toggle.py

"""
System-lifecycle commands: session-scoped enable/disable/list, and
reload-from-disk. Split out of system.py (2026-07-16).
"""

from core.alex_core import alex_core
from config.logger_config import logger

from systems.controller._role_gates import require_privileged, require_creator
from core.text_utils import strip_trailing_punctuation
from core.phrasebook import get_phrase


async def handle(session, user_id: str, text: str, msg: str):
    """Returns a response dict if this category handled the message,
    None otherwise (caller tries the next category)."""

    # -------------------------
    # SYSTEM TOGGLING (creator or super_user, both voice-verified —
    # previously gated behind an unauthenticated "become controller"
    # phrase anyone on the LAN could say; that's gone now)
    # -------------------------
    if msg.startswith("disable system"):
        denial = await require_privileged(user_id, session, text)
        if denial:
            return denial

        name = strip_trailing_punctuation(msg.replace("disable system", "").strip())

        if not name:
            return {
                "type": "response",
                "content": await get_phrase("system_name_missing")
            }

        disabled = session.setdefault("disabled_systems", set())
        disabled.add(name)

        logger.info(f"[ACTION] System '{name}' disabled (session, by {user_id})")

        return {
            "type": "response",
            "content": await get_phrase("system_disabled", name=name)
        }

    if msg.startswith("enable system"):
        denial = await require_privileged(user_id, session, text)
        if denial:
            return denial

        name = strip_trailing_punctuation(msg.replace("enable system", "").strip())

        disabled = session.setdefault("disabled_systems", set())

        if name in disabled:
            disabled.remove(name)

            logger.info(f"[ACTION] System '{name}' re-enabled (session, by {user_id})")

            return {
                "type": "response",
                "content": await get_phrase("system_enabled", name=name)
            }

        return {
            "type": "response",
            "content": await get_phrase("system_was_not_disabled", name=name)
        }

    # -------------------------
    # LIST SYSTEMS
    # -------------------------
    if msg.startswith("list systems"):
        denial = await require_privileged(user_id, session, text)
        if denial:
            return denial

        disabled = session.get("disabled_systems", set())

        return {
            "type": "response",
            "content": await get_phrase("disabled_systems_list", disabled_list=list(disabled))
        }

    # -------------------------
    # RELOAD SYSTEM (creator only — this re-executes code from disk)
    # -------------------------
    if msg.startswith("reload system"):

        denial = await require_creator(user_id, session, text)
        if denial:
            return denial

        name = strip_trailing_punctuation(msg.replace("reload system", "").strip())

        if not name:
            return {
                "type": "response",
                "content": await get_phrase("system_name_missing")
            }

        ok = await alex_core.reload_system(name)

        logger.info(f"[ACTION] Reloaded system '{name}' (by creator {user_id}): {'ok' if ok else 'FAILED'}")

        phrase_key = "system_reloaded" if ok else "system_reload_failed"
        return {
            "type": "response",
            "content": await get_phrase(phrase_key, name=name)
        }

    return None
