# systems/permissions/system.py

"""
Permission System

Handles:
- edit_code validation (self updates)
- creator override (global updates)
- controlled fact mutation
- protected identity fields (override only)
"""

from core.system_base import BaseSystem
from db.db import update_fact, fetch_user_facts
from config.logger_config import logger


# -------------------------
# PROTECTED KEYS
# -------------------------
#
# 🔒 This is the actual security boundary — enforced in code below on
# whatever key comes out of extraction (LLM or regex), never trusted from
# either source directly.

LOCKED_KEYS = ["edit_code", "override_code", "role"]
OVERRIDE_ONLY_KEYS = ["user_name", "username", "profile", "id"]

PLACEHOLDER_VALUES = {"", "none", "null", "n/a", "not specified", "unspecified", "unknown"}


def _valid_field(v) -> bool:
    return isinstance(v, str) and v.strip() and v.strip().lower() not in PLACEHOLDER_VALUES


class System(BaseSystem):

    name = "permissions"
    priority = 6  # before facts

    async def init(self):
        print("🔐 Permission system ready")

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        try:
            return await self._handle_update(text, user_id, session)
        except Exception as e:
            print(f"⚠️ Permission error: {e}")
            return {
                "type": "response",
                "content": "Failed to process update."
            }

    def _extract_update_from_intent(self, session: dict):
        """
        Reads the shared classification already done once per message by
        systems/intent/system.py (session["intent"]) — no separate LLM
        call here. This is extraction only — it decides WHAT was asked
        for, never whether it's ALLOWED. Returns (None, None, None) if the
        classifier didn't find a permission_command, or any field is
        missing/a placeholder — callers fall back to the deterministic
        regex parser in that case.
        """
        intent = session.get("intent") or {}

        if intent.get("intent") != "permission_command":
            return None, None, None

        key, value, code = intent.get("key"), intent.get("value"), intent.get("code")

        if not (_valid_field(key) and _valid_field(value) and _valid_field(code)):
            return None, None, None

        return key.strip().lower().replace(" ", "_"), value.strip(), code.strip()

    def _extract_update_regex(self, text: str):
        """Deterministic fallback — the original fixed-phrase parser."""
        lower = text.lower()
        raw = text.strip()

        code = None
        before_code = None

        if "with override code" in lower:
            idx = lower.index("with override code")
            before_code = raw[:idx]
            code = raw[idx + len("with override code"):].strip()

        elif "with code" in lower:
            idx = lower.index("with code")
            before_code = raw[:idx]
            code = raw[idx + len("with code"):].strip()

        else:
            return None, None, None

        try:
            _, rest = before_code.split("set", 1)
            key_part, value = rest.split("to", 1)
        except Exception:
            return None, None, None

        key = key_part.strip().lower().replace(" ", "_")
        value = value.strip()

        if not key or not value or not code:
            return None, None, None

        return key, value, code

    async def _handle_update(self, text, user_id, session):

        key, value, code = self._extract_update_from_intent(session)

        if not (key and value and code):
            key, value, code = self._extract_update_regex(text)

        if not (key and value and code):
            return None

        # -------------------------
        # LOAD USER FACTS
        # -------------------------
        facts = await fetch_user_facts(user_id)

        user_edit_code = str(facts.get("edit_code", "")).strip()
        override_code = str(facts.get("override_code", "")).strip()
        code = code.strip()

        # -------------------------
        # VALIDATE CODE
        # -------------------------
        if code == user_edit_code:
            reason = "edit_code"
        elif code == override_code:
            reason = "override"
        else:
            return {
                "type": "response",
                "content": "Invalid code. Update rejected."
            }

        # -------------------------
        # ENFORCE KEY RULES
        # -------------------------
        if key in LOCKED_KEYS:
            return {
                "type": "response",
                "content": "This field cannot be modified."
            }

        if key in OVERRIDE_ONLY_KEYS and reason != "override":
            return {
                "type": "response",
                "content": "This field requires override authorization."
            }

        # -------------------------
        # APPLY UPDATE
        # -------------------------
        await update_fact(user_id, key, value)

        session["last_update"] = f"{key} = {value}"
        logger.info(f"[ACTION] Authorized update for {user_id}: {key} = {value!r} ({reason})")

        return {
            "type": "response",
            "content": f"{key.replace('_',' ')} updated to {value} ({reason})."
        }