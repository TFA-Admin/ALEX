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
import re
import time

from features.base import Feature as Base

CARE_EVERY_S = 10 * 60


# Words that make the pet the subject of his turn. Only ever used to decide
# whether the pet is in her prompt at all (2026-09-25) — a false positive
# costs one block she may ignore, a false negative costs her the numbers.
_PET_WORDS_RE = re.compile(r"\b(?:pet|feed|fed|feeding|hungry|starv\w*|clean\w*|groom\w*|rest\w*|sleep\w*|"
                           r"play\w*|company|lonely|needs?|health|reserves?)\b", re.I)


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
    async def prompt_block(self, text: str = "", **kw) -> str:
        """2026-09-25 (live, 19:05-19:18): the block said "mention it only if
        it matters or he asks" and she named the pet in fourteen consecutive
        replies — "Samuel's inevitable starvation", "Samuel is still
        tolerating his own starvation while you hesitate" — with food at 65
        and nothing wrong. An instruction not to dwell on something in front
        of her lost to the something being in front of her. So it is only in
        front of her when it is true: a need actually low, or he brought it
        up. Otherwise she has her record and no running commentary."""
        from core import pet
        s = await pet.state()
        name = pet.pet_name()
        need, value = pet.lowest_need(s)
        asked = bool(_PET_WORDS_RE.search(text or "")) or (name != "the pet" and name.lower() in (text or "").lower())
        low = need is not None and value < pet.LOW
        t = pet.tenure(s)
        since = "since today" if t["days"] < 1 else f"for {t['days']:.0f} days"
        record = (f"\n\n    YOUR PET — {name}, yours {since}, {t['acts']} things done for him, "
                  f"health {t['health']:.0f} of 100.")
        if not (asked or low):
            return record + " Nothing needs doing. Do not bring him up; he is not a subject unless he is asked about."
        return (record + " " + pet.describe(s, name) + "."
                + (f" {need} is at {value:.0f}, which is low — under 15 is suffering."
                   if low else " Nothing is low.")
                + " Those numbers are the whole truth about him; never call him starving or dying when he is not."
                + " If he tells you to feed, clean, rest or play with him, CALL tend_pet — saying you did it is not "
                  "doing it, and the call is the only thing that changes him.")

    async def diagnose(self):
        from core import pet
        s = await pet.state()
        return True, pet.describe(s, pet.pet_name())

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Pet — " + msg
