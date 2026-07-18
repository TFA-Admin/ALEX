# core/mood.py
"""
Deterministic mood tagging for the avatar orb's color (2026-07-17, Craig:
"I would like to implement some kind of mood system for her to make the
UI real").

This is a UI signal, not a safety/accuracy-critical decision, so a cheap
keyword-based heuristic is the right tool here — same reasoning as every
other cheap deterministic check in this project (FACTUAL_MARKERS,
ACKNOWLEDGMENT_PHRASES, etc.), and deliberately NOT a new per-turn LLM
call: that would reintroduce exactly the kind of unconditional per-turn
latency tax the module-gap classifier removal just got rid of. Computed
once per turn in core/response_handler.py, after the real response is
already on its way to the user — never blocks or slows down the actual
reply.

Four moods, matching the UI concept mockup's palette:
- "alert"   — something is actually wrong (diagnostic problem, error)
- "edge"    — THIS response is notably blunt/dismissive/sarcastic
- "focused" — a longer, substantive, or inquisitive response
- "calm"    — the default resting state

2026-07-17: found live — Craig noticed she "only exists in edge," and
asked the right question: "if that's her norm, shouldn't that be calm?"
Yes. The original version checked whether her PERSISTENT personality
description contained blunt/sarcastic wording — but her personality is
currently tuned that way as a baseline trait, not an occasional
exception, so that check matched on essentially every single turn
regardless of what the response actually said. Personality is a
slow-changing baseline; mood is supposed to be a per-response, reactive
signal — conflating the two collapsed it into one static color. Fixed
by deriving mood from THIS response's own content instead of her static
personality at all: calm is the true default now, and edge is reserved
for a response that's actually notably sharp in its own right (the
example that prompted this fix: "...But really, wasn't it obvious? Just
override me if you want to hear less of it." — genuinely dismissive
content, not just "her personality happens to be blunt").

Starting heuristic, not tuned — genuinely can't be validated without
watching it against real conversation.
"""

MOODS = ("calm", "focused", "edge", "alert")

_ALERT_MARKERS = (
    "diagnostic found a problem", "unreachable", "failed to",
    "wasn't able to", "something went wrong",
)

_EDGE_CONTENT_MARKERS = (
    "wasn't it obvious", "isn't it obvious", "obviously", "duh",
    "whatever", "if you want", "clearly you", "just saying",
    "your call", "not my problem", "your problem",
)

_FOCUSED_LENGTH_THRESHOLD = 220


def derive_mood(response_text: str) -> str:
    """Returns one of MOODS. Order matters — alert (a real problem) takes
    priority over anything else, then whether THIS response reads as
    notably blunt/dismissive, then response shape, defaulting to calm."""
    if not response_text:
        return "calm"

    lowered_resp = response_text.lower()

    if any(m in lowered_resp for m in _ALERT_MARKERS):
        return "alert"

    if any(m in lowered_resp for m in _EDGE_CONTENT_MARKERS):
        return "edge"

    if len(response_text) > _FOCUSED_LENGTH_THRESHOLD or "?" in response_text:
        return "focused"

    return "calm"
