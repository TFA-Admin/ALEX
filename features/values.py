# features/values.py
"""
What he values (core/values.py): arithmetic on his thanks and his
corrections and the SHAPE of the reply each followed — never on agreement.
This feature keeps the shape of her last reply on the session, turns his
thanks and his corrections into signals, and renders the lopsided tallies
into her prompt. Off: nothing recorded, nothing rendered.
"""
from features.base import Feature as Base


class Feature(Base):
    name = "values"
    summary = ("What he values (core/values.py): arithmetic on his thanks and corrections and the shape "
               "of the reply each followed; never on agreement.")
    owns = ("core.values",)
    order = 30

    async def prompt_block(self, user_id: str = "", **kw) -> str:
        from core import values
        return values.render(await values.lines_for(user_id))

    async def on(self, event: str, **kw):
        from core import values
        session = kw.get("session")
        if session is None:
            return
        if event == "reply":
            if kw.get("interrupted") or not kw.get("text"):
                return
            session["last_reply"] = values.reply_shape(kw.get("text", ""), bool(kw.get("looked")))
        elif event == "thanked":
            await values.note_signal("thanks", kw.get("user_id", ""), session.get("last_reply"))
        elif event == "corrected" and kw.get("creator"):
            await values.note_signal("correction", kw.get("user_id", ""), session.get("last_reply"))

    async def diagnose(self):
        from core.self_reflection import get_creator_name
        from db.db import fetch_value_signals
        from core import values
        who = (await get_creator_name() or "craig").lower()
        signals = await fetch_value_signals(who, days=values.WINDOW_DAYS)
        lines = values.conclude(signals)
        return True, f"{len(signals)} signal(s) in {values.WINDOW_DAYS} days; {len(lines)} preference(s) supported"

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Values — " + msg
