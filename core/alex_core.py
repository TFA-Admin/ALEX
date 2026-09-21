# core/alex_core.py

"""
ALEX CORE (Microkernel)

This is the ONLY orchestrator in the system.

Responsibilities:
- Route input → systems
- Manage system lifecycle
- Handle hot-swapping
- Maintain session context
"""

import time
from typing import Dict, Any

from core.system_manager import SystemManager


class AlexCore:

    def __init__(self):
        self.systems = SystemManager()
        self.sessions: Dict[str, Dict[str, Any]] = {}

    # -------------------------
    # SYSTEM INIT
    # -------------------------
    async def init_systems(self):
        await self.load_system("controller")
        await self.load_system("command")
        await self.load_system("intent")
        await self.load_system("permissions")
        await self.load_system("facts")
        await self.load_system("memory")
        await self.load_system("diagnostics")
        await self.load_system("inquiry")
        await self.load_system("modules")
        await self.load_system("awareness")
        await self.load_system("llm")

    # -------------------------
    # SESSION MANAGEMENT
    # -------------------------
    def get_session(self, session_id: str):
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "active_system": None,
                "context": {}
            }
        return self.sessions[session_id]

    def end_session(self, session_id: str):
        """Drops a session when its WebSocket closes.

        2026-09-20 (Craig): "connections don't seem to be closing out
        entirely." Correct. `_active_connections` in ws/ws_handlers.py was
        cleaned up in a finally block, but nothing ever removed the session
        dict behind it — `get_session` creates on demand and there was no
        matching delete anywhere in the codebase. Every browser reload,
        every reconnect and every harness case left a permanent entry
        holding that turn's fact context, memory context, intent and any
        pending offers. One test run today opened about a hundred.

        Not a correctness bug, because session_id is a fresh uuid per
        connection so a stale entry is never read again — a pure memory
        leak that grows for as long as the process lives.

        **Race, accepted deliberately**: a response task spawned by the
        last message can still be streaming when this runs. If it calls
        get_session() afterwards it gets a fresh empty dict, so
        after_response() finds no `_llm_match` and does nothing. That is
        the right outcome — the connection is gone, so a queued
        "want me to keep that?" offer has nobody to ask."""
        self.sessions.pop(session_id, None)

    # -------------------------
    # MAIN ENTRY POINT
    # -------------------------
    async def handle_input(self, session_id: str, user_id: str, input_data: Dict):

        session = self.get_session(session_id)

        # Stamped here — the one true single entry point every message
        # passes through — so response_handler.py's [TIMING] TOTAL line
        # can report real heard-to-spoken time, not just the time spent
        # after dispatch already picked a system to answer with.
        session["turn_start_time"] = time.time()

        # 🔥 ROUTE THROUGH SYSTEM MANAGER
        response = await self.systems.route(
            session=session,
            user_id=user_id,
            input_data=input_data
        )

        return response

    # -------------------------
    # HOT SWAP SYSTEM
    # -------------------------
    async def load_system(self, name: str):
        return await self.systems.load(name)

    async def unload_system(self, name: str):
        return await self.systems.unload(name)

    async def reload_system(self, name: str):
        await self.unload_system(name)
        return await self.load_system(name)


# -------------------------
# SINGLETON
# -------------------------
alex_core = AlexCore()