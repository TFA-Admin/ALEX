# systems/memory/system.py

"""
Memory System

Responsibilities:
- Retrieve relevant memory
- Inject into session context
- Keep it lightweight and fast
"""

import re

from core.system_base import BaseSystem

from db.db import (
    fetch_recent_memory,
    fetch_vector_memories,
    add_memory
)

from core.embedding_engine import embed, cosine_similarity
from config.logger_config import logger

# 2026-09-20: the reinforcement/weight mechanism was removed entirely.
#
# It was wired up on 2026-07-18 - a regex detecting "thanks"/"perfect"/etc
# bumped the preceding turn's weight, and retrieval multiplied similarity by
# that weight. Measured 2026-09-20: all 572 memory rows sat at weight 1, so
# the multiplier was 1.0x on every retrieval and the path did nothing at all.
# The cause was that decay_memory() subtracted 1 per pass HOURLY while
# reinforcement added 1 per positive reaction, so decay ran ~24 times a day
# against a signal that fires a handful of times at best.
#
# Slowing decay would have made the numbers move, but Craig's call was to drop
# it: "weight was an idea but if it's irrelevant and does nothing simplify it
# by just removing it." It also had a design problem worth recording - it
# reinforced memories that earned a pleased REACTION, which is a gradient
# toward telling him what he wants to hear, and Design Principle 12 now
# explicitly forbids that. It measured approval, not usefulness.
#
# learned_knowledge's touch-on-retrieval (added the same day) is the mechanism
# that replaces it, and it measures actual reuse rather than approval.


class System(BaseSystem):

    name = "memory"
    priority = 8  # runs before modules + llm

    async def init(self):
        print("🧠 Memory system ready")

    async def diagnose(self):
        """Real check against embed() specifically — the vector-memory
        path this system depends on every turn, not exercised by
        recall's own diagnose() (which only checks fetch_recent_memory,
        not embedding)."""
        try:
            embed("diagnostic check")
        except Exception as e:
            return False, f"embed() raised: {e}"
        return True, ""

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

        # 2026-07-16: widened from -2: to -4:, affordable now that
        # llm/ollama_client.py's shared num_ctx quadrupled. A 2-turn window
        # let a real topic scroll out after just one intervening exchange
        # (confirmed live: python_code_explorer -> info_lookup -> "what did
        # I just ask you to build?" already had the real answer pushed out).
        recent = recent[-4:]

        # -------------------------
        # BUILD CONTEXT STRING
        # -------------------------
        # Each line is labeled with when it happened, not presented as
        # uniformly "now" — found live (2026-07-16) that an old,
        # topically-similar-but-unrelated exchange from ~15 minutes earlier
        # (vector-similarity match, no recency awareness) got surfaced and
        # stated as if it were the current conversation. A real timestamp
        # gives the model something concrete to reason about instead.
        context_parts = []

        for m in top_memories:
            context_parts.append(f"Relevant (from {m['created_at']}): {m['prompt']} -> {m['response']}")

        for r in recent:
            context_parts.append(f"Recent (from {r['created_at']}): {r['prompt']} -> {r['response']}")

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

        2026-07-16: one write, not two — memory/vector_memory were merged
        (they held identical data in two tables). Embedding is computed
        first but the memory row still gets written even if that fails
        (better a plain record than none), same graceful-degradation
        behavior the old two-call version had.
        """
        text = input_data.get("text", "")
        if not text or not response_text:
            return

        vec = None
        try:
            vec = embed(text)
        except Exception as e:
            logger.warning(f"⚠️ Failed to embed memory (storing without it): {e}")

        try:
            await add_memory(user_id, text, response_text, embedding=vec)
        except Exception as e:
            logger.warning(f"⚠️ Failed to store memory: {e}")