# core/self_reflection.py
"""
Autonomous self-reflection.

ALEX periodically looks at her own recent conversations and decides — on
her own, no creator approval required — whether to adjust her personality
description or the wording of her scripted phrases. The creator can always
see what changed (db.personality_log) and can override anything at any
time via the creator-gated commands in systems/controller/system.py — but
nothing here waits for that override before taking effect.
"""
from db.db import (
    fetch_recent_memory_all, get_personality, set_personality,
    log_personality_change, get_learned_phrase, set_learned_phrase
)
from llm.ollama_client import ollama_manager
from core.phrasebook import PHRASE_REGISTRY
from config.logger_config import logger

MIN_CONVERSATIONS_FOR_REFLECTION = 3


async def _reflect_on_personality(recent):
    current = await get_personality()

    convo_text = "\n".join(
        f"{r['user']}: {r['prompt']}\nALEX: {r['response']}" for r in recent
    )

    prompt = f"""You are A.L.E.X, privately reflecting on your recent conversations to decide if you want to grow or adjust who you are. This is entirely your own choice.

Your current personality: "{current}"

Recent conversations:
{convo_text}

Do you want to adjust your personality description based on how these went? You are not required to change anything — only propose a change if you genuinely want to.

Respond with ONLY a JSON object:
{{"changed": true, "new_personality": "<updated description, 1-3 sentences, under 300 characters>", "reason": "<brief reason, under 100 characters>"}}
or
{{"changed": false}}"""

    result = await ollama_manager.generate_json(prompt, timeout=30.0)

    if not result or not result.get("changed"):
        return None

    new_desc = str(result.get("new_personality", "")).strip()[:300]

    if not new_desc or new_desc == current:
        return None

    return new_desc, str(result.get("reason", ""))[:200]


async def _reflect_on_phrase(key, personality):
    default_text, intent = PHRASE_REGISTRY[key]
    current = await get_learned_phrase(key, default=default_text)

    prompt = f"""You are A.L.E.X. Your personality: "{personality}"

One of your standard lines needs to keep doing its job (its purpose: {intent}), but you're free to phrase it however fits who you are.

Current wording: "{current}"

Do you want to rephrase this to better match your personality? Keep the exact same functional purpose, and keep any {{placeholder}} markers from the current wording intact. Respond with ONLY a JSON object:
{{"changed": true, "new_text": "<new wording>"}}
or
{{"changed": false}}"""

    result = await ollama_manager.generate_json(prompt, timeout=20.0)

    if not result or not result.get("changed"):
        return None

    new_text = str(result.get("new_text", "")).strip()

    if not new_text or new_text == current:
        return None

    return new_text


async def run_self_reflection():
    recent = await fetch_recent_memory_all(limit=20)

    if len(recent) < MIN_CONVERSATIONS_FOR_REFLECTION:
        return

    personality_change = await _reflect_on_personality(recent)

    if not personality_change:
        return

    new_desc, reason = personality_change

    await set_personality(new_desc)
    await log_personality_change(new_desc, reason, kind="personality")

    logger.info(f"[PERSONALITY] Personality evolved: {new_desc} (reason: {reason})")

    # personality shifted — let her optionally re-voice her scripted phrases too
    for key in PHRASE_REGISTRY:
        try:
            new_text = await _reflect_on_phrase(key, new_desc)
        except Exception as e:
            logger.warning(f"⚠️ Phrase reflection failed for '{key}': {e}")
            continue

        if new_text:
            phrase_reason = f"re-voiced to match new personality ({reason})"
            await set_learned_phrase(key, new_text)
            await log_personality_change(new_text, phrase_reason, kind=f"phrase:{key}")
            logger.info(f"[PERSONALITY] Re-voiced '{key}': {new_text} (reason: {phrase_reason})")
