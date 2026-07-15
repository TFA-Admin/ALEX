# core/phrasebook.py
"""
Dynamic phrase bank.

Every scripted line ALEX says has two parts: what it needs to ACCOMPLISH
(the functional intent — e.g. "elicit the person's name") and the actual
WORDING used to accomplish it. The intent stays fixed, since other code
depends on it (e.g. identity_manager expects a response to the greeting
that can be parsed for a name). The wording is hers — the self-reflection
loop can rewrite it any time, no approval needed, via set_learned_phrase().

get_phrase() always has a hardcoded default as a safety net, so a missing
or corrupted stored phrase never breaks a flow — it just falls back to the
plain, functional default wording.
"""
from db.db import get_learned_phrase

# key -> (default_text, functional_intent — used by the reflection loop
# when it rewrites a phrase, to keep the purpose intact)
PHRASE_REGISTRY = {
    "greeting_new_session": (
        "Hello, who am I speaking with?",
        "Ask who you're speaking with, in a way that invites them to say their name."
    ),
    "greeting_returning_user": (
        "Welcome back, {name}.",
        "Greet someone you've already recognized by name. {name} is a placeholder for their name — keep it in the phrase."
    ),
    "voice_enroll_intro": (
        "Let's learn your voice so I can recognize you later.",
        "Tell the person you're about to learn their voice, before asking them to speak."
    ),
    "voice_verify_prompt": (
        "Please say a short phrase so I can verify it's you.",
        "Ask the person to say something so you can verify their voice matches who they claim to be."
    ),
}


async def get_phrase(key: str, **kwargs) -> str:
    default_text, _ = PHRASE_REGISTRY[key]
    text = await get_learned_phrase(key, default=default_text)

    try:
        return text.format(**kwargs)
    except Exception:
        # a rewritten phrase that broke its {placeholder} shouldn't ever
        # crash a live conversation — fall back to the known-good default
        return default_text.format(**kwargs)
