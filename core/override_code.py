# core/override_code.py

"""
Shared override-code gating (2026-07-16, moved here from
systems/controller/_personality.py once a second, unrelated system —
systems/modules/system.py's build confirmation — needed the exact same
check). Anything the creator wants locked behind "you must know something,
not just BE something" (voice+role alone isn't enough) uses this.
"""

import re

from db.db import fetch_user_facts, get_creator_identity

_CODE_NORMALIZE_RE = re.compile(r"[^a-z0-9]")


def _normalize_code(s: str) -> str:
    return _CODE_NORMALIZE_RE.sub("", s.lower())


def strip_override_code_mention(text: str, msg: str) -> str:
    """Best-effort only — NOT the security check (that's
    override_code_status below, which always checks the full original
    message). This just gives a downstream classifier a cleaner input:
    confirmed live (2026-07-16) that classify_personality_set() itself
    returned "no" for "override code alphabravocharlie123 stop using
    emojis" — the "override code X" prefix confused an otherwise
    well-tested prompt, causing a real instruction said together with its
    code in one breath to go undetected entirely. Codes are a single
    contiguous token in practice, so removing "override code" plus the one
    word right after it is enough to recover the real instruction for
    classification purposes, even though it's too crude to trust for the
    actual code comparison."""
    idx = msg.find("override code")
    if idx == -1:
        return text

    before = text[:idx]
    after = text[idx + len("override code"):].strip()
    parts = after.split(None, 1)
    remainder = parts[1] if len(parts) == 2 else ""
    return (before + remainder).strip()


async def is_creator_override_code(text: str) -> bool:
    """2026-07-17 (Craig: "I'd like to be able to use my override code at
    any point... and have her pull my creator account"): true if `text`
    contains the ACTUAL creator's override code, regardless of whose
    session/identity is asking — the code alone is now sufficient proof
    of creator identity for require_creator()/require_privileged()
    (see systems/controller/_role_gates.py), independent of whatever
    voice/role this session already resolved to. Same substring-
    containment convention as override_code_status below, just checked
    against THE creator's code specifically rather than a given user_id's
    (this is meant to work even when the current session ISN'T yet
    resolved as the creator at all)."""
    _, creator_code = await get_creator_identity()
    if not creator_code:
        return False
    return _normalize_code(creator_code) in _normalize_code(text)


async def override_code_status(user_id: str, msg: str) -> str:
    """Returns "valid", "invalid", or "absent". Checked via substring
    containment on the fully punctuation/space-normalized message against
    the creator's real stored override code (same normalization
    convention systems/command/system.py's unlock-profile flow already
    uses: strip everything but letters/digits, lowercase) — NOT by trying
    to positionally parse "the code" out of free speech.

    Found live (2026-07-16) that a strict "override code <next word>"
    prefix parse broke on completely ordinary phrasing variance: a comma
    right after "code" ("override code, alpha bravo charlie 123" — the
    prefix match itself failed, since the literal text has a comma where
    the check expected a space), a connective word ("override code TO
    alpha bravo charlie 123" grabbed just "to" as the whole code), and STT
    mishearing "override" itself ("overtred code"). Substring containment
    sidesteps all of that — it doesn't matter where exactly the code sits
    or what surrounds it, only whether it's present anywhere at all."""
    facts = await fetch_user_facts(user_id)
    real_code = _normalize_code(str(facts.get("override_code", "")))

    if real_code and real_code in _normalize_code(msg):
        return "valid"
    if "override code" in msg:
        return "invalid"
    return "absent"
