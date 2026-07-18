# systems/controller/system.py

"""
Controller System

Responsibilities:
- Enforce the 3-role model (creator / super_user / user), each gated by
  role + live voice verification for privileged actions
- Route every creator/super_user admin command

This file is deliberately thin — it's the single entry point
core/system_manager.py's dynamic loader requires ("systems.controller.
system" exposing a System class), but the actual command handling lives
in focused sibling files, split out 2026-07-16 once this file had grown
to 500+ lines covering seven unrelated concerns:
  _role_gates.py    — shared require_creator()/require_privileged()
  _system_toggle.py — system enable/disable/list, reload
  _module_admin.py  — module enable/disable/list, elevated-access approval
  _database.py      — the gated database capability
  _personality.py   — personality set/reset/query, phrase reset, role grants
"""

from core.system_base import BaseSystem
from db.db import get_user_role

from systems.controller._role_gates import require_creator, require_privileged
from systems.controller import _system_toggle, _module_admin, _database, _personality

# Checked in sequence — safe regardless of order since every category's
# trigger phrases are mutually exclusive (no two categories can match the
# same message), but grouped roughly by how often each comes up.
_HANDLERS = (
    _system_toggle.handle,
    _module_admin.handle,
    _database.handle,
    _personality.handle,
)


class System(BaseSystem):

    name = "controller"
    priority = 0  # 🔥 HIGHEST PRIORITY

    # -------------------------
    # ROLE GATES — kept as methods (not just module functions) so
    # anything relying on self._require_creator()/self._require_privileged()
    # keeps working unchanged; both just delegate to _role_gates.py.
    # -------------------------
    async def _require_creator(self, user_id: str, session: dict, text: str = None):
        return await require_creator(user_id, session, text)

    async def _require_privileged(self, user_id: str, session: dict, text: str = None):
        return await require_privileged(user_id, session, text)

    # -------------------------
    # INIT
    # -------------------------
    async def init(self):
        print("🎛️ Controller system ready")

    # -------------------------
    # SELF-CHECK — real, not a presence check: every role-gate in this
    # system depends on get_user_role() actually resolving correctly, so
    # this confirms the creator account specifically still resolves to
    # "creator" rather than just "the DB call didn't raise."
    # -------------------------
    async def diagnose(self):
        try:
            role = await get_user_role("craig")
        except Exception as e:
            return False, f"get_user_role() raised: {e}"

        if role != "creator":
            return False, f"creator account resolved to role {role!r}, expected 'creator'"

        return True, ""

    # -------------------------
    # MAIN HANDLER
    # -------------------------
    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        msg = text.lower().strip()

        for handler in _HANDLERS:
            result = await handler(session, user_id, text, msg)
            if result is not None:
                return result

        return None
