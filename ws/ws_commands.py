import re
import json
import time

from db.db import update_fact, fetch_user_facts, get_user_role
from ws.ws_utils import enrich_profile
from llm.ollama_client import pending_profile_changes, locked_fields
from speech.tts_engine import speak, stop_speaking
from ws.ws_chat import pending_module_builds

CONFIRM_TIMEOUT = 30


async def handle_command(websocket, msg, user_id):
    normalized = msg.strip().lower()

    # -------------------------
    # 🔐 SET EDIT CODE
    # -------------------------
    if "set my edit code" in normalized:
        numbers = re.findall(r'\d+', normalized)

        if numbers:
            code = "".join(numbers)

            await update_fact(user_id, "edit_code", code)

            await websocket.send_text("__START__")
            await websocket.send_text(f"Edit code set to {code}.")
            await websocket.send_text("__END__")

            updated_facts = await enrich_profile(user_id)
            await websocket.send_text("__PROFILE__" + json.dumps(updated_facts))

            return True

    # -------------------------
    # 🔐 SET OVERRIDE CODE
    # -------------------------
    if "set override code" in normalized:
        role = await get_user_role(user_id)

        if role not in ["admin", "creator"]:
            await websocket.send_text("__START__")
            await websocket.send_text("Not authorized.")
            await websocket.send_text("__END__")
            return True

        match = re.search(r'override code (.+)', normalized)

        if match:
            code = match.group(1).strip().replace(" ", "").lower()

            await update_fact(user_id, "override_code", code)

            await websocket.send_text("__START__")
            await websocket.send_text(f"Override code set to {code}.")
            await websocket.send_text("__END__")

            updated_facts = await enrich_profile(user_id)
            await websocket.send_text("__PROFILE__" + json.dumps(updated_facts))

            return True

    # -------------------------
    # 🔐 UNLOCK PROFILE
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
            await websocket.send_text("__START__")
            await websocket.send_text("Please provide a valid code.")
            await websocket.send_text("__END__")
            return True

        stored_override = (facts.get("override_code") or "").replace(" ", "").lower()

        if facts.get("edit_code") == code or (
            role in ["admin", "creator"] and stored_override == code
        ):
            locked_fields[user_id] = {}

            await websocket.send_text("__START__")
            await websocket.send_text(f"Edit enabled for code {code}.")
            await websocket.send_text("__END__")
        else:
            await websocket.send_text("__START__")
            await websocket.send_text("Invalid unlock code.")
            await websocket.send_text("__END__")

        updated_facts = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated_facts))

        return True

    # -------------------------
    # 🔒 LOCK PROFILE
    # -------------------------
    if normalized.startswith("lock profile"):

        locked_fields[user_id] = {"all": True}

        await websocket.send_text("__START__")
        await websocket.send_text("Profile locked.")
        await websocket.send_text("__END__")

        updated_facts = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated_facts))

        return True

    # -------------------------
    # ✅ CONFIRMATION SYSTEM
    # -------------------------
    if user_id in pending_profile_changes and user_id not in pending_module_builds:

        change = pending_profile_changes[user_id]

        if time.time() - change.get("timestamp", 0) > CONFIRM_TIMEOUT:
            await websocket.send_text("__START__")
            await websocket.send_text(
                f"Timed out. I’ll keep your {change['key']} as {change.get("old", "the current value")}."
            )
            await websocket.send_text("__END__")

            del pending_profile_changes[user_id]
            return True

        normalized = re.sub(r'[^a-z]', '', msg.lower())

        if normalized.startswith(("yes","y","yeah","confirm")):

            await update_fact(user_id, change["key"], change["new"])

            await websocket.send_text("__START__")
            await websocket.send_text(f"Updated {change['key']} to {change['new']}.")
            await websocket.send_text("__END__")

            stop_speaking()
            speak(f"Updated {change['key']}.")

            updated_facts = await enrich_profile(user_id)
            await websocket.send_text("__PROFILE__" + json.dumps(updated_facts))

            del pending_profile_changes[user_id]
            return True

        if normalized.startswith(("no","n","nope")):

            await websocket.send_text("__START__")
            await websocket.send_text("Okay, keeping existing value.")
            await websocket.send_text("__END__")

            del pending_profile_changes[user_id]
            return True

    return False