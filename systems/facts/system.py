# systems/facts/system.py

"""
Fact System (Stabilized)

Responsibilities:
- Extract structured facts from user input
- Ignore questions
- Store clean, reliable values
- Inject facts into session
"""

import re

from core.system_base import BaseSystem
from db.db import fetch_user_facts, update_fact, delete_fact, log_security_event
from config.logger_config import logger
from systems.permissions.system import LOCKED_KEYS, OVERRIDE_ONLY_KEYS
from core.override_code import is_creator_override_code


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

# 2026-07-18 (Craig: "she can add things without it but to require it to
# remove them seems a bit off") — mirrors ALLOWED_FACT_KEYS/FACT_RULES
# above: same three casual, no-code fields, now removable the same way
# they're added. Real identity facts (role/user_name/codes) never go
# through this system at all, so there's nothing security-sensitive to
# accidentally expose here — only ever the low-stakes tier.
FACT_LABELS = {
    "favorite_color": ("favorite color", "favourite color"),
    "alias": ("name", "alias", "nickname"),
    "job": ("job", "occupation"),
}

FORGET_TRIGGER_RE = re.compile(r"\b(?:forget|remove|clear|delete)\s+my\s+(.+)$", re.IGNORECASE)


def extract_forget_key(text: str, existing_keys):
    """Returns the fact key to delete, or None. Deliberately narrow — an
    explicit forget/remove/clear/delete verb plus "my" plus a label, same
    strict-match philosophy as extract_fact_deterministic() below (avoids
    matching a merely-topic-adjacent remark like "forget it" or "never
    mind", which already mean something else entirely — see
    ws/ws_handlers.py's end-of-discussion detection).

    2026-07-18 (Craig: found a stray "personality" fact in his own
    profile that no current code even writes — confirmed live: only a
    since-removed classifier path or the unauthenticated /update_fact
    HTTP endpoint in api/routes.py could have put it there. The facts
    table was never actually limited to ALLOWED_FACT_KEYS in practice,
    so "we can't just hard code the removal fields" is correct — this
    now also matches the spoken label directly against whatever keys are
    ACTUALLY present for this user (existing_keys), not just the three
    with a hand-written friendly name. The caller (handle(), below)
    still gates the result through LOCKED_KEYS/OVERRIDE_ONLY_KEYS before
    ever deleting anything — this function only identifies WHICH key was
    named, not whether removing it is allowed."""
    match = FORGET_TRIGGER_RE.search(text.strip())
    if not match:
        return None

    # Prefix match, not exact-equals — "forget my job, thanks" or "forget
    # my job we're done here" both still name "job" first; real speech
    # (especially STT transcripts, which often carry no punctuation at
    # all) shouldn't need to end exactly there. \b keeps "job" from
    # matching a longer unrelated word like "jobless".
    remainder = match.group(1).strip().lower()

    for key, labels in FACT_LABELS.items():
        for label in labels:
            if re.match(rf"{re.escape(label)}\b", remainder):
                return key

    normalized = remainder.replace(" ", "_")
    for key in existing_keys:
        key_spaced = key.replace("_", " ")
        if re.match(rf"{re.escape(key)}\b", normalized) or re.match(rf"{re.escape(key_spaced)}\b", remainder):
            return key

    return None

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

    async def diagnose(self):
        """Real check: handle() depends on fetch_user_facts() every
        turn to build fact_context, regardless of whether a new fact
        was extracted this message."""
        try:
            await fetch_user_facts("craig")
        except Exception as e:
            return False, f"fetch_user_facts() raised: {e}"
        return True, ""

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        # -------------------------
        # LOAD FACTS (loaded first — the forget-path below needs the
        # real key set to match a spoken label against, and this same
        # dict is reused for fact_context at the end instead of a second
        # fetch)
        # -------------------------
        try:
            facts = await fetch_user_facts(user_id)
        except Exception:
            facts = {}

        # -------------------------
        # FORGET (checked first — "forget my name" would otherwise also
        # partially resemble an add-trigger's surrounding words)
        # -------------------------
        forget_key = extract_forget_key(text, facts.keys())

        if forget_key:
            # Same two-tier protection ADDING these fields already goes
            # through (systems/permissions/system.py) — removal can't be
            # looser than writing. LOCKED_KEYS never leaves conversation
            # at all; OVERRIDE_ONLY_KEYS needs the real override code
            # actually present in what was said (core/override_code.py),
            # not just asked for.
            #
            # 2026-07-18 (Craig: "she did remove my favorite color but
            # then made something odd up in response — mentions blue") —
            # a fact ADD is always naturally, truthfully acknowledgeable
            # (the user's own words already state the new value, so the
            # LLM has real content to reflect back), but a FORGET has no
            # equivalent: nothing in her context said what happened, so
            # she filled the gap with whatever was conversationally
            # nearby (a blue Corvette mentioned minutes earlier) instead
            # of admitting she had nothing real to say. fact_action_context
            # (read by systems/llm/system.py, one-shot) gives her the
            # actual, true outcome to work from instead of a blank.
            if forget_key in LOCKED_KEYS:
                logger.info(f"[SECURITY] Blocked casual removal of locked fact '{forget_key}' for {user_id}: {text!r}")
                await log_security_event(user_id, "fact_forget_blocked", f"locked field '{forget_key}': {text!r}")
                session["fact_action_context"] = (
                    f"Craig just asked you to forget his '{forget_key}' field, but that "
                    f"field is protected and can never be removed through casual "
                    f"conversation. Tell him plainly that you can't do that this way — "
                    f"don't invent a reason beyond it being a protected field."
                )
            elif forget_key in OVERRIDE_ONLY_KEYS and not await is_creator_override_code(text):
                logger.info(f"[SECURITY] Blocked removal of override-only fact '{forget_key}' for {user_id} (no override code): {text!r}")
                await log_security_event(user_id, "fact_forget_blocked", f"override-only field '{forget_key}' attempted without override code: {text!r}")
                session["fact_action_context"] = (
                    f"Craig just asked you to forget his '{forget_key}' field, but that "
                    f"needs his real override code stated in the same request and it "
                    f"wasn't there. Tell him plainly he needs to include the override "
                    f"code to remove that one."
                )
            else:
                try:
                    await delete_fact(user_id, forget_key)
                    facts.pop(forget_key, None)
                    logger.info(f"[ACTION] Fact forgotten for {user_id}: {forget_key} (from: {text!r})")
                    session["fact_action_context"] = (
                        f"You just permanently removed the '{forget_key}' fact from "
                        f"Craig's profile, per his request. Confirm this plainly. Do "
                        f"NOT invent, guess, or restate what the old value used to be, "
                        f"and do NOT suggest or assign a new value — it is simply gone."
                    )
                except Exception as e:
                    print(f"⚠️ Fact delete error: {e}")

        # -------------------------
        # EXTRACT + STORE
        # -------------------------
        else:
            key, value = extract_fact(text, session)

            if key and value:
                try:
                    await update_fact(user_id, key, value)
                    facts[key] = value
                    logger.info(f"[ACTION] Fact set for {user_id}: {key} = {value!r} (from: {text!r})")
                except Exception as e:
                    print(f"⚠️ Fact store error: {e}")

        # -------------------------
        # BUILD CONTEXT
        # -------------------------
        # 2026-07-16: found live — edit_code/override_code/role were
        # included here unfiltered, meaning the LLM prompt (built from
        # this on every single turn, not just identity-related ones) had
        # ambient access to real security codes and could, and did,
        # blurt one out verbatim in an ordinary conversational reply.
        # LOCKED_KEYS already exists in permissions/system.py to protect
        # these fields from being *written* via conversation — reusing
        # the same list here closes the matching *read* exposure, rather
        # than defining a second list that could drift out of sync.
        safe_facts = {k: v for k, v in facts.items() if k not in LOCKED_KEYS}

        if safe_facts:
            lines = [f"{k} = {v}" for k, v in safe_facts.items()]
            fact_text = "\n".join(lines)
        else:
            fact_text = ""

        # -------------------------
        # STORE IN SESSION
        # -------------------------
        session["fact_context"] = fact_text

        return None