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

from core.intent_classifier import classify_module_gap
from db.db import get_module_state, set_module_state, get_user_role, create_module_build_request
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
        # CONFIRMATION PHASE — checked FIRST, independent of the gap
        # classifier below. A real bug (pre-existing, not introduced by the
        # classifier swap): the old keyword gate (detect_module_name) ran
        # on EVERY message including "yes"/"no" replies, and "yes" alone
        # never contained "play"/"build"/"create" — so it returned None
        # before ever reaching this confirmation logic, meaning a build
        # could be proposed but never actually confirmed. Checking pending
        # state first fixes that regardless of what the reply text itself
        # looks like.
        # -------------------------
        if user_id in self.pending_builds:

            if msg.startswith(("yes", "y", "yeah", "confirm")):
                module_name = self.pending_builds.pop(user_id)

                # Creator confirming their own request IS the approval —
                # no extra step. Anyone else's "yes" only queues it; she
                # never builds anything a non-creator asked for without
                # the creator approving it first (via the Controller).
                is_creator = (
                    await get_user_role(user_id) == "creator"
                    and session.get("creator_verified")
                )

                if is_creator:
                    logger.info(f"[ACTION] Build confirmed by creator {user_id}: '{module_name}'")
                    return await self._build_module(module_name, user_id, text)

                request_id = await create_module_build_request(user_id, module_name, text)
                logger.info(
                    f"[ACTION] Build requested by {user_id}: '{module_name}' "
                    f"(request #{request_id}, awaiting creator approval)"
                )

                return {
                    "type": "response",
                    "content": f"I've sent the {module_name} request to my creator for approval — I won't build it until they say yes."
                }

            if msg.startswith(("no", "n")):
                module_name = self.pending_builds.pop(user_id)

                logger.info(f"[ACTION] Build declined by {user_id}: '{module_name}'")

                return {
                    "type": "response",
                    "content": "Okay, I won’t build it."
                }

            # Neither yes nor no — leave the pending build in place and let
            # this message fall through to whatever else might handle it
            # (e.g. a genuine change of subject), same as before.
            return None

        # -------------------------
        # DETECT MODULE NAME
        # -------------------------
        gap = await classify_module_gap(text)
        logger.info(f"[ACTION] Module gap check for {user_id}: {gap} (from: {text!r})")

        if not gap.get("wants_module"):
            return None

        module_name = gap.get("name")

        if not module_name:
            return None

        module = get_module(module_name)

        # -------------------------
        # BUILD IF MISSING
        # -------------------------
        if not module:
            self.pending_builds[user_id] = module_name

            logger.info(f"[ACTION] Build proposed to {user_id}: '{module_name}' (awaiting confirmation)")

            return {
                "type": "response",
                "content": f"I don’t have {module_name}. Want me to build it?"
            }

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