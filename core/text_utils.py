# core/text_utils.py

"""
Shared text-cleanup helpers (2026-07-16, moved here from
systems/controller/_text.py once a second, unrelated package needed the
same thing — a helper used across package boundaries belongs somewhere
neutral, not nested inside the first consumer that happened to need it).

A command that extracts a name/argument by stripping a fixed prefix off
the raw utterance ("disable module egg_timer." -> "egg_timer.") ends up
with STT's trailing sentence punctuation baked into the value — a name
with a literal period on the end never matches a real system/module/
table name, so the command fails, silently and confusingly (not "denied"
or "not found" in an obviously wrong way, just "I don't have a module
called 'egg_timer.'"). This hit three different controller commands in
one session (the elevated-access approval regex, then "reload system
diagnostics.") before being centralized instead of patched one call site
at a time — see SELF_MODIFICATION_ARCHITECTURE.md's session history.
"""

import re

_STRIP_CHARS = " .,!?"

# 2026-07-16: found live — Craig told her to stop using emojis several
# times (creator override, merged into personality, added as a hard rule
# in db.system_learning) and the model still produced one anyway.
# Instruction-following for a negative constraint ("never do X") isn't
# perfectly reliable on this size of model even with temperature=0 and
# explicit prompting — same lesson as every other place in this project
# that a real guarantee needs a deterministic check, not trusting the LLM
# to comply. Range covers the common emoji blocks (emoticons, symbols,
# transport/map symbols, supplemental symbols, dingbats, variation
# selectors) — broad enough to catch what she's actually produced without
# stripping ordinary punctuation/text.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002700-\U000027BF"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def strip_trailing_punctuation(text: str) -> str:
    return text.strip(_STRIP_CHARS)


def first_word(text: str) -> str:
    """The first whitespace-separated token, trailing punctuation
    stripped — for matching a short reply ("yes"/"no"/"y"/"n") without
    the false-positive risk of a raw .startswith() check, which matches
    ANY message starting with that letter ("You're just gonna say that
    for everything now".startswith("y") is True). Confirmed live
    (2026-07-16): that exact false positive let an unrelated sentence
    accidentally confirm a 20-minute-stale pending module build."""
    parts = text.strip().split(None, 1)
    if not parts:
        return ""
    return strip_trailing_punctuation(parts[0])
