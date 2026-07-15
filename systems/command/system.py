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
import json

from core.system_base import BaseSystem

from db.db import update_fact, fetch_user_facts, get_user_role
from ws.ws_utils import enrich_profile
from llm.ollama_client import pending_profile_changes, locked_fields


CONFIRM_TIMEOUT = 30


class System(BaseSystem):

    name = "command"
    priority = 5  # 🔥 higher than modules + llm

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        normalized = text.strip().lower()

        # -------------------------
        # SET EDIT CODE
        # -------------------------
        if "set my edit code" in normalized:
            numbers = re.findall(r'\d+', normalized)

            if numbers:
                code = "".join(numbers)

                await update_fact(user_id, "edit_code", code)

                updated = await enrich_profile(user_id)

                return {
                    "type": "response",
                    "content": f"Edit code set to {code}.",
                    "profile": updated
                }

        # -------------------------
        # SET OVERRIDE CODE
        # -------------------------
        if "set override code" in normalized:
            role = await get_user_role(user_id)

            if role not in ["admin", "creator"]:
                return {
                    "type": "response",
                    "content": "Not authorized."
                }

            match = re.search(r'override code (.+)', normalized)

            if match:
                code = match.group(1).strip().replace(" ", "").lower()

                await update_fact(user_id, "override_code", code)

                updated = await enrich_profile(user_id)

                return {
                    "type": "response",
                    "content": f"Override code set to {code}.",
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
                    "content": "Please provide a valid code."
                }

            stored_override = (facts.get("override_code") or "").replace(" ", "").lower()

            if facts.get("edit_code") == code or (
                role in ["admin", "creator"] and stored_override == code
            ):
                locked_fields[user_id] = {}

                updated = await enrich_profile(user_id)

                return {
                    "type": "response",
                    "content": f"Edit enabled for code {code}.",
                    "profile": updated
                }

            return {
                "type": "response",
                "content": "Invalid unlock code."
            }

        # -------------------------
        # LOCK PROFILE
        # -------------------------
        if normalized.startswith("lock profile"):
            locked_fields[user_id] = {"all": True}

            updated = await enrich_profile(user_id)

            return {
                "type": "response",
                "content": "Profile locked.",
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
                    "content": f"Timed out. Keeping existing value."
                }

            normalized_clean = re.sub(r'[^a-z]', '', normalized)

            if normalized_clean.startswith(("yes", "y", "confirm")):

                await update_fact(user_id, change["key"], change["new"])

                updated = await enrich_profile(user_id)

                pending_profile_changes.pop(user_id, None)

                return {
                    "type": "response",
                    "content": f"Updated {change['key']} to {change['new']}.",
                    "profile": updated
                }

            if normalized_clean.startswith(("no", "n")):
                pending_profile_changes.pop(user_id, None)

                return {
                    "type": "response",
                    "content": "Okay, keeping existing value."
                }

        return None