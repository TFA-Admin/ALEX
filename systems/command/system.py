# systems/command/system.py

"""
Command System

Handles:
- profile locking/unlocking
- edit codes
- override codes
- confirmation responses

Replaces ws_commands.py
"""

import re
import time

from core.system_base import BaseSystem

from db.db import update_fact, fetch_user_facts, get_user_role
from ws.ws_utils import enrich_profile
from llm.ollama_client import pending_profile_changes, locked_fields
from core.phrasebook import get_phrase
from core.text_utils import first_word


CONFIRM_TIMEOUT = 30

# 2026-07-17: widened phrase coverage rather than moving to a classifier —
# same reasoning PERSONALITY_RESET_TRIGGERS (systems/controller/_personality.py)
# and SEARCH_TRIGGERS (systems/inquiry/system.py) already settled on: these
# are small, enumerable, high-stakes action spaces where a classifier
# false-positive (misreading an unrelated sentence as "set override code",
# or accidentally unlocking) is a real security cost, not just an
# annoyance — confirmed live elsewhere in this project ("reset the
# router" -> would have reset her personality) that an LLM judgment call
# on exactly this shape of decision produces dangerous false positives.
# Deterministic phrase lists trade some missed natural phrasing (a false
# negative just means rephrasing) for zero risk of acting on the wrong
# thing.
SET_EDIT_CODE_TRIGGERS = (
    "set my edit code", "change my edit code", "update my edit code",
    "set the edit code", "change the edit code", "update the edit code",
)

SET_OVERRIDE_CODE_TRIGGERS = (
    "set override code", "set the override code", "change override code",
    "change the override code", "update override code", "update the override code",
)

LOCK_PROFILE_TRIGGERS = (
    "lock profile", "lock my profile", "lock the profile",
    "secure my profile", "re-lock profile", "relock profile",
)


class System(BaseSystem):

    name = "command"
    priority = 5  # 🔥 higher than modules + llm

    async def diagnose(self):
        """Real check: every branch here depends on fetch_user_facts()
        actually working (edit-code/override-code/unlock all read facts
        first)."""
        try:
            await fetch_user_facts("craig")
        except Exception as e:
            return False, f"fetch_user_facts() raised: {e}"
        return True, ""

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        normalized = text.strip().lower()

        # -------------------------
        # SET EDIT CODE
        # -------------------------
        if any(t in normalized for t in SET_EDIT_CODE_TRIGGERS):
            numbers = re.findall(r'\d+', normalized)

            if numbers:
                code = "".join(numbers)

                await update_fact(user_id, "edit_code", code)

                updated = await enrich_profile(user_id)

                return {
                    "type": "response",
                    "content": await get_phrase("edit_code_set", code=code),
                    "profile": updated
                }

        # -------------------------
        # SET OVERRIDE CODE
        # -------------------------
        if any(t in normalized for t in SET_OVERRIDE_CODE_TRIGGERS):
            role = await get_user_role(user_id)

            if role not in ["admin", "creator"]:
                return {
                    "type": "response",
                    "content": await get_phrase("not_authorized")
                }

            match = re.search(r'override code (.+)', normalized)

            if match:
                code = match.group(1).strip().replace(" ", "").lower()

                await update_fact(user_id, "override_code", code)

                updated = await enrich_profile(user_id)

                return {
                    "type": "response",
                    "content": await get_phrase("override_code_set", code=code),
                    "profile": updated
                }

        # -------------------------
        # UNLOCK PROFILE
        # -------------------------
        if "unlock" in normalized or "enable edit" in normalized:

            numbers = re.findall(r'\d+', normalized)
            facts = await fetch_user_facts(user_id)
            role = await get_user_role(user_id)

            code = None

            if numbers:
                code = "".join(numbers)
            elif role in ["admin", "creator"]:
                match = re.search(r'(?:unlock|enable edit)(?: profile)?(?: with code)? (.+)', normalized)
                if match:
                    code = match.group(1).strip().replace(" ", "").lower()

            if not code:
                return {
                    "type": "response",
                    "content": await get_phrase("invalid_code_prompt")
                }

            stored_override = (facts.get("override_code") or "").replace(" ", "").lower()

            if facts.get("edit_code") == code or (
                role in ["admin", "creator"] and stored_override == code
            ):
                locked_fields[user_id] = {}

                updated = await enrich_profile(user_id)

                return {
                    "type": "response",
                    "content": await get_phrase("edit_enabled", code=code),
                    "profile": updated
                }

            return {
                "type": "response",
                "content": await get_phrase("invalid_unlock_code")
            }

        # -------------------------
        # LOCK PROFILE
        # -------------------------
        if any(t in normalized for t in LOCK_PROFILE_TRIGGERS):
            locked_fields[user_id] = {"all": True}

            updated = await enrich_profile(user_id)

            return {
                "type": "response",
                "content": await get_phrase("profile_locked"),
                "profile": updated
            }

        # -------------------------
        # CONFIRMATION SYSTEM
        # -------------------------
        if user_id in pending_profile_changes:

            change = pending_profile_changes[user_id]

            if time.time() - change.get("timestamp", 0) > CONFIRM_TIMEOUT:
                pending_profile_changes.pop(user_id, None)

                return {
                    "type": "response",
                    "content": await get_phrase("confirmation_timed_out")
                }

            # 2026-07-16: was re.sub(r'[^a-z]', '', normalized).startswith(
            # ("yes","y",...)) — stripping ALL non-letters (spaces
            # included) before checking meant "you're right" collapsed to
            # "youreright", which still matches a bare "y" — same false-
            # positive class fixed elsewhere tonight via first_word().
            word = first_word(normalized)

            if word in ("yes", "y", "confirm"):

                await update_fact(user_id, change["key"], change["new"])

                updated = await enrich_profile(user_id)

                pending_profile_changes.pop(user_id, None)

                return {
                    "type": "response",
                    "content": await get_phrase("fact_updated", field=change["key"], value=change["new"]),
                    "profile": updated
                }

            if word in ("no", "n"):
                pending_profile_changes.pop(user_id, None)

                return {
                    "type": "response",
                    "content": await get_phrase("keeping_existing_value")
                }

            # Neither yes nor no — same reasoning applied to every other
            # pending-confirmation flow tonight (Craig): a reply that
            # isn't yes/no means they've moved on, not that they're still
            # mid-answer. Clearing this now instead of leaving it open
            # for up to CONFIRM_TIMEOUT more seconds closes the window
            # where a later, unrelated "yes" could wrongly confirm this
            # stale change instead.
            pending_profile_changes.pop(user_id, None)

        return None