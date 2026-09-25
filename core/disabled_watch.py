"""
Noticing when part of her has been switched off.

2026-09-20 (Craig): "I'm thinking more, mid conversation even if you were to
switch something off, for her to notice and ask me why this is now off. I would
then explain, and later tell her to re-enable it and she would. I think this
would also further serve as a means to generate interaction which I also feel
she needs."

The first version of this idea was mine and it was worse: a timer that nagged
once something had been off for three days. Craig's is event-driven — the
change itself is the trigger — which makes it a conversation rather than a
chore, and gives her something real to be curious about.

Three states per feature, and the distinction matters:

  unknown   — off, and she has not asked yet. She should ask.
  explained — off, and Craig told her why. She should not ask again.
  on        — nothing to say.

Storage is `system_learning`, deliberately not a new table: this is a handful
of keys, and the reasons are conversational text rather than structured data.

WHAT THIS IS NOT
    It does not decide when to speak, and it does not write what she says. It
    answers "has something changed, and does she have a reason for it yet".
    Whether that becomes a question this turn is the calling system's business;
    how it is worded is hers (Design Principle 6 — the intent is fixed, the
    words are not). An earlier version of this notice was a hardcoded English
    sentence, which Craig caught immediately: "This is an advisory not
    something hard coded into her though correct?"

DEFERRED, ON PURPOSE
    Severity. Craig: "that should be severity based in my mind, but would
    require she also have a sense of urgency." Right, and she has no such
    sense yet, so everything here waits for a natural gap rather than
    interrupting. Building urgency levels before she can tell urgent from
    trivial would just be a number nobody could set honestly.
"""
import json

from db.db import (
    get_system_value, set_system_value,
    persona_disabled, list_module_registry,
)

_REASONS_KEY = "disabled_reasons"     # feature -> Craig's explanation
_SEEN_KEY = "disabled_last_seen"      # features she already knows are off

# Feature keys are stable identifiers; the labels are only ever context for
# her own phrasing, never something she is made to recite verbatim.
PERSONA = "personality"


async def current_disabled() -> dict:
    """Everything currently switched off, as {key: human-readable label}."""
    off = {}

    if persona_disabled():
        off[PERSONA] = "your personality (switched off at the Controller)"

    try:
        for entry in await list_module_registry():
            if entry.get("status") != "enabled":
                off[f"module:{entry['name']}"] = f"your '{entry['name']}' module"
    except Exception:
        pass      # a registry read failing must never break a conversation

    # 2026-09-25: her built-in modules (features/) switched off by him —
    # something she should notice and may ask about, like the rest.
    try:
        from db.db import get_features_wanted
        for name, entry in (await get_features_wanted()).items():
            if not (entry or {}).get("enabled", True):
                off[f"feature:{name}"] = f"your built-in '{name}' module"
    except Exception:
        pass

    return off


async def _load(key: str) -> dict:
    raw = await get_system_value(key, "{}")
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def _save(key: str, value: dict):
    await set_system_value(key, json.dumps(value))


async def unexplained() -> dict:
    """What is off that she has NOT been given a reason for — i.e. what is
    worth asking about. Also records that she has now seen these, so a feature
    is only ever raised once per time it is switched off."""
    off = await current_disabled()
    reasons = await _load(_REASONS_KEY)
    seen = await _load(_SEEN_KEY)

    # Anything switched back on clears both its reason and its seen-marker, so
    # if it is ever disabled again that is a NEW event she should ask about
    # rather than something she thinks she already understands.
    for key in list(reasons):
        if key not in off:
            reasons.pop(key, None)
    for key in list(seen):
        if key not in off:
            seen.pop(key, None)

    # Ask ONCE. Not "until answered" — if Craig ignores the question, asking
    # again next turn is nagging, which is the failure mode that makes an
    # otherwise good feature something you want switched off. `seen` is the
    # already-asked marker, and it is cleared above when a feature comes back
    # on, so the same feature disabled again later is a fresh question.
    pending = {k: label for k, label in off.items()
               if k not in reasons and k not in seen}

    await _save(_REASONS_KEY, reasons)
    await _save(_SEEN_KEY, {k: True for k in off})

    return pending


async def record_reason(feature: str, reason: str):
    """Craig explained why. She stops asking about this one."""
    reasons = await _load(_REASONS_KEY)
    reasons[feature] = reason
    await _save(_REASONS_KEY, reasons)


async def known_reasons() -> dict:
    return await _load(_REASONS_KEY)
