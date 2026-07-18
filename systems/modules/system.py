# systems/modules/system.py

"""
Module System

Handles:
- running already-installed modules by name
- module state

Replaces ALL module logic previously in ws_chat.py

2026-07-16: no longer does gap DETECTION or build PROPOSALS — see
KNOWN_MODULE_TRIGGERS below for why. Actual module building now happens
entirely through Claude authoring code directly in a dev session
(tools/pending_builds.py), not through any live voice/chat flow.
"""

import time

from core.system_base import BaseSystem

from module_runtime.module_loader import (
    load_module,
    load_all_modules
)
from module_runtime.module_executor import run_module

from core.phrasebook import get_phrase
from db.db import (
    get_module_state, set_module_state,
    get_module_registry_entry, list_module_registry
)
from config.logger_config import logger

# 2026-07-16 (Craig: "I'd almost rather it not be there then... 99% of it
# probably isn't going to prompt a build") — removed classify_module_gap(),
# an LLM classifier call this system used to run on EVERY single message
# (~2s each, same model as the main conversation, so not a reload cost —
# just unconditional per-turn latency) specifically to catch IMPLICIT
# build requests ("I shoot guns" -> proposed 'firearm_simulation') that
# essentially never turn into a real build anymore now that building
# actually happens through Claude directly, not through her own live
# conversational gap-detection. That same classifier output was ALSO how
# an already-installed module got invoked by name — but in practice that
# only ever mattered for 'recall': diagnostic_tool and inquiry both
# already have their own dedicated, deterministic trigger systems running
# at a HIGHER priority (9, vs. this system's 10), so a relevant message
# would already have been claimed before ever reaching here. A small
# fixed trigger list for the one remaining real case is both faster and,
# per the standing project-wide lesson about this model's classifiers,
# more reliable than an LLM call for something this narrow and enumerable.
KNOWN_MODULE_TRIGGERS = {
    "recall": ("remember", "recall", "your memories", "memories"),
}


def _detect_known_module(text: str):
    lowered = text.lower()
    for name, triggers in KNOWN_MODULE_TRIGGERS.items():
        if any(t in lowered for t in triggers):
            return name
    return None


_MODULE_QUESTION_STARTS = (
    "tell me about", "what is", "what does", "what's", "whats",
    "how do", "how does", "do you have", "can you tell me", "describe",
)


def _looks_like_module_question(text):
    t = text.strip().lower()
    if t.endswith("?"):
        return True
    return t.startswith(_MODULE_QUESTION_STARTS)


class System(BaseSystem):

    name = "modules"
    priority = 10  # 🔥 higher priority than LLM

    async def init(self):
        await load_all_modules()
        print("🧩 Module system ready")

    async def diagnose(self):
        """Real check: running an already-installed module depends on
        list_module_registry() to know what actually exists."""
        try:
            await list_module_registry()
        except Exception as e:
            return False, f"list_module_registry() raised: {e}"
        return True, ""

    # -------------------------
    # MAIN HANDLER
    # -------------------------
    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        module_name = _detect_known_module(text)
        if not module_name:
            return None

        registry_entry = await get_module_registry_entry(module_name)
        if not registry_entry:
            return None

        # -------------------------
        # RUN MODULE (respect enable/disable from the registry — same
        # "checked at invocation time, not unloaded from memory" pattern
        # the systems/* tier already uses for disabled_systems)
        # -------------------------
        if registry_entry["status"] == "disabled":
            return {
                "type": "response",
                "content": await get_phrase("module_currently_disabled", module_name=module_name)
            }

        module = await load_module(module_name)

        if not module:
            logger.warning(f"[ACTION] Module '{module_name}' exists but failed to load (blocked or broken)")
            return {
                "type": "response",
                "content": await get_phrase("module_blocked_or_broken", module_name=module_name)
            }

        # Meta/conversational questions about a module ("tell me about X",
        # "what does X do?") shouldn't be piped into the module's own
        # handle() as a raw command. Confirmed live: egg_timer's handle()
        # correctly didn't recognize "tell me about the egg timer you
        # made" as a command and returned its own "Unknown command: ..."
        # fallback verbatim — which reads exactly like she'd forgotten
        # building it, seconds after actually building it. Deliberately a
        # small deterministic check, not a classifier: a false positive
        # here just falls through to a description instead of running the
        # module, a low-cost failure, while the real bug (a genuine
        # question misrouted as a command) is the one actually observed.
        if _looks_like_module_question(text):
            module_help = None
            try:
                if hasattr(module, "help"):
                    module_help = module.help()
            except Exception:
                module_help = None

            if module_help:
                return {"type": "response", "content": await get_phrase("module_description", module_name=module_name, module_help=module_help)}

            version_note = f" (v{registry_entry['version']})" if registry_entry else ""
            return {
                "type": "response",
                "content": await get_phrase("module_built_no_description", module_name=module_name, version_note=version_note)
            }

        state = await get_module_state(user_id, module_name)

        result, new_state = await run_module(module, text, state, user_id)

        if new_state is not None:
            await set_module_state(user_id, module_name, new_state)

        if result:
            return {
                "type": "response",
                "content": result
            }

        return None