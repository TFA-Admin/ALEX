# systems/controller/_role_gates.py

"""
Shared authorization helpers for every controller command category.
Split out of system.py (2026-07-16) so the gate logic has one home
instead of being duplicated or scattered as the command list grew.
"""

from db.db import get_user_role
from core.phrasebook import get_phrase
from core.override_code import is_creator_override_code


async def require_creator(user_id: str, session: dict, text: str = None):
    """Returns None if authorized, or a rejection response dict if not.

    2026-07-17 (Craig: "I'd like to be able to use my override code at
    any point more or less and have her pull my creator account, thus
    giving access to whatever special commands I would have"): if `text`
    is given and contains the real creator's override code, that's
    treated as sufficient proof on its own — independent of whatever
    this session's voice verification/role already resolved to, so
    stating it works even under an unverified or different identity.
    `text` defaults to None so any call site that doesn't have the raw
    message handy still behaves exactly as before."""
    if text and await is_creator_override_code(text):
        return None

    role = await get_user_role(user_id)

    if role != "creator":
        return {"type": "response", "content": await get_phrase("denial_not_creator")}

    if not session.get("creator_verified"):
        return {"type": "response", "content": await get_phrase("denial_not_verified")}

    return None


async def require_privileged(user_id: str, session: dict, text: str = None):
    """Creator OR super_user, both requiring live voice verification this
    session. Used for lower-stakes admin actions (system enable/disable/
    list) — creator-identity actions (personality, reload, granting
    roles) stay behind require_creator above. Same override-code
    shortcut as require_creator — the creator's own code proves creator
    identity, which is a superset of privileged."""
    if text and await is_creator_override_code(text):
        return None

    role = await get_user_role(user_id)

    if role not in ("creator", "super_user"):
        return {"type": "response", "content": await get_phrase("denial_not_privileged")}

    if not session.get("creator_verified"):
        return {"type": "response", "content": await get_phrase("denial_not_verified")}

    return None
