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

        t0 = time.time()
        intent = await classify_intent(text)
        logger.info(f"[TIMING] intent classification: {time.time() - t0:.2f}s")
        session["intent"] = intent

        # only log when something was actually detected — logging "none"
        # for every ordinary message would bury the signal in noise
        if intent.get("intent") != "none":
            logger.info(f"[ACTION] Intent classified for {user_id}: {intent} (from: {text!r})")

        return None
