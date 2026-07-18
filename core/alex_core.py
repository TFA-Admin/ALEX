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

import asyncio
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