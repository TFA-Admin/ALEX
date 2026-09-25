# features/pet.py
"""
Her pet (core/pet.py): four needs fall on the clock, health follows, and
she tends the lowest herself in quiet time. This feature owns the care
pass (every ten minutes), the page's pet line (every minute), the two
tools, and the prompt line. Off: the pet's clock stops with her — an
absence is not neglect (MAX_GAP_HOURS in core/pet.py) — and she has no
pet tools or pet line.
"""
import json
import time

from features.base import Feature as Base

CARE_EVERY_S = 10 * 60


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


class Feature(Base):
    name = "pet"
    summary = ("Her pet (core/pet.py): four needs fall on the clock, health follows; she tends the lowest "
               "herself in quiet time; shown on the page and in the Controller's Pet tab.")
    owns = ("core.pet",)
    order = 40
    tick_every_s = 60.0
    first_tick_after_s = 90.0

    def __init__(self):
        super().__init__()
        self._last_care = 0.0

    async def tick(self):
        from core import pet
        from ws.ws_handlers import broadcast_signal, _active_connections
        now = time.time()
        if now - self._last_care >= CARE_EVERY_S:
            self._last_care = now
            await pet.care_pass()
        if _active_connections:
            await broadcast_signal("__PET__" + json.dumps(pet.status_for_page(await pet.state())))

    # ---- tools ----------------------------------------------------------
    def tools(self) -> list:
        return [
            _fn("pet_status",
                "How your pet is: its needs (food, rest, clean, company), its health, and what was done for it lately."),
            _fn("tend_pet",
                "Do one thing for your pet now: feed, rest, clean or play. In quiet time you do this yourself; "
                "in conversation use it when a need is suffering or someone asks you to.",
                {"action": {"type": "string", "enum": ["feed", "rest", "clean", "play"],
                            "description": "feed, rest, clean or play"}}, ["action"]),
        ]

    async def run_tool(self, name: str, args: dict, user_id: str):
        from core import pet
        if name == "pet_status":
            s = await pet.state()
            lines = [pet.describe(s, pet.pet_name())]
            for entry in (s.get("log") or [])[-5:]:
                lines.append(f"- {time.strftime('%H:%M', time.localtime(float(entry.get('t', 0))))} "
                             f"{entry.get('by', '?')}: {entry.get('action')} ({entry.get('before', 0):.0f} -> {entry.get('after', 0):.0f})")
            return "\n".join(lines)
        if name == "tend_pet":
            action = str(args.get("action", "")).strip().lower()
            if action not in pet.ACTIONS:
                return f"tend_pet takes one of: {', '.join(pet.ACTIONS)}."
            return await pet.tend(action, by="her", why="asked for, or judged urgent, in conversation")
        return None

    # ---- prompt ---------------------------------------------------------
    async def prompt_block(self, **kw) -> str:
        from core import pet
        s = await pet.state()
        return ("\n\n    YOUR PET — " + pet.describe(s, pet.pet_name())
                + ". You tend it yourself in quiet time; in conversation, tend_pet only if a need is "
                  "suffering or someone asks. Mention it only if it matters or he asks.")

    async def diagnose(self):
        from core import pet
        s = await pet.state()
        return True, pet.describe(s, pet.pet_name())

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Pet — " + msg
