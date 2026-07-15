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
from core.system_base import BaseSystem
from core.intent_classifier import classify_intent
from config.logger_config import logger


class System(BaseSystem):

    name = "intent"
    priority = 5  # before permissions(6)/facts(7)/diagnostics(9)

    async def init(self):
        print("🧭 Intent classifier ready")

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        intent = await classify_intent(text)
        session["intent"] = intent

        # only log when something was actually detected — logging "none"
        # for every ordinary message would bury the signal in noise
        if intent.get("intent") != "none":
            logger.info(f"[ACTION] Intent classified for {user_id}: {intent} (from: {text!r})")

        return None
