# features/mood.py
"""
How she is (core/mood.py): irritation, engagement and strain, moved by
what happens to her, fading on their own half-lives, shown on the orb and
added to her dials. This feature is the wiring: the orb told once a minute
and after every reply, and at connect so it does not open calm when she
is not. The events themselves are noted where they happen (core/mood.note),
and note() does nothing while this feature is off.
"""
import json

from features.base import Feature as Base


class Feature(Base):
    name = "mood"
    summary = ("How she is — irritation, engagement, strain (core/mood.py) — moved by what happens to her, "
               "fading on its own; shown on the orb and added to her dials.")
    owns = ("core.mood",)
    order = 15
    tick_every_s = 60.0
    first_tick_after_s = 60.0

    async def _payload(self) -> str:
        from core import mood
        return "__MOOD__" + mood.payload(await mood.state())

    async def tick(self):
        from ws.ws_handlers import broadcast_signal, _active_connections
        if _active_connections:
            await broadcast_signal(await self._payload())

    async def on(self, event: str, **kw):
        if event == "connect" or (event == "reply" and not kw.get("interrupted")):
            ws = kw.get("websocket")
            if ws is not None:
                try:
                    await ws.send_text(await self._payload())
                except Exception:
                    pass

    async def diagnose(self):
        from core import mood
        axes = mood.decayed(await mood.state())
        return True, ", ".join(f"{k} {v:.1f}/10" for k, v in axes.items())

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Mood — " + msg
