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


# Minimum cosine similarity for a stored memory to be offered as "Relevant:"
# context. See the selection site below for why this exists and why the number
# is a reasoned starting point rather than a tuned one.
MEMORY_RELEVANCE_FLOOR = 0.45

# How many turns she is shown, and a ceiling on what they may add up to.
#
# 2026-09-21. The window was 4 turns, by a `recent[-4:]` slice below. The
# commit the night before "raised it to twelve" by changing the fetch limit
# and left the slice in place, so nothing reached her that had not before —
# and its diagnosis was wrong anyway: on every turn of the "thrilling"
# argument her own line WAS inside the four turns she was given, checked row
# by row against `memory`. The window was not why she argued (that was the
# unconfirmed beliefs block, see systems/llm/system.py). It is still too
# short for continuity, and the reason to bound it by size rather than by a
# turn count is a real hazard: the prompt shares num_ctx=4096 with a
# 300-token reply, and Ollama truncates an over-long prompt from the FRONT,
# silently — the first thing to go would be "You are A.L.E.X." and
# everything after it.
#
# Measured on her real rows, rendered exactly as below (user=craig, last
# 400 turns):
#
#   window      median    p90    p99    max   (chars)
#   4 turns       992    1422   2538   4059
#   8 turns      2013    2745   3883   5467
#   12 turns     3003    4026   5191   6254
#   one turn      232      --    635   2740
#
# The prompt measured ~2670 tokens with the old 4-turn window and ~1400
# spare. 4000 chars is roughly 1000 tokens: twelve ordinary turns fit at the
# median and nearly at p90, the long tail (recall dumps, monologues) drops
# the OLDEST turns instead of the system prompt, and there is headroom left.
MEMORY_WINDOW_TURNS = 12
MEMORY_CONTEXT_MAX_CHARS = 4000


def _fit_recent_to_budget(parts, budget):
    """Drops the oldest turns until the rendered window fits `budget`.

    Never drops the newest one: a single oversized turn (a recall dump) is
    still the thing he just heard, and cutting it would recreate the exact
    fault a memory window exists to prevent."""
    kept = list(parts)
    while len(kept) > 1 and sum(len(p) + 1 for p in kept) > budget:
        kept.pop(0)
    return kept


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

        # 🔥 keep it SMALL (performance critical), and only if it's actually
        # relevant. 2026-09-20: this used to take scored[:2] unconditionally,
        # so the two least-unrelated memories were injected as "Relevant:"
        # context on EVERY turn even when nothing stored had anything to do
        # with the question. Recall with no floor is not recall, it is just
        # the top of a sorted list.
        #
        # This matters beyond noise: whatever gets injected, she treats as
        # established. Craig hit the extreme version on 2026-09-20 — one
        # spurious line about chlorophyll entered the record and she then
        # asserted "you were going on and on about how green everything is",
        # attributing her own invention to him. A floor does not fix that case
        # (it arrived through the recency window below, where a relevance
        # filter would break "what did I just ask you?"), but unrelated
        # memories being presented as relevant is the same class of problem.
        #
        # Untuned starting point, stated as such: the project's own measured
        # reference points are 0.63-0.70 for genuine greeting-vs-greeting
        # similarity and 0.85 for a confident factual match, so 0.45 sits well
        # below "related" while still excluding the clearly unconnected.
        top_memories = [m for score, m in scored[:2] if score >= MEMORY_RELEVANCE_FLOOR]

        # -------------------------
        # RECENT MEMORY
        # -------------------------
        # Fetched by count, kept by size — see MEMORY_WINDOW_TURNS and
        # MEMORY_CONTEXT_MAX_CHARS above. The trimming happens after the
        # rows are rendered, because the size that matters is the rendered
        # one.
        try:
            recent = await fetch_recent_memory(user_id, limit=MEMORY_WINDOW_TURNS)
        except:
            recent = []

        # -------------------------
        # BUILD CONTEXT STRING
        # -------------------------
        # Each line is labeled with when it happened, not presented as
        # uniformly "now" — found live (2026-07-16) that an old,
        # topically-similar-but-unrelated exchange from ~15 minutes earlier
        # (vector-similarity match, no recency awareness) got surfaced and
        # stated as if it were the current conversation. A real timestamp
        # gives the model something concrete to reason about instead.
        # 2026-09-20 — formatted as dialogue rather than as data rows.
        #
        # Craig caught her emitting a context line verbatim into a reply:
        # "Did you write the error message yourself or did you let me handle
        # that part? Recent (from 2026-09-21 05:26:09): you testing your
        # same systems repeatedly. -> What's next on the agenda, Craig?"
        #
        # Not truncation — the prompt was at 65% of num_ctx with 1400
        # tokens spare. The old shape was the problem:
        #
        #     Recent (from <timestamp>): <prompt> -> <response>
        #
        # A labelled row with an arrow in it reads as a template to
        # continue, and a model given several of them in a list will
        # sometimes produce another one instead of an answer. Written as
        # speech it is unambiguous: these are things that were SAID, and
        # the next thing to produce is a reply rather than another row.
        #
        # The timestamp stays — it exists because an old vector-matched
        # exchange once got stated as if it were the current conversation,
        # and she needs something concrete to date it by.
        def _as_dialogue(row, label):
            said = (row["prompt"] or "").strip()
            replied = (row["response"] or "").strip()

            # Her own unprompted turns have a marker in the prompt column
            # rather than anything he said — see db.remember_own_utterance().
            if said.startswith("(unprompted"):
                return f'[{label} {row["created_at"]}] You said, unprompted: "{replied}"'

            return (f'[{label} {row["created_at"]}] He said: "{said}"\n'
                    f'    You answered: "{replied}"')

        earlier_parts = [_as_dialogue(m, "earlier") for m in top_memories]
        recent_parts = _fit_recent_to_budget(
            [_as_dialogue(r, "recent") for r in recent],
            MEMORY_CONTEXT_MAX_CHARS - sum(len(p) + 1 for p in earlier_parts))

        context_text = "\n".join(earlier_parts + recent_parts)

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