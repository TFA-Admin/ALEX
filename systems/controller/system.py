# systems/controller/system.py

"""
Controller System

Responsibilities:
- Enforce the 3-role model (creator / super_user / user), each gated by
  role + live voice verification for privileged actions
- Toggle systems on/off per session (creator or super_user)
- Route creator-only admin commands (reload, personality, granting roles)
"""

from core.system_base import BaseSystem
from core.alex_core import alex_core
from core.intent_classifier import classify_personality_set
from db.db import (
    get_user_role, set_personality, get_personality, DEFAULT_PERSONALITY,
    log_personality_change, reset_all_phrases, fetch_user_facts, update_fact,
    profile_exists, find_profile_by_prefix,
    get_module_registry_entry, set_module_status, list_module_registry
)
from config.logger_config import logger

# 🔒 Kept as a deterministic list (not LLM judgment) because "reset" is a
# small, enumerable phrase space, and — confirmed live — a probabilistic
# classifier asked to detect "reset" produced dangerous false positives on
# totally unrelated messages ("reset the router" -> would have reset her
# personality). False negatives here (an unrecognized reset phrasing) just
# mean the creator has to rephrase; false positives would silently corrupt
# state, so this stays fixed logic. See core/intent_classifier.py's
# classify_personality_set() docstring for the "set" side of this decision.
PERSONALITY_RESET_TRIGGERS = (
    "reset your personality",
    "go back to your default personality",
    "go back to default",
    "default personality",
)


class System(BaseSystem):

    name = "controller"
    priority = 0  # 🔥 HIGHEST PRIORITY

    # -------------------------
    # ROLE GATES (role AND live voice verification, not just claimed name)
    # -------------------------
    async def _require_creator(self, user_id: str, session: dict):
        """Returns None if authorized, or a rejection response dict if not."""
        role = await get_user_role(user_id)

        if role != "creator":
            return {"type": "response", "content": "Only my creator can do that."}

        if not session.get("creator_verified"):
            return {
                "type": "response",
                "content": "I can't verify that's really you this session — voice verification is required first."
            }

        return None

    async def _require_privileged(self, user_id: str, session: dict):
        """Creator OR super_user, both requiring live voice verification this
        session. Used for lower-stakes admin actions (system enable/disable/
        list) — creator-identity actions (personality, reload, granting
        roles) stay behind _require_creator above."""
        role = await get_user_role(user_id)

        if role not in ("creator", "super_user"):
            return {"type": "response", "content": "You don't have permission to do that."}

        if not session.get("creator_verified"):
            return {
                "type": "response",
                "content": "I can't verify that's really you this session — voice verification is required first."
            }

        return None

    # -------------------------
    # INIT
    # -------------------------
    async def init(self):
        print("🎛️ Controller system ready")

    # -------------------------
    # MAIN HANDLER
    # -------------------------
    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        msg = text.lower().strip()

        # -------------------------
        # SYSTEM TOGGLING (creator or super_user, both voice-verified —
        # previously gated behind an unauthenticated "become controller"
        # phrase anyone on the LAN could say; that's gone now)
        # -------------------------
        if msg.startswith("disable system"):
            denial = await self._require_privileged(user_id, session)
            if denial:
                return denial

            name = msg.replace("disable system", "").strip()

            if not name:
                return {
                    "type": "response",
                    "content": "Specify a system name."
                }

            disabled = session.setdefault("disabled_systems", set())
            disabled.add(name)

            logger.info(f"[ACTION] System '{name}' disabled (session, by {user_id})")

            return {
                "type": "response",
                "content": f"System '{name}' disabled."
            }

        if msg.startswith("enable system"):
            denial = await self._require_privileged(user_id, session)
            if denial:
                return denial

            name = msg.replace("enable system", "").strip()

            disabled = session.setdefault("disabled_systems", set())

            if name in disabled:
                disabled.remove(name)

                logger.info(f"[ACTION] System '{name}' re-enabled (session, by {user_id})")

                return {
                    "type": "response",
                    "content": f"System '{name}' enabled."
                }

            return {
                "type": "response",
                "content": f"System '{name}' was not disabled."
            }

        # -------------------------
        # LIST SYSTEMS
        # -------------------------
        if msg.startswith("list systems"):
            denial = await self._require_privileged(user_id, session)
            if denial:
                return denial

            disabled = session.get("disabled_systems", set())

            return {
                "type": "response",
                "content": f"Disabled systems: {list(disabled)}"
            }

        # -------------------------
        # MODULE ENABLE/DISABLE/LIST (Phase 1 registry — durable, DB-backed,
        # not a session-scoped set like the systems/* toggles above, since
        # modules are creator-built artifacts meant to persist)
        # -------------------------
        if msg.startswith("disable module"):
            denial = await self._require_privileged(user_id, session)
            if denial:
                return denial

            name = msg.replace("disable module", "").strip()

            if not name:
                return {"type": "response", "content": "Specify a module name."}

            entry = await get_module_registry_entry(name)
            if not entry:
                return {"type": "response", "content": f"I don't have a module called '{name}'."}

            await set_module_status(name, "disabled")
            logger.info(f"[ACTION] Module '{name}' disabled (by {user_id})")

            return {"type": "response", "content": f"Module '{name}' disabled."}

        if msg.startswith("enable module"):
            denial = await self._require_privileged(user_id, session)
            if denial:
                return denial

            name = msg.replace("enable module", "").strip()

            entry = await get_module_registry_entry(name)
            if not entry:
                return {"type": "response", "content": f"I don't have a module called '{name}'."}

            await set_module_status(name, "enabled")
            logger.info(f"[ACTION] Module '{name}' enabled (by {user_id})")

            return {"type": "response", "content": f"Module '{name}' enabled."}

        if msg.startswith("list modules"):
            denial = await self._require_privileged(user_id, session)
            if denial:
                return denial

            modules = await list_module_registry()

            if not modules:
                return {"type": "response", "content": "No modules built yet."}

            lines = [f"{m['name']} (v{m['version']}, {m['status']})" for m in modules]
            return {"type": "response", "content": "\n".join(lines)}

        # -------------------------
        # RELOAD SYSTEM (creator only — this re-executes code from disk)
        # -------------------------
        if msg.startswith("reload system"):

            denial = await self._require_creator(user_id, session)
            if denial:
                return denial

            name = msg.replace("reload system", "").strip()

            if not name:
                return {
                    "type": "response",
                    "content": "Specify a system name."
                }

            ok = await alex_core.reload_system(name)

            logger.info(f"[ACTION] Reloaded system '{name}' (by creator {user_id}): {'ok' if ok else 'FAILED'}")

            return {
                "type": "response",
                "content": f"Reloaded '{name}'." if ok else f"Failed to reload '{name}'."
            }

        # -------------------------
        # GRANT / REVOKE super_user (creator only, own override code) —
        # "role" stays fully locked from the generic fact-update flow in
        # permissions/system.py; this is the one deliberate, narrow path
        # that can touch it, and only ever to/from "super_user" — it
        # refuses to touch anyone whose current role is "creator".
        # -------------------------
        if "with override code" in msg:
            idx = msg.index("with override code")
            code = text.strip()[idx + len("with override code"):].strip()
            before = msg[:idx].strip()

            action, name = None, None

            if before.startswith("grant super user to "):
                action = "grant"
                name = before[len("grant super user to "):].strip()
            elif before.startswith("revoke super user from "):
                action = "revoke"
                name = before[len("revoke super user from "):].strip()

            if action and name:
                denial = await self._require_creator(user_id, session)
                if denial:
                    return denial

                target = name if await profile_exists(name) else await find_profile_by_prefix(name)

                if not target:
                    return {"type": "response", "content": f"I don't have a profile for '{name}'."}

                creator_facts = await fetch_user_facts(user_id)
                override_code = str(creator_facts.get("override_code", "")).strip()

                if not override_code or not code or code != override_code:
                    return {"type": "response", "content": "Invalid override code."}

                target_role = await get_user_role(target)

                if target_role == "creator":
                    return {"type": "response", "content": "I can't change that user's role."}

                new_role = "super_user" if action == "grant" else "user"
                await update_fact(target, "role", new_role)

                logger.info(f"[ACTION] Role change: '{target}' -> {new_role} ({action} by creator {user_id})")

                verb = "Granted super user to" if action == "grant" else "Revoked super user from"
                return {
                    "type": "response",
                    "content": f"{verb} {target}."
                }

        # -------------------------
        # PERSONALITY OVERRIDE (creator only — she develops her own
        # personality autonomously, but the creator can always step in)
        # -------------------------
        if any(t in msg for t in PERSONALITY_RESET_TRIGGERS):

            denial = await self._require_creator(user_id, session)
            if denial:
                return denial

            await set_personality(DEFAULT_PERSONALITY)
            await log_personality_change(DEFAULT_PERSONALITY, "creator reset to default", kind="personality")
            logger.info("[PERSONALITY] Creator reset personality to default.")

            return {
                "type": "response",
                "content": "Personality reset to default."
            }

        new_desc = None

        for trigger in ("set your personality to", "override your personality to"):
            if msg.startswith(trigger):
                new_desc = text.strip()[len(trigger):].strip().strip('."\'')
                break

        # Fallback for phrasing that isn't the exact literal command (e.g.
        # "be snarkier") — only asked for creator messages that didn't
        # already match a fixed phrase above, via a dedicated classifier
        # call (see core/intent_classifier.py's classify_personality_set()
        # docstring for why this is a separate call, not folded into the
        # shared one).
        if new_desc is None and await get_user_role(user_id) == "creator":
            result = await classify_personality_set(text)
            if result.get("personality_command") == "set":
                new_desc = result.get("value")

        if new_desc is not None:

            denial = await self._require_creator(user_id, session)
            if denial:
                return denial

            if not new_desc:
                return {
                    "type": "response",
                    "content": "Tell me what you'd like my personality to be."
                }

            await set_personality(new_desc)
            await log_personality_change(new_desc, "creator override", kind="personality")
            logger.info(f"[PERSONALITY] Creator override: {new_desc}")

            return {
                "type": "response",
                "content": f"Personality updated: {new_desc}"
            }

        if msg.startswith("what is your personality") or msg.startswith("what's your personality"):

            current = await get_personality()

            return {
                "type": "response",
                "content": current
            }

        if msg.startswith("reset your phrases") or msg.startswith("reset how you talk"):

            denial = await self._require_creator(user_id, session)
            if denial:
                return denial

            await reset_all_phrases()
            await log_personality_change("(all reset to defaults)", "creator reset", kind="phrases")
            logger.info("[PERSONALITY] Creator reset all phrases to defaults.")

            return {
                "type": "response",
                "content": "All my scripted phrases are back to their defaults."
            }

        return None