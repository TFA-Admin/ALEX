# systems/memory/system.py

"""
Memory System

Responsibilities:
- Retrieve relevant memory
- Inject into session context
- Keep it lightweight and fast
"""

from core.system_base import BaseSystem

from db.db import (
    fetch_recent_memory,
    fetch_vector_memories,
    add_memory,
    add_vector_memory
)

from core.embedding_engine import embed, cosine_similarity
from config.logger_config import logger


class System(BaseSystem):

    name = "memory"
    priority = 8  # runs before modules + llm

    async def init(self):
        print("🧠 Memory system ready")

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        # -------------------------
        # EMBEDDING FOR QUERY
        # -------------------------
        try:
            query_vec = embed(text)
        except:
            return None  # fail silently

        # -------------------------
        # VECTOR MEMORY
        # -------------------------
        try:
            memories = await fetch_vector_memories(user_id)
        except:
            memories = []

        scored = []

        for m in memories:
            try:
                score = cosine_similarity(query_vec, m["embedding"])
                scored.append((score, m))
            except:
                continue

        scored.sort(reverse=True, key=lambda x: x[0])

        # 🔥 keep it SMALL (performance critical)
        top_memories = [m for _, m in scored[:2]]

        # -------------------------
        # RECENT MEMORY
        # -------------------------
        try:
            recent = await fetch_recent_memory(user_id)
        except:
            recent = []

        recent = recent[-2:]

        # -------------------------
        # BUILD CONTEXT STRING
        # -------------------------
        context_parts = []

        for m in top_memories:
            context_parts.append(f"Relevant: {m['prompt']} -> {m['response']}")

        for r in recent:
            context_parts.append(f"Recent: {r['prompt']} -> {r['response']}")

        context_text = "\n".join(context_parts)

        # -------------------------
        # STORE IN SESSION
        # -------------------------
        session["memory_context"] = context_text

        return None  # 🔥 does not respond

    async def after_response(self, session, user_id: str, input_data: dict, response_text: str):
        """
        Persists this turn so later turns (this session or a future one) can
        actually see it via fetch_recent_memory/fetch_vector_memories above.
        This was previously never wired up anywhere on the live WS chat path
        — SystemManager.after_response() was called correctly after every
        response, but no system implemented the hook, so add_memory() was
        only ever reachable through an unused legacy HTTP endpoint
        (api/routes.py's /ask). Conversations were being answered and then
        immediately discarded, with nothing for either same-session
        continuity or the self-reflection loop to draw on.
        """
        text = input_data.get("text", "")
        if not text or not response_text:
            return

        try:
            await add_memory(user_id, text, response_text)
        except Exception as e:
            logger.warning(f"⚠️ Failed to store memory: {e}")
            return

        try:
            vec = embed(text)
            await add_vector_memory(user_id, text, response_text, vec)
        except Exception as e:
            logger.warning(f"⚠️ Failed to store vector memory: {e}")