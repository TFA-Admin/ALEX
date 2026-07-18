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
import os
import sys
import asyncio
from typing import Dict, Any


class SystemManager:

    def __init__(self):
        self.systems: Dict[str, Any] = {}
        self.active_order = []
        # name -> latest mtime across that system's package, as of its
        # last successful load. Lets route() auto-reload a system whose
        # files changed on disk without needing an explicit "reload
        # system X" or a full restart — see _maybe_hot_reload().
        self._mtimes: Dict[str, float] = {}

    # -------------------------
    # PACKAGE MTIME
    # -------------------------
    def _package_mtime(self, name: str) -> float:
        """Latest mtime across every .py file under systems/{name}/ —
        not just system.py, since controller/diagnostics/etc. are split
        across multiple files (_module_admin.py, _text.py, ...) and a
        change to any of them should count."""
        root = os.path.join("systems", name)
        latest = 0.0

        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if fname.endswith(".py"):
                    try:
                        latest = max(latest, os.path.getmtime(os.path.join(dirpath, fname)))
                    except OSError:
                        pass

        return latest

    # -------------------------
    # LOAD SYSTEM
    # -------------------------
    async def load(self, name: str):
        try:
            module_path = f"systems.{name}.system"
            prefix = f"systems.{name}."

            # Reload every already-imported submodule under this
            # system's package FIRST. importlib.reload() only
            # re-executes the exact module object it's given — reloading
            # just system.py would leave stale code in any _submodule.py
            # it imports via `from systems.controller import
            # _module_admin`, since that's a cached-name lookup, not a
            # fresh re-exec of _module_admin itself.
            for mod_name in list(sys.modules):
                if mod_name.startswith(prefix) and mod_name != module_path and sys.modules.get(mod_name):
                    importlib.reload(sys.modules[mod_name])

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
            self._mtimes[name] = self._package_mtime(name)

            if name not in self.active_order:
                self.active_order.append(name)

            print(f"✅ Loaded system: {name}")
            return True

        except Exception as e:
            print(f"❌ Failed to load system {name}: {e}")
            return False

    # -------------------------
    # HOT RELOAD CHECK
    # -------------------------
    async def _maybe_hot_reload(self, name: str):
        """Transparent hot-reload: if any .py file under this system's
        package changed on disk since it was last loaded, reload it
        before dispatching — the same "edit it and it just works"
        property modules already have (module_loader.py reloads fresh
        every call), but only paying the reload cost when something
        actually changed rather than on every message. A failed reload
        (e.g. a syntax error mid-edit) leaves the previously-working
        instance in place — see load()'s exception handling. Any
        in-memory instance state (e.g. a system's own pending-
        confirmation dict) resets when an actual reload happens, same as
        the existing "reload system X" command already did."""
        current = self._package_mtime(name)

        if current > self._mtimes.get(name, 0):
            print(f"🔄 Auto-reloading system '{name}' (source changed on disk)")
            await self.load(name)

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

            await self._maybe_hot_reload(active)
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

            await self._maybe_hot_reload(name)
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
        except Exception:
            # Was print()-only — invisible in the log file, so a system
            # silently failing (falling through to the next-priority
            # system, eventually the LLM) left zero trace of why. Full
            # traceback now goes to the real logger so this is actually
            # diagnosable instead of guessed at.
            from config.logger_config import logger
            logger.exception(f"[ERROR] System error in {getattr(func, '__qualname__', func)}")
            return None