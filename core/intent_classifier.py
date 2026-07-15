# core/intent_classifier.py
"""
Single shared intent classification pass.

Previously facts/system.py, systems/permissions/system.py, and
systems/diagnostics/system.py each ran their own hardcoded keyword
pre-filter before deciding whether to make an LLM call — meaning natural
phrasings that didn't match a fixed word list got silently missed (e.g.
"can you perform a diagnostic on yourself" didn't match a "diagnostic
check"-style list). Removing the pre-filters entirely and letting each
system make its own LLM call instead would triple the classification
overhead on every single message.

This module exists so that cost is paid exactly once per message: one
call classifies fact statements, authorized update commands, and
self-status questions all at once, and the systems that care read the
result from session["intent"] instead of re-deriving it.
"""
from llm.ollama_client import ollama_manager

ALLOWED_FACT_KEYS = ["favorite_color", "alias", "job"]

# Not a coverage-limiting gate (that's what we just removed) — this is a
# deterministic SAFETY CHECK applied to the classifier's own output, for one
# specific, known, consequential failure mode: mistral will sometimes
# classify hypothetical language ("what if my favorite color was blue") as
# a real "fact" intent. Storing a hypothetical as if it were a confirmed
# fact is a real correctness problem, so this one judgment stays fixed
# logic rather than trusting the model, the same reasoning already applied
# in systems/facts/system.py's is_declarative() check.
HYPOTHETICAL_MARKERS = ["what if", "if ", "suppose", "imagine", "pretend", "would be"]


def _is_hypothetical(text: str) -> bool:
    lower = text.strip().lower()
    return "?" in lower or any(m in lower for m in HYPOTHETICAL_MARKERS)


# Fact keys (alias/favorite_color/job) are their own top-level "intent"
# values in the prompt below, NOT nested under a "fact" wrapper — this is
# what Qwen2.5 naturally wants to produce under JSON-constrained decoding
# (confirmed live: asking for {"intent": "fact", "key": "alias", ...}
# made it collapse to {"intent": "none"} on plain cases like "call me
# Craig", while it reliably gets the flat shape right). classify_intent()
# normalizes the flat shape back into the {"intent": "fact", "key": ...}
# contract below so callers (facts/system.py etc.) don't need to change.
INTENT_PROMPT_TEMPLATE = """You are A.L.E.X, an AI assistant. The user said: "{text}"

Classify this message into EXACTLY ONE of these categories:

1. "alias" — the user is stating THEIR OWN name or nickname (e.g. "my name is X", "call me X", "you can call me X", "I go by X"). This is about the USER's name, not A.L.E.X.'s (statements like "your name is X" or "you are X" describe A.L.E.X. and are "none").

2. "favorite_color" — the user is stating their favorite color (e.g. "my favorite color is blue").

3. "job" — the user is stating their job/profession (e.g. "my job is a teacher", "I work as an engineer").

4. "status_check" — the user is asking A.L.E.X. to check, test, run, or report on her OWN operational status or systems, in ANY phrasing, including short/casual ones (e.g. "are you okay", "are you working correctly", "check your systems", "is everything working", "run/perform/do a diagnostic (on yourself)", "system check"). This is NOT about the user's own status or feelings — only hers.

5. "permission_command" — the user is asking to update a stored value AND provides an authorization code together in the same message (e.g. "set my job to teacher with code 1234").

6. "none" — anything else: normal conversation, questions, hypotheticals ("what if", "suppose", "imagine"), requests unrelated to the above.

Respond with ONLY a JSON object, matching the category exactly:
- alias: {{"intent": "alias", "value": "<name>"}}
- favorite_color: {{"intent": "favorite_color", "value": "<color>"}}
- job: {{"intent": "job", "value": "<job>"}}
- status_check: {{"intent": "status_check"}}
- permission_command: {{"intent": "permission_command", "key": "<field name>", "value": "<new value>", "code": "<code>"}}
- none: {{"intent": "none"}}"""


