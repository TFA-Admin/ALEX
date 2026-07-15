import re
import json

from db.db import fetch_user_facts
from llm.ollama_client import locked_fields


async def enrich_profile(user_id):
    facts = await fetch_user_facts(user_id)

    # edit status
    if facts.get("edit_code"):
        facts["edit_status"] = "Edit Code Set"
    else:
        facts["edit_status"] = "No Edit Code"

    # lock status
    state = locked_fields.get(user_id)
    if state and state.get("all"):
        facts["lock_status"] = "Locked"
    else:
        facts["lock_status"] = "Unlocked"

    # override mask
    if facts.get("override_code"):
        facts["override_status"] = "Override Code Enabled"
        facts["override_code"] = "****"

    return facts


async def send_debug(websocket, message: str):
    try:
        await websocket.send_text(f"__DEBUG__{message}")
    except:
        pass


def split_speakable_text(buffer: str):
    # Each speak() call spawns a fresh piper.exe process (~0.6-0.8s model
    # reload) — chunking too eagerly turns that into an audible gap between
    # every couple of sentences. Batching more text per chunk trades a
    # little more delay before the first words play for far fewer of these
    # gaps overall. (A persistent Piper process would remove the gap
    # entirely — bigger change, deferred, see roadmap.)
    matches = list(re.finditer(r".+?[.!?](?=\s+|$)", buffer, re.S))

    if not matches:
        return None, buffer

    first_end = matches[0].end()
    first_text = buffer[:first_end].strip()

    if len(first_text) >= 100:
        return first_text, buffer[first_end:].lstrip()

    if len(matches) < 2:
        return None, buffer

    second_end = matches[1].end()
    text = buffer[:second_end].strip()
    remaining = buffer[second_end:].lstrip()

    return text, remaining