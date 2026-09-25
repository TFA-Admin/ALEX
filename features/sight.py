# features/sight.py
"""
Her eyes (core/sight.py): a look through the page's camera when asked or
when he points at something, his face at connect, and glances in quiet
time that become observations she can bring up. This feature owns the
glance tick, the `look` tool, and the two prompt blocks — what she saw
this turn, and what she has noticed lately. Off: no glances, no look tool,
no camera blocks; the page's eyes toggle still works but nothing reads
the frames, and verification at connect falls back to voice.
"""
from features.base import Feature as Base


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


class Feature(Base):
    name = "sight"
    summary = ("Her eyes (core/sight.py): a look through the page's camera when asked or when he points at "
               "something, his face at connect, and glances in quiet time that become observations.")
    owns = ("core.sight",)
    order = 20
    first_tick_after_s = 30.0

    @property
    def tick_every_s(self):
        from core import sight
        return sight.GLANCE_EVERY_S

    async def tick(self):
        from core import sight
        await sight.glance_all()

    # ---- tools ----------------------------------------------------------
    def tools(self) -> list:
        return [_fn("look",
                    "Look through the camera on the page of the person you are talking to: one frame, "
                    "described, with whose face it is if you know them. Use it when asked what you see, "
                    "who is there, what they are holding or wearing, or to look at something. Their page "
                    "must have its eyes open; a frame is taken only when you look.",
                    {"question": {"type": "string", "description": "what they asked you to look at or for, if anything"}})]

    def tool_timeout(self, name: str):
        from core import sight
        return sight.LOOK_TIMEOUT_S if name == "look" else None

    async def run_tool(self, name: str, args: dict, user_id: str):
        if name != "look":
            return None
        from core import sight
        return await sight.look(user_id, str(args.get("question") or args.get("query") or ""))

    # ---- prompt ---------------------------------------------------------
    async def prompt_block(self, user_id: str = "", context_blocks=None, **kw) -> str:
        from core import claims
        # 2026-09-23: a look this turn is repeated here, last thing before
        # she speaks. Among the context blocks it lost to a conversation
        # window full of her own "the camera remains dark".
        saw_now = claims.look_saw("\n".join(context_blocks)) if context_blocks else ""
        block = ((f"\n\n    YOU LOOKED THROUGH THE CAMERA JUST NOW AND SAW: {saw_now}"
                  "\n    The camera is on and that is the picture. Say what you saw; never say it is dark or that you cannot see.")
                 if saw_now else "")
        # 2026-09-23: what her glances found lately, so she can refer to it
        # — "the can that has been on your desk since three".
        try:
            from db.db import fetch_observations
            obs = await fetch_observations(user_id, hours=2.0, limit=3)
        except Exception:
            obs = []
        if obs:
            lines = "\n".join(f"    - {o['text']}" for o in reversed(obs))
            # 2026-09-25 (Craig: "she is now saying 'glancing at the camera'"):
            # the word "glances" in this header became her verb. What she
            # has is things she noticed; the looking is never described.
            block += ("\n\n    THINGS YOU HAVE NOTICED THROUGH THE CAMERA LATELY (bring one up only if it matters "
                      "or he asks, and then say what you noticed — never say you glanced, looked, checked "
                      "or are watching):\n" + lines)
        return block

    # ---- self-check -----------------------------------------------------
    async def diagnose(self):
        from core import sight
        from ws.ws_handlers import _active_connections
        if not sight.available():
            return False, "face models missing under models/vision"
        eyes = sum(1 for c in _active_connections.values() if c.get("eyes"))
        return True, f"face models present; {eyes} of {len(_active_connections)} page(s) with eyes open"

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Sight — " + msg
