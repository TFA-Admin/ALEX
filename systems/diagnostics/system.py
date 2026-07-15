# systems/diagnostics/system.py
"""
Diagnostics System

Answers "are you okay / check your systems / is everything working"
questions with real, just-gathered facts about her own operational state.

This responds DIRECTLY with a deterministically-built message rather than
staging context for the general LLM system to phrase — tried that first,
and even with an explicit "don't add anything beyond these facts" rule,
the model repeatedly added invented troubleshooting advice ("check your
GPU drivers", "consult a technician") that was never in the gathered
data. For a feature whose entire point is trustworthy self-reporting,
that's not an acceptable failure mode, so this trades the LLM's
personality-flavored phrasing for a guarantee: what she reports is
exactly what was measured, nothing more.
"""
import httpx

from core.system_base import BaseSystem

# Deterministic, not a classifier call — a casual presence/hearing check
# ("can you hear me?") is a small, enumerable phrase space (same reasoning
# as the personality "reset" trigger list), and it needs a different
# ANSWER than a real diagnostic request, not a different classification.
# status_check stays broad on purpose (it's what stops "are you okay"
# from reaching the LLM and getting hallucinated advice) — this only
# changes what she says once she's already been routed here.
CASUAL_PRESENCE_CHECKS = (
    "can you hear me", "can you hear", "are you there",
    "are you listening", "do you hear me", "are you hearing me",
)


class System(BaseSystem):

    name = "diagnostics"
    priority = 9  # after memory (8), before modules (10) / llm (100)

    async def init(self):
        print("🩺 Diagnostics system ready")

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        # reads the shared classification already done once per message by
        # systems/intent/system.py — no separate LLM call here
        intent = session.get("intent") or {}

        if intent.get("intent") != "status_check":
            return None

        lower = text.strip().lower()

        if any(phrase in lower for phrase in CASUAL_PRESENCE_CHECKS):
            # Grounded, not a guess: the message was received, transcribed,
            # and reached this handler at all, which IS the confirmation —
            # no need for the full system-by-system dump for a question
            # this narrow.
            return {
                "type": "response",
                "content": "Yes, I can hear you."
            }

        status = await self._gather()

        return {
            "type": "response",
            "content": status
        }

    async def _gather(self) -> str:
        from core.alex_core import alex_core
        from llm.ollama_client import ollama_manager
        from db.db import fetch_recent_memory_all

        expected = [
            "controller", "command", "intent", "permissions", "facts",
            "memory", "diagnostics", "modules", "llm"
        ]
        loaded = set(alex_core.systems.systems.keys())

        lines = [f"{s}: {'online' if s in loaded else 'offline'}" for s in expected]

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(ollama_manager.host)
                ollama_status = "online" if r.status_code == 200 else "error"
        except Exception:
            ollama_status = "offline"
        lines.append(f"ollama: {ollama_status}")

        try:
            await fetch_recent_memory_all(limit=1)
            db_status = "online"
        except Exception:
            db_status = "error"
        lines.append(f"database: {db_status}")

        try:
            from speech.stt_engine import FORCE_STT_CPU
            stt_mode = "cpu" if FORCE_STT_CPU else "gpu"
        except Exception:
            stt_mode = "error"
        lines.append(f"stt: {stt_mode}")

        return "\n".join(lines)
