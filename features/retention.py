# features/retention.py
"""
What she forgets on purpose (core/retention.py): once a day, old
observations, bookkeeping decisions, retracted memory, old sessions,
surplus backups and rotated logs. First pass ten minutes after start.
Off: nothing is removed; the store grows until it is switched back on.
"""
import time

from features.base import Feature as Base


class Feature(Base):
    name = "retention"
    summary = ("What she forgets on purpose (core/retention.py): once a day, old observations, bookkeeping "
               "decisions, retracted memory, old sessions, backups and rotated logs.")
    owns = ("core.retention",)
    order = 80
    tick_every_s = 24 * 3600.0
    first_tick_after_s = 600.0

    async def tick(self):
        from core import retention
        await retention.prune()

    async def diagnose(self):
        from db.db import get_retention_summary
        summary = await get_retention_summary()
        if not summary:
            return True, "has not run yet"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(summary.get("at", 0))))
        removed = {k: v for k, v in (summary.get("removed") or {}).items() if v}
        return True, f"last ran {when}; removed {removed if removed else 'nothing'}"

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Retention — " + msg
