# systems/intent/system.py
"""
Intent Classification System

Runs once per message, one LLM call, and classifies it (fact statement /
authorized update command / self-status question / none). Never responds
directly — it stages the result in session["intent"] for facts,
permissions, and diagnostics to read, so those systems don't each need
their own separate classification call (and don't need hardcoded trigger-
phrase lists either).
"""
import time

from core.system_base import BaseSystem
from core.intent_classifier import classify_intent
from core import prompt_head
from core.text_utils import has_content_words
from db.db import fetch_recent_memory
from config.logger_config import logger


class System(BaseSystem):

    name = "intent"
    priority = 5  # before permissions(6)/facts(7)/diagnostics(9)

    async def init(self):
        print("🧭 Intent classifier ready")

    async def diagnose(self):
        """Deliberately lightweight — a genuine check would mean a real
        LLM round-trip on every diagnostic run, and Ollama's own
        reachability is already covered separately (diagnostic_tool's
        dedicated check). This only confirms the classifier itself is
        actually callable, not that a live classification succeeds."""
        if not callable(classify_intent):
            return False, "classify_intent is not callable"
        return True, ""

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        # 2026-09-21: the deliberation needs (core/deliberation.py) ride on
        # this same call for messages with content words — measured
        # 57/58 intent agreement with the plain call across real and
        # security-relevant utterances, 2.1s against 3.0s for two calls.
        # Content-free turns ("okay", "it's me") keep the plain 0.8s call.
        with_needs = has_content_words(text)
        recent_lines = None
        if with_needs:
            try:
                recent_lines = [
                    f'He: "{(r["prompt"] or "")[:120]}" / You: "{(r["response"] or "")[:120]}"'
                    for r in await fetch_recent_memory(user_id, limit=3)
                    if not (r["prompt"] or "").startswith("(unprompted")]
            except Exception:
                recent_lines = None

        t0 = time.time()
        # 2026-09-25: tried with her prompt head (core/prompt_head.py) as
        # the system message so this call and her reply would share Ollama's
        # prefix cache. Measured the same hour: the intent suite fell from
        # 84/84 to 76/84 — seven "I'm testing..." sentences read as status
        # checks with her rules in front of the classifier — and the cache
        # was not shared anyway (a system message is re-evaluated in full by
        # this Ollama/model; see ANOMALIES.md). So: no head here.
        intent = await classify_intent(text, with_needs=with_needs, recent_lines=recent_lines)
        logger.info(f"[TIMING] intent classification: {time.time() - t0:.2f}s"
                    + (" (with deliberation needs)" if with_needs else ""))
        session["needs"] = intent.pop("needs", None)
        persona = int(intent.pop("persona", 0) or 0)
        session["intent"] = intent

        # 2026-09-25 (Craig: "would it be possible for her to determine based
        # on the sentence whether something is personality related and THEN
        # kick on that process... if it's the creator role talking then ask
        # if that was meant to be a personality adjustment"). The judgement
        # rides on this call; systems/controller/_personality.py decides
        # what to do with the score — nothing at all for anyone but him.
        if persona >= 4:
            from systems.controller import _personality
            try:
                result = await _personality.on_persona_score(session, user_id, text, text.lower().strip(), persona)
            except Exception as e:
                logger.warning(f"⚠️ persona handling failed: {e}")
                result = None
            if result:
                return result

        # only log when something was actually detected — logging "none"
        # for every ordinary message would bury the signal in noise
        if intent.get("intent") != "none":
            logger.info(f"[ACTION] Intent classified for {user_id}: {intent} (from: {text!r})")

        return None
