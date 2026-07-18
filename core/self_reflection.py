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
import re
import random

from db.db import (
    fetch_recent_memory_all, get_personality, set_personality,
    log_personality_change, get_learned_phrase, set_learned_phrase,
    queue_curiosity_question, get_personality_hard_rules,
    get_last_reflection_memory_id, set_last_reflection_memory_id,
    get_seconds_since_last_activity, get_seconds_since_last_personality_change
)
from llm.ollama_client import ollama_manager
from core.phrasebook import PHRASE_REGISTRY
from core.text_utils import strip_emojis
from config.logger_config import logger

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

MIN_CONVERSATIONS_FOR_REFLECTION = 3

# 2026-07-17 (Craig: "have her know if there's room for her to make
# changes, like a downtime, and kick off the things she wants to
# adjust"): self-reflection now waits for a real lull in actual
# conversation before doing any work, rather than firing on a blind
# timer regardless of whether Craig is mid-conversation right now — the
# exact thing the 180s startup delay (main.py) was already a narrower
# fix for. A quiet stretch is a reasonable proxy for "safe to make
# changes without competing with active use for the same Ollama
# instance." Starting point, not tuned — long enough that she's very
# unlikely to still be mid-conversation, short enough that a real lull
# doesn't sit unused for long.
IDLE_BEFORE_REFLECTION_S = 300


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
    """
    2026-07-16: the placeholder-preservation instruction used to say
    "keep any {placeholder} markers... intact" as a generic example —
    found live, real bug: the model (qwen2.5, already known to take
    instructions too literally for this project — same lesson as the
    intent classifier's prompt-length regression) sometimes echoed that
    literal example text into phrases that had NO real placeholders at
    all (confirmed: onboard_name_too_short, denial_not_privileged,
    denial_not_verified all came back with a bogus trailing
    "{placeholder}" that isn't a real key anywhere). get_phrase()'s
    .format() call safely falls back to the default when this happens
    (a missing kwarg raises, caught, default used) — so it never broke
    anything user-facing, but it silently discarded the personality
    rewording every time it happened.

    Fixed two ways: (1) only mention placeholder preservation when the
    CURRENT text actually has real ones, naming them explicitly (e.g.
    "{name}") instead of the generic word "placeholder" — nothing left
    for the model to misinterpret when there's nothing to preserve;
    (2) a real structural check below (not just a better prompt) rejects
    any reword that doesn't preserve the exact same placeholder set,
    instead of trusting the model to have followed instructions.
    """
    default_text, intent = PHRASE_REGISTRY[key]
    current = await get_learned_phrase(key, default=default_text)

    required_placeholders = set(_PLACEHOLDER_RE.findall(current))

    if required_placeholders:
        names = ", ".join(f"{{{p}}}" for p in sorted(required_placeholders))
        placeholder_instruction = (
            f" This phrase uses {names} — keep those exact tokens, spelled "
            f"exactly like that, somewhere in your new wording, since other "
            f"code fills them in. Don't add any other {{curly-brace}} tokens."
        )
    else:
        placeholder_instruction = ""

    prompt = f"""You are A.L.E.X. Your personality: "{personality}"

One of your standard lines needs to keep doing its job (its purpose: {intent}), but you're free to phrase it however fits who you are.

Current wording: "{current}"

Do you want to rephrase this to better match your personality? Keep the exact same functional purpose.{placeholder_instruction} Respond with ONLY a JSON object:
{{"changed": true, "new_text": "<new wording>"}}
or
{{"changed": false}}"""

    result = await ollama_manager.generate_json(prompt, timeout=20.0)

    if not result or not result.get("changed"):
        return None

    new_text = str(result.get("new_text", "")).strip()

    if not new_text or new_text == current:
        return None

    # Structural safety net, not just trusting the prompt: reject a
    # reword outright if it doesn't preserve exactly the placeholders
    # this phrase actually needs — either dropped one (breaks the
    # phrase's real function) or hallucinated an extra one (exactly
    # today's bug, now caught even if the prompt fix above ever slips).
    if set(_PLACEHOLDER_RE.findall(new_text)) != required_placeholders:
        logger.warning(
            f"⚠️ Rejected reword for '{key}': placeholder mismatch "
            f"(needed {required_placeholders}, got {new_text!r})"
        )
        return None

    return new_text


async def _reflect_on_curiosity(recent):
    """Component 11's self-initiated curiosity trigger (2026-07-16): during
    this same reflection pass, notice a real, nameable topic she doesn't
    actually have knowledge about, rather than only reacting when a live
    conversation happens to expose the gap. A judgment call, not a
    deterministic check, so — same as _reflect_on_personality/
    _reflect_on_phrase above — this trusts LLM judgment directly, no
    creator approval gate; queued for delivery at the next verified
    connect (ws/ws_handlers.py), not spoken mid-conversation."""
    convo_text = "\n".join(
        f"{r['user']}: {r['prompt']}\nALEX: {r['response']}" for r in recent
    )

    prompt = f"""You are A.L.E.X, privately reviewing your recent conversations.

Recent conversations:
{convo_text}

Is there a specific, distinct topic mentioned here that you genuinely don't have real knowledge about, that would be worth asking your creator to explain later? Only say yes if it's a real, nameable topic — not vague curiosity.

Respond with ONLY a JSON object:
{{"curious": true, "topic": "<short topic>", "question": "<one natural sentence asking about it>"}}
or
{{"curious": false}}"""

    result = await ollama_manager.generate_json(prompt, timeout=20.0)

    if not result or not result.get("curious"):
        return None

    topic = str(result.get("topic", "")).strip()[:200]
    question = str(result.get("question", "")).strip()[:300]

    if not topic or not question:
        return None

    return topic, question


async def run_self_reflection():
    # Idle gate, checked first and cheaply (no LLM call) — don't even look
    # at whether there's new conversation to reflect on until there's
    # been a real lull. Two independent signals, whichever is MORE
    # RECENT wins (the smaller "seconds ago" value): real conversation
    # (memory) and a direct personality edit (personality_log — set via
    # the Controller or a chat override, neither of which ever touch
    # `memory` at all). Found live (2026-07-17, Craig: "I modified her
    # personality via the controller" right after being told nothing new
    # had happened) — a Controller edit is real engagement and should
    # delay the next autonomous pass the same way a real conversation
    # does, not be invisible to the gate. None from either source means
    # "not idle enough to tell" rather than assumed infinitely idle.
    seconds_since_conversation = await get_seconds_since_last_activity()
    seconds_since_personality_edit = await get_seconds_since_last_personality_change()

    candidates = [s for s in (seconds_since_conversation, seconds_since_personality_edit) if s is not None]
    if not candidates:
        return

    seconds_idle = min(candidates)
    if seconds_idle < IDLE_BEFORE_REFLECTION_S:
        return

    # 2026-07-17: found live — Craig noticed personality drifting every
    # 15-30 minutes with zero real interaction behind it. Root cause:
    # this used to sample the most recent 20 conversation turns
    # UNCONDITIONALLY on every pass, with no memory of what it already
    # reflected on last time. Shortening the interval to 900s (from
    # 3600s, so evolution would be visible within a session) turned that
    # latent issue into a real, visible one — during any quiet stretch,
    # every single pass was re-reflecting on the exact same stale
    # conversation window, and the model's own sampling variance (not
    # temperature=0, unlike the classifiers — genuine variety is wanted
    # here) produced a slightly different reworded personality each time,
    # a random walk with no real signal behind it. Fixed by bookmarking
    # the newest memory row id actually reflected on and only proceeding
    # if there are genuinely NEW turns since then.
    last_id = await get_last_reflection_memory_id()
    recent = await fetch_recent_memory_all(limit=20, since_id=last_id)

    if len(recent) < MIN_CONVERSATIONS_FOR_REFLECTION:
        return

    await set_last_reflection_memory_id(recent[-1]["id"])

    try:
        curiosity = await _reflect_on_curiosity(recent)
    except Exception as e:
        logger.warning(f"⚠️ Curiosity reflection failed: {e}")
        curiosity = None

    if curiosity:
        topic, question = curiosity
        await queue_curiosity_question(topic, question)
        logger.info(f"[ACTION] Queued curiosity question: {question}")

    personality_change = await _reflect_on_personality(recent)

    if not personality_change:
        return

    new_desc, reason = personality_change

    await set_personality(new_desc)
    await log_personality_change(new_desc, reason, kind="personality")

    logger.info(f"[PERSONALITY] Personality evolved: {new_desc} (reason: {reason})")

    # personality shifted — let her optionally re-voice her scripted
    # phrases too. Capped to a small random sample per pass, NOT the
    # whole registry — found live (2026-07-16) that re-voicing all 78
    # entries (grown from ~4 when this was first written) meant every
    # single personality change kicked off ~78 sequential LLM calls,
    # monopolizing the one shared Ollama instance for several minutes at
    # a time. Since this fires immediately on every restart (see
    # main.py's periodic_self_reflection(), no initial delay) and
    # personality changes happened often tonight, this was directly
    # competing with — and badly starving — real conversational requests
    # the whole time it ran. Phrases still drift toward her personality
    # over time, just gradually across many reflection passes instead of
    # all at once.
    keys_to_revoice = random.sample(list(PHRASE_REGISTRY), min(5, len(PHRASE_REGISTRY)))

    # Same deterministic guarantee as systems/llm/system.py's generation
    # stream — found live that a reworded phrase ("Yas, gotcha! I'll
    # update my records and give the old info the ol' boot. 👍") carried
    # an emoji right through this same rewording path, independent of the
    # main conversational generation.
    hard_rules = await get_personality_hard_rules()
    suppress_emojis = any("emoji" in r.lower() for r in hard_rules)

    for key in keys_to_revoice:
        try:
            new_text = await _reflect_on_phrase(key, new_desc)
        except Exception as e:
            logger.warning(f"⚠️ Phrase reflection failed for '{key}': {e}")
            continue

        if new_text and suppress_emojis:
            new_text = strip_emojis(new_text)

        if new_text:
            phrase_reason = f"re-voiced to match new personality ({reason})"
            await set_learned_phrase(key, new_text)
            await log_personality_change(new_text, phrase_reason, kind=f"phrase:{key}")
            logger.info(f"[PERSONALITY] Re-voiced '{key}': {new_text} (reason: {phrase_reason})")
