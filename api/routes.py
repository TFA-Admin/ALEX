from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from llm.ollama_client import ollama_manager
from db.db import (
    add_memory,
    update_fact,
    fetch_user_facts
)

router = APIRouter()


class AskRequest(BaseModel):
    user: str
    prompt: str


class AddFactRequest(BaseModel):
    user: str
    key: str
    value: str


class UpdateFactRequest(BaseModel):
    user: str
    key: str
    value: str


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


# --------------------------
# Fact endpoints (FIXED)
# --------------------------
@router.post("/add_fact")
async def add_fact_endpoint(data: AddFactRequest):

    # 🔥 unified into update_fact
    await update_fact(data.user, data.key, data.value)

    return JSONResponse({
        "status": "ok",
        "message": f"Fact {data.key} set for {data.user}"
    })


@router.post("/update_fact")
async def update_fact_endpoint(data: UpdateFactRequest):

    await update_fact(data.user, data.key, data.value)

    return JSONResponse({
        "status": "ok",
        "message": f"Updated {data.key} for {data.user}"
    })