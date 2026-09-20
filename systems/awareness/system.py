"""
Awareness System

Notices when part of her has been switched off and gives her the chance to ask
about it, in her own words, as part of whatever she was already saying.

2026-09-20 (Craig): "mid conversation even if you were to switch something off,
for her to notice and ask me why this is now off. I would then explain, and
later tell her to re-enable it and she would. I think this would also further
serve as a means to generate interaction which I also feel she needs."

This system never answers anything itself — it returns None always, and only
sets session state that systems/llm/system.py turns into context. That
separation is the point: the FACT that something is off is deterministic and
checkable, while the words she uses are hers. An earlier attempt at this put a
hardcoded English sentence in the diagnostic module, and Craig caught it
immediately: "This is an advisory not something hard coded into her though
correct?"

Cost is a registry read and a file stat per turn, no LLM call.
"""
from core.system_base import BaseSystem
from core import disabled_watch
from db.db import get_user_role
from config.logger_config import logger


class System(BaseSystem):

    name = "awareness"
    # After modules (10), before the LLM fallback (100). If a module answers
    # this turn the notice simply stays pending — it is popped when a
    # generated reply is actually built, so nothing is lost, it just waits for
    # a turn she speaks in her own voice.
    priority = 11

    async def init(self):
        print("👁️ Awareness system ready")

    async def diagnose(self):
        """Real check: the watch has to be able to read what is currently
        disabled. If that read fails she would silently stop noticing, which
        is exactly the kind of quiet failure this system exists to catch in
        other places."""
        try:
            await disabled_watch.current_disabled()
        except Exception as e:
            return False, f"disabled_watch.current_disabled() raised: {e}"
        return True, ""

    async def handle(self, session, user_id: str, input_data: dict):
        text = input_data.get("text")
        if not text:
            return None

        # Only the creator is asked about this. Another user has no idea why
        # something was switched off and cannot re-enable it, so raising it
        # with them would be noise they cannot act on.
        #
        # Uses the real per-user role rather than a session flag: there is no
        # session["role"] (checked — the session carries `creator_verified`,
        # which is about voice proof, not identity). Asking does not need
        # voice verification; it is a question, not a privileged action.
        try:
            if await get_user_role(user_id) != "creator":
                return None
        except Exception:
            return None

        try:
            pending = await disabled_watch.unexplained()
        except Exception as e:
            logger.warning(f"⚠️ disabled-feature check failed: {e}")
            return None

        # If she asked last turn, THIS turn is almost certainly the answer.
        # 2026-09-20: without this the loop was half-built — she raised the
        # question and nothing captured the reply, so she would ask again next
        # time. Craig answered "I turned it off for testing" and it went
        # nowhere. Deliberately a positional heuristic rather than a
        # classifier: the turn straight after her question is the answer often
        # enough, and being wrong only means she stops asking about something
        # she already asked about.
        awaiting = session.pop("awaiting_disabled_reason", None)
        if awaiting:
            for feature in awaiting:
                await disabled_watch.record_reason(feature, text)
            logger.info(f"[AWARENESS] recorded reason for {list(awaiting)}: {text!r}")

        if pending:
            logger.info(f"[AWARENESS] noticed switched off, no reason given: {list(pending)}")
            session["pending_disabled_notice"] = pending
            session["awaiting_disabled_reason"] = list(pending)

        return None      # never answers; only supplies context
