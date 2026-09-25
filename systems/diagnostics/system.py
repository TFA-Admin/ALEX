# systems/diagnostics/system.py
"""
Diagnostics System

Answers "are you okay / check your systems / is everything working"
questions with real, just-gathered facts about her own operational state.

This responds DIRECTLY with a deterministically-built message rather than
staging context for the general LLM system to phrase — tried that first,
and even with an explicit "don't add anything beyond these facts" rule,
the model repeatedly added invented troubleshooting advice ("check your
GPU drivers", "consult a technician") that was never in the gathered
data. For a feature whose entire point is trustworthy self-reporting,
that's not an acceptable failure mode, so this trades the LLM's
personality-flavored phrasing for a guarantee: what she reports is
exactly what was measured, nothing more.

This system's priority (9) still runs before the module system (10), so
it's the one that actually answers a "status check" utterance. As of
2026-07-16 this delegates entirely to the privileged `diagnostic_tool`
module (registry-tracked, versioned, Claude-authored — see
SELF_MODIFICATION_ARCHITECTURE.md Component 1) instead of keeping a
second, hardcoded copy of the same checks here — a duplicate is exactly
what a migration is supposed to eliminate, not preserve as a fallback.
`run_module()` already returns an error string rather than raising or
going silent, so this stays honest (a real "module error" report) even
if the module itself breaks, without needing a shadow implementation.
"""
from core.system_base import BaseSystem
from config.logger_config import logger
from core.phrasebook import get_phrase
from db.db import get_module_registry_entry
from module_runtime.module_loader import load_module
from module_runtime.module_executor import run_module

# Deterministic, not a classifier call — a casual presence/hearing check
# needs a different ANSWER than a real diagnostic request, not a
# different classification. status_check stays broad on purpose (it's
# what stops "are you okay" from reaching the LLM and getting
# hallucinated advice) — this only changes what she says once she's
# already been routed here.
#
# Keyword-based, not an enumerated phrase list — found live (2026-07-16)
# that the original exact-phrase list ("can you hear me", "do you hear
# me", ...) missed real rephrasings entirely: "You can't hear me?" and
# "...I asked if you can hear me" both contain the word order flipped
# from every listed phrase, so neither matched, and both silently fell
# through to the full diagnostic dump instead of a simple "yes, I can
# hear you" — which is exactly what produced "you're just gonna say that
# for everything now." Same lesson as the elevated-access approval
# command earlier tonight: match the actual signal (the word "hear"),
# not literal phrasings, since STT/rephrasing variance is unpredictable
# in advance.
CASUAL_PRESENCE_KEYWORDS = ("hear", "listening")
CASUAL_PRESENCE_PHRASES = ("are you there",)


def _is_presence_check(lower: str) -> bool:
    return (
        any(k in lower for k in CASUAL_PRESENCE_KEYWORDS)
        or any(p in lower for p in CASUAL_PRESENCE_PHRASES)
    )


class System(BaseSystem):

    name = "diagnostics"
    priority = 9  # after memory (8), before modules (10) / llm (100)

    async def init(self):
        print("🩺 Diagnostics system ready")

    async def diagnose(self):
        """Deliberately does NOT call load_module/run_module on
        diagnostic_tool — that module's own handle() loops over every
        system's diagnose() (this one included), so actually invoking
        it here would recurse. A read-only registry check is the
        correct depth: confirms the thing this system delegates to is
        actually present and enabled, without running it."""
        entry = await get_module_registry_entry("diagnostic_tool")

        if not entry:
            return False, "diagnostic_tool module is not registered"

        if entry["status"] != "enabled":
            return False, f"diagnostic_tool module status is {entry['status']!r}, expected 'enabled'"

        return True, ""

    async def handle(self, session, user_id: str, input_data: dict):

        text = input_data.get("text", "")
        if not text:
            return None

        # reads the shared classification already done once per message by
        # systems/intent/system.py — no separate LLM call here
        intent = session.get("intent") or {}

        if intent.get("intent") != "status_check":
            return None

        lower = text.strip().lower()

        if _is_presence_check(lower):
            # Grounded, not a guess: the message was received, transcribed,
            # and reached this handler at all, which IS the confirmation —
            # no need for the full system-by-system dump for a question
            # this narrow.
            return {
                "type": "response",
                "content": await get_phrase("presence_confirmed")
            }

        # 2026-09-25 (Craig: "if I'm asking for a systems check or a
        # diagnostic I mean everything. Unless I say 'check this module'
        # or something specific... I'm also using this as a means to tell
        # what she can and can't see on herself"): a named part is checked
        # on its own; anything else is the full sweep (core/sweep.py).
        # The full report goes to his screen as it is, unspoken; what she
        # SAYS is the summary, in her own words — the 2026-09-21 rule
        # (report it, add nothing) still applies to that, in
        # systems/llm/system.py, which she falls through to.
        from core import sweep
        try:
            from db.db import list_module_registry
            module_names = [e["name"] for e in await list_module_registry()]
        except Exception:
            module_names = []
        try:
            from modules.diagnostic_tool.module import _discover_system_names
            system_names = _discover_system_names()
        except Exception:
            system_names = []
        part = sweep.named_part(lower, module_names, system_names)
        try:
            if part:
                shown, spoken = await sweep.check_part(user_id, part)
                session["diagnostic_context"] = shown
            else:
                shown, spoken = sweep.render(await sweep.full_sweep(user_id))
                await self._show_report(user_id, shown)
                session["diagnostic_context"] = (spoken + " The full report is already on his screen; "
                                                 "say the summary, not the list.")
        except Exception as e:
            session["diagnostic_context"] = f"The sweep itself failed: {e}"
        return None

    async def _show_report(self, user_id: str, text: str):
        """The whole report to his page, unspoken, before she speaks."""
        try:
            from ws.ws_handlers import _active_connections
            from core.voice import say
            for conn in list(_active_connections.values()):
                if conn.get("user_id") == user_id:
                    # 2026-09-25: remember=False. Twice today the report went
                    # into memory as her own utterance and came back as her
                    # context: "Craig remains offline", "my systems are 95%
                    # online", "the Controller mediates our exchange" — her
                    # reading of the module list and the cannot-see list.
                    await say(conn["websocket"], text, user_id=user_id, speak=False, remember=False)
        except Exception as e:
            logger.warning(f"[SWEEP] could not show the report: {e}")

    async def _gather(self, user_id: str) -> str:
        registry_entry = await get_module_registry_entry("diagnostic_tool")

        if not registry_entry:
            return "diagnostic_tool module isn't installed."

        if registry_entry["status"] == "disabled":
            return "diagnostic_tool module is currently disabled."

        module = await load_module("diagnostic_tool")
        result, _ = await run_module(module, "run a diagnostic check", {}, user_id)

        return result or "diagnostic_tool ran but returned nothing."
