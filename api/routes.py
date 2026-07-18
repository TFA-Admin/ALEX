from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from llm.ollama_client import ollama_manager
from db.db import (
    add_memory,
    fetch_user_facts
)

router = APIRouter()


class AskRequest(BaseModel):
    user: str
    prompt: str


# --------------------------
# ASK endpoint (FIXED)
# --------------------------
@router.post("/ask")
async def ask_text(data: AskRequest):

    user = data.user
    prompt = data.prompt

    # 🔥 simple existence check (safe fallback)
    facts = await fetch_user_facts(user)
    if facts is None:
        return JSONResponse({
            "response": "⚠️ Unknown profile. Please initialize identity via WebSocket first."
        }, status_code=403)

    # 🔥 FIX: use streaming generator properly
    full_response = ""

    async for chunk in ollama_manager.generate_stream(prompt):
        full_response += chunk

    await add_memory(user, prompt, full_response)

    return JSONResponse({"response": full_response})

# 2026-07-18 (Craig: found live — /add_fact and /update_fact took a raw
# key/value with no restriction and no authentication, meaning anyone who
# could reach this server could POST key="override_code" for any user and
# take over creator identity, bypassing voice verification and every
# LOCKED_KEYS/OVERRIDE_ONLY_KEYS gate the conversational path enforces
# (systems/permissions/system.py, systems/facts/system.py). Confirmed
# nothing but tests/test_alex.py's manual smoke-test script ever called
# either route — removed both entirely rather than trying to bolt auth
# onto a single endpoint in a codebase with no other network-auth layer
# (security here is enforced at the content/identity level — voice
# verification, override codes — not network access control, so adding
# one-off HTTP auth would be inconsistent with everything else). Any real
# fact write now has to go through the conversational path's own gates.