# systems/modules/system.py

"""
Module System

Handles:
- module detection
- module generation
- module execution
- module state

Replaces ALL module logic previously in ws_chat.py
"""

import re
import asyncio
import textwrap

from core.system_base import BaseSystem

from module_runtime.module_loader import (
    get_module,
    load_module,
    load_all_modules
)
from module_runtime.module_executor import run_module
from module_runtime.module_generator import generate_module_code
from module_runtime.module_installer import install_module

from db.db import get_module_state, set_module_state
from config.logger_config import logger


class System(BaseSystem):

    name = "modules"
    priority = 10  # 🔥 higher priority than LLM

    def __init__(self):
        self.pending_builds = {}
        self.generation_lock = asyncio.Lock()

    async def init(self):
        load_all_modules()
        print("🧩 Module system ready")

    # -------------------------
    # MAIN HANDLER
    # -------------------------
    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        msg = text.lower()

        # -------------------------
        # DETECT MODULE NAME
        # -------------------------
        module_name = self.detect_module_name(msg)

        if not module_name:
            return None

        module = get_module(module_name)

        # -------------------------
        # BUILD IF MISSING
        # -------------------------
        if not module:

            if user_id not in self.pending_builds:
                self.pending_builds[user_id] = module_name

                return {
                    "type": "response",
                    "content": f"I don’t have {module_name}. Want me to build it?"
                }

            # confirmation phase
            if msg.startswith(("yes", "y", "yeah", "confirm")):
                module_name = self.pending_builds.pop(user_id)

                return await self._build_module(module_name, user_id, text)

            if msg.startswith(("no", "n")):
                self.pending_builds.pop(user_id)

                return {
                    "type": "response",
                    "content": "Okay, I won’t build it."
                }

            return None

        # -------------------------
        # RUN MODULE
        # -------------------------
        state = await get_module_state(user_id, module_name)

        result, new_state = run_module(module, text, state)

        if new_state is not None:
            await set_module_state(user_id, module_name, new_state)

        if result:
            return {
                "type": "response",
                "content": result
            }

        return None

    # -------------------------
    # MODULE BUILD
    # -------------------------
    async def _build_module(self, module_name, user_id, prompt):

        async with self.generation_lock:

            code = await generate_module_code(module_name, prompt)

            if not code:
                fallback = textwrap.dedent(f"""
                def init():
                    return "{module_name} ready"

                def handle(command, state):
                    return "Basic {module_name} module created.", state
                """)

                success, _ = await install_module(module_name, fallback, user_id)

                if not success:
                    return {
                        "type": "response",
                        "content": "Module build failed."
                    }

                load_module(module_name)

                logger.info(f"[ACTION] Built module '{module_name}' (fallback template, requested by {user_id})")

                return {
                    "type": "response",
                    "content": f"Created basic {module_name} module."
                }

            success, _ = await install_module(module_name, code, user_id)

            if not success:
                return {
                    "type": "response",
                    "content": "Module build failed."
                }

            load_module(module_name)

            logger.info(f"[ACTION] Built module '{module_name}' (generated, requested by {user_id})")

            return {
                "type": "response",
                "content": f"{module_name} module ready."
            }

    # -------------------------
    # NAME DETECTION
    # -------------------------
    def detect_module_name(self, msg: str):

        if "play" in msg:
            name = msg.split("play")[-1]
        elif "build" in msg:
            name = msg.split("build")[-1]
        elif "create" in msg:
            name = msg.split("create")[-1]
        else:
            return None

        name = name.strip()
        name = re.sub(r"[^\w\s]", "", name)
        name = re.sub(r"\b(a|an|the)\b", "", name)
        name = name.strip()
        name = name.replace(" ", "_")

        return name if name else None