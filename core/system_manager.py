# core/system_manager.py

"""
System Manager

Responsibilities:
- Load/unload systems dynamically
- Route input to correct system
- Maintain system registry
- Run post-response hooks (memory, logging, etc.)
"""

import importlib
import sys
import asyncio
from typing import Dict, Any


class SystemManager:

    def __init__(self):
        self.systems: Dict[str, Any] = {}
        self.active_order = []

    # -------------------------
    # LOAD SYSTEM
    # -------------------------
    async def load(self, name: str):
        try:
            module_path = f"systems.{name}.system"

            if module_path in sys.modules:
                # import_module() alone would hand back the cached module
                # untouched — reload() re-executes it from disk, which is
                # what actually makes hot-swapping pick up code edits.
                module = importlib.reload(sys.modules[module_path])
            else:
                module = importlib.import_module(module_path)

            if not hasattr(module, "System"):
                raise Exception(f"{name} missing System class")

            system_instance = module.System()

            if hasattr(system_instance, "init"):
                await self._safe_call(system_instance.init)

            self.systems[name] = system_instance

            if name not in self.active_order:
                self.active_order.append(name)

            print(f"✅ Loaded system: {name}")
            return True

        except Exception as e:
            print(f"❌ Failed to load system {name}: {e}")
            return False

    # -------------------------
    # UNLOAD SYSTEM
    # -------------------------
    async def unload(self, name: str):
        system = self.systems.get(name)

        if not system:
            return False

        try:
            if hasattr(system, "shutdown"):
                await self._safe_call(system.shutdown)

            del self.systems[name]

            if name in self.active_order:
                self.active_order.remove(name)

            print(f"🗑️ Unloaded system: {name}")
            return True

        except Exception as e:
            print(f"❌ Failed to unload {name}: {e}")
            return False

    # -------------------------
    # ROUTING CORE
    # -------------------------
    async def route(self, session, user_id: str, input_data: Dict):

        disabled = session.get("disabled_systems") or set()
        active = session.get("active_system")

        # -------------------------
        # ACTIVE SYSTEM FIRST
        # -------------------------
        if active and active in self.systems and active not in disabled:

            system = self.systems[active]

            result = await self._safe_call(
                system.handle,
                session=session,
                user_id=user_id,
                input_data=input_data
            )

            if result:
                return result

        # -------------------------
        # ALL SYSTEMS
        # -------------------------
        for name in self.active_order:

            if name in disabled:
                continue

            system = self.systems.get(name)

            result = await self._safe_call(
                system.handle,
                session=session,
                user_id=user_id,
                input_data=input_data
            )

            if result:
                return result

        # -------------------------
        # FALLBACK
        # -------------------------
        return {
            "type": "response",
            "content": "No system handled the input."
        }

    # -------------------------
    # AFTER RESPONSE HOOK
    # -------------------------
    async def after_response(self, session, user_id: str, input_data: Dict, response_text: str):

        print("🔥 after_response triggered")

        if not session:
            return

        disabled = session.get("disabled_systems") or set()

        for name in self.active_order:

            if name in disabled:
                continue

            system = self.systems.get(name)

            if hasattr(system, "after_response"):
                await self._safe_call(
                    system.after_response,
                    session=session,
                    user_id=user_id,
                    input_data=input_data,
                    response_text=response_text
                )

    # -------------------------
    # SAFE CALL
    # -------------------------
    async def _safe_call(self, func, *args, **kwargs):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)
        except Exception as e:
            print(f"⚠️ System error: {e}")
            return None