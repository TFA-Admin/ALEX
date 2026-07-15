# systems/facts/system.py

"""
Fact System (Stabilized)

Responsibilities:
- Extract structured facts from user input
- Ignore questions
- Store clean, reliable values
- Inject facts into session
"""

from core.system_base import BaseSystem
from db.db import fetch_user_facts, update_fact
from config.logger_config import logger


# -------------------------
# RULE DEFINITIONS (clean + extendable)
# -------------------------
#
# 🔒 Hard allowlist — enforced in code below, independent of what the LLM
# extractor returns. Never includes "role", "edit_code", "override_code",
# "user_name" etc: those are identity/security-critical and can only ever
# be changed through their own protected flows (permissions/system.py,
# creator bootstrap), never through casual conversation.

ALLOWED_FACT_KEYS = ["favorite_color", "alias", "job"]

FACT_RULES = [
    {
        "key": "favorite_color",
        "patterns": ["favorite color is"]
    },
    {
        # 🔒 NOT "user_name" — that's the protected identity fact
        # (override-gated in permissions/system.py, tied to login/voice
        # profile lookup). "call me"/"my name is" said mid-conversation is
        # casual and unconfirmed, so it only ever sets a freely-changeable
        # alias, never the real identity.
        "key": "alias",
        "patterns": ["my name is", "call me", "i go by"]
    },
    {
        "key": "job",
        "patterns": ["my job is", "i work as"]
    }
]

HYPOTHETICALS = ["what if", "if ", "suppose", "imagine", "pretend", "would be"]

# 🔒 Deterministic guard on the "alias" VALUE, not the trigger phrase — the
# classifier is asked to recognize "I am Mary" as a name statement without
# requiring the literal "call me"/"my name is" prefix, but that same looser
# reading lets state/status words through too (confirmed live: "I am
# married" got extracted as alias="married"). A name and a marital/mood
# status aren't distinguishable by keyword pattern, only by meaning — so
# this blocks the specific known-bad category (common non-name self-
# descriptors) rather than trying to whitelist all valid names.
NOT_A_NAME = {
    "married", "single", "divorced", "widowed", "engaged", "separated",
    "tired", "hungry", "thirsty", "sad", "happy", "angry", "busy", "sick",
    "fine", "okay", "ok", "good", "bad", "great", "ready", "done", "here",
    "home", "confused", "stressed", "excited", "bored", "sorry",
}


def is_declarative(text: str) -> bool:
    """
    Deterministic safety check — kept as fixed logic (not LLM judgment)
    because getting this wrong lets hypothetical/rhetorical statements get
    stored as if they were real. Used by the deterministic fallback below;
    the shared classifier (core/intent_classifier.py) applies the same
    check to its own output before this module ever sees it.
    """
    lower = text.strip().lower()

    if "?" in lower:
        return False

    return not any(h in lower for h in HYPOTHETICALS)


# -------------------------
# CLEAN VALUE
# -------------------------

def clean_value(value: str):
    value = value.strip()

    # remove punctuation
    value = value.strip(".").strip("!").strip(",")

    # basic sanity checks
    if not value:
        return None

    if len(value) > 50:
        return None

    return value


# -------------------------
# FACT EXTRACTION
# -------------------------

def extract_value_for_key(text: str, key: str):
    """
    Pulls the actual value from the user's own text via the same trigger
    phrases as the deterministic extractor below, but matched ANYWHERE in
    the text (not just as a prefix) — used even when the LLM classifier
    correctly identified the category, because its own extracted "value"
    field is unreliable under JSON-constrained decoding (confirmed live,
    repeatedly: "call me Craig" -> value "user"/"assistant"/"You" instead
    of "Craig"). The LLM's job stays limited to "is this a fact, and which
    kind" — the actual substring always comes from a real match in what
    the user said, never from the model's own generation.
    """
    lower = text.strip().lower()

    for rule in FACT_RULES:
        if rule["key"] != key:
            continue

        for pattern in rule["patterns"]:
            if pattern in lower:
                idx = lower.rfind(pattern)
                raw_value = text[idx + len(pattern):]
                return clean_value(raw_value)

    return None


def extract_fact_deterministic(text: str):
    """Fixed-pattern extraction — used when the LLM is unavailable, or as
    the ground-truth path this whole module used to rely on exclusively."""
    lower = text.strip().lower()

    if not is_declarative(lower):
        return None, None

    for rule in FACT_RULES:
        key = rule["key"]

        for pattern in rule["patterns"]:
            # 🔥 STRICT MATCH (prevents accidental writes)
            if lower.startswith(pattern):
                value = lower.split(pattern)[-1].strip()
                value = clean_value(value)

                if value:
                    return key, value

    return None, None


def extract_fact(text: str, session: dict):
    """
    Reads the shared classification already done once per message by
    systems/intent/system.py (session["intent"]) — no separate LLM call
    here. Falls back to the deterministic pattern matcher when the
    classifier didn't find a fact (a genuine negative, or it being
    unavailable are indistinguishable, so the cheap fallback always runs
    regardless — costs nothing when it also finds nothing).
    """
    intent = session.get("intent") or {}

    if intent.get("intent") == "fact":
        key = intent.get("key")

        if key in ALLOWED_FACT_KEYS:
            value = extract_value_for_key(text, key)
            if value and not (key == "alias" and value.lower() in NOT_A_NAME):
                return key, value

    return extract_fact_deterministic(text)


# -------------------------
# SYSTEM CLASS
# -------------------------

class System(BaseSystem):

    name = "facts"
    priority = 7

    async def init(self):
        print("📚 Fact system ready")

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        # -------------------------
        # EXTRACT + STORE
        # -------------------------
        key, value = extract_fact(text, session)

        if key and value:
            try:
                await update_fact(user_id, key, value)
                logger.info(f"[ACTION] Fact set for {user_id}: {key} = {value!r} (from: {text!r})")
            except Exception as e:
                print(f"⚠️ Fact store error: {e}")

        # -------------------------
        # LOAD FACTS
        # -------------------------
        try:
            facts = await fetch_user_facts(user_id)
        except:
            facts = {}

        # -------------------------
        # BUILD CONTEXT
        # -------------------------
        if facts:
            lines = [f"{k} = {v}" for k, v in facts.items()]
            fact_text = "\n".join(lines)
        else:
            fact_text = ""

        # -------------------------
        # STORE IN SESSION
        # -------------------------
        session["fact_context"] = fact_text

        return None