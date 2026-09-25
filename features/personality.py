# features/personality.py
"""
Her dials (core/traits.py): his standing adjustments plus what her mood
adds this turn, rendered into her prompt; the verbosity dial also caps
her reply. Off: no dials block, default reply length, the personality
text alone.

The personality TEXT, the hard rules and the persona switch stay in the
LLM system — they are her identity, not a feature, and the Controller's
kill path (mute persona) must not depend on a module being up.
"""
from features.base import Feature as Base


class Feature(Base):
    name = "personality"
    summary = ("Her dials (core/traits.py): his standing adjustments plus what her mood adds, "
               "rendered into her prompt; verbosity also caps the reply.")
    owns = ("core.traits",)
    order = 10

    async def effective_dials(self):
        """(effective offsets or None, mood state or None) for this turn."""
        from core import traits, mood
        from db.db import get_personality_traits
        try:
            dials = await get_personality_traits()
        except Exception:
            dials = None
        try:
            mood_state = await mood.state()
        except Exception:
            mood_state = None
        return traits.effective(dials, mood.dial_offsets(mood_state) if mood_state else {}), mood_state

    async def prompt_block(self, **kw) -> str:
        from core import traits, mood
        dials, mood_state = await self.effective_dials()
        return traits.render(dials, mood_line=mood.line(mood_state) if mood_state else "")

    async def diagnose(self):
        from core import traits
        from db.db import get_personality_traits
        dials = await get_personality_traits()
        if dials is None:
            return True, "nothing adjusted"
        t = traits.normalize(dials)
        moved = {k: v for k, v in t.items() if v}
        return True, ("adjusted: " + ", ".join(f"{k} {traits.signed(v)}" for k, v in moved.items())) if moved else "all at zero"

    async def status_line(self) -> str:
        ok, msg = await self.diagnose()
        return f"Dials — {msg}"