async def classify_intent(text: str) -> dict:
    """
    Returns a dict with at least {"intent": "fact"|"permission_command"|"status_check"|"none"},
    plus extracted fields for "fact"/"permission_command". Falls back to
    {"intent": "none"} on any failure (Ollama unavailable, bad JSON) — this
    is indistinguishable from a genuine negative classification, so callers
    that need robustness against the LLM being briefly down should keep
    their own cheap deterministic fallback, not treat "none" as certain.
    """
    prompt = INTENT_PROMPT_TEMPLATE.format(text=text)

    result = await ollama_manager.generate_json(prompt, timeout=20.0, temperature=0)

    if not result or "intent" not in result:
        return {"intent": "none"}

    raw_intent = result.get("intent")

    # normalize the flat fact-key shape back into the public contract
    if raw_intent in ALLOWED_FACT_KEYS:
        result = {"intent": "fact", "key": raw_intent, "value": result.get("value")}

    if result.get("intent") == "fact" and _is_hypothetical(text):
        return {"intent": "none"}

    return result


PERSONALITY_SET_PROMPT = """You are A.L.E.X, an AI assistant. The user said: "{text}"

Is the user DIRECTLY asking A.L.E.X. to adopt a NEW, DIFFERENT personality or way of talking (not just chatting, not asking her to reset/default, not about anything else)? This includes both describing a new trait ("set your personality to be more sarcastic", "be snarkier", "you should be more upbeat") AND telling her to stop/change a specific habit of hers ("stop saying you're here to assist me", "you don't need to keep telling me that", "stop being so repetitive", "stop offering to help every time").

Respond with ONLY a JSON object:
{{"personality_command": "set", "value": "<the desired personality/behavior change, as a short description>"}}
or {{"personality_command": "no"}}"""


async def classify_personality_set(text: str) -> dict:
    """
    Separate, dedicated, single-purpose classifier — deliberately NOT folded
    into classify_intent() above. Confirmed live: adding a 5th category to
    that shared prompt caused total collapse (even solid cases like "call me
    Craig" started returning "none"), so this stays isolated with its own
    short prompt. Also deliberately narrower in scope than a full
    "personality_command" classifier would be: an earlier version that also
    tried to detect "reset" requests here produced dangerous false positives
    on totally unrelated messages ("reset the router" -> reset her
    personality). "reset" phrasing is a small, enumerable space, so it's
    handled by a deterministic phrase list in systems/controller/system.py
    instead — only the genuinely open-ended "set to something new" case
    needs real judgment, and that's the only thing this function decides.
    Callers MUST check deterministic "set"/"reset" phrases first and only
    fall back to this for everything else.
    """
    prompt = PERSONALITY_SET_PROMPT.format(text=text)
    result = await ollama_manager.generate_json(prompt, timeout=20.0, temperature=0)

    if not result or result.get("personality_command") != "set":
        return {"personality_command": "no"}

    value = str(result.get("value", "")).strip()
    if not value:
        return {"personality_command": "no"}

    return {"personality_command": "set", "value": value}


MODULE_GAP_PROMPT = """You are A.L.E.X, an AI assistant who can build small self-contained tools (modules) for herself when she doesn't already have one. The user said: "{text}"

Is the user asking for a capability/tool/skill that doesn't already exist as part of a normal conversation (e.g. "I need a calculator", "can you build a timer", "play some music", "create a todo list", "make a unit converter") — NOT a normal question you'd just answer, NOT casual conversation, NOT something about A.L.E.X. herself or the user's stored data?

Respond with ONLY a JSON object:
{{"wants_module": true, "name": "<short_snake_case_name>"}}
or {{"wants_module": false}}"""


async def classify_module_gap(text: str) -> dict:
    """
    Separate, dedicated classifier — replaces systems/modules/system.py's
    old detect_module_name(), which only fired on the literal substrings
    "play"/"build"/"create" (the same hardcoded-trigger-phrase problem
    already fixed everywhere else in this project; see Scan Pass 1 in
    SELF_MODIFICATION_ARCHITECTURE.md). This system runs late in the
    routing order (priority 10, after controller/command/intent/
    permissions/facts/memory/diagnostics), so by the time a message
    reaches it, personality/status/fact-style requests have already been
    claimed by an earlier system — this only needs to catch genuine
    capability requests among what's left.
    """
    prompt = MODULE_GAP_PROMPT.format(text=text)
    result = await ollama_manager.generate_json(prompt, timeout=20.0, temperature=0)

    if not result or not result.get("wants_module"):
        return {"wants_module": False}

    name = str(result.get("name", "")).strip().lower()
    name = "".join(c if c.isalnum() else "_" for c in name).strip("_")

    if not name:
        return {"wants_module": False}

    return {"wants_module": True, "name": name}
