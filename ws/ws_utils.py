import re

from db.db import fetch_user_facts
from llm.ollama_client import locked_fields
from config.logger_config import logger


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
    # Also logged (not just sent to the browser's Debug panel) so the
    # Controller's A.L.E.X. tab — which only ever sees what's written to
    # the log file — has the same visibility the browser does.
    logger.info(f"[DEBUG] {message}")

    try:
        await websocket.send_text(f"__DEBUG__{message}")
    except:
        pass


def split_speakable_text(buffer: str):
    # Batching two short sentences per chunk dates from when every chunk
    # spawned a fresh piper.exe (~0.6-0.8s); Piper has been one persistent
    # process since 2026-07-18, so the batching now only buys intonation
    # continuity across a pair of short sentences. Kept for that.
    #
    # 2026-09-21 (Craig: "Her listing things could use some work"): a line
    # break is a boundary too. A bulleted list has no sentence punctuation
    # until its last item, so the whole list used to arrive as one chunk
    # and be read as a single run-on breath. Each line is now its own
    # clause; core/response_handler.py turns the break into a pause when
    # it is spoken and keeps it as a line on the screen.
    matches = list(re.finditer(r".+?(?:[.!?](?=\s+|$)|(?=\n))", buffer, re.S))

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