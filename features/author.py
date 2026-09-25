# features/author.py
"""
Her idle author (core/idle_author.py, core/self_author.py): in quiet
time, one whitelisted setting looked at against her own numbers; a
proposal lands in his Inbox and nothing changes without him. This
feature owns the loop's task. The Controller's "Let her author proposals
while idle" switch still applies inside the loop (idle_author.enabled());
this is the coarser switch, and the thing "reload module author" reloads.
"""
import asyncio

from features.base import Feature as Base


class Feature(Base):
    name = "author"
    summary = ("Her idle author (core/idle_author.py, core/self_author.py): in quiet time, one whitelisted "
               "setting looked at against her numbers; a proposal lands in his Inbox, nothing changes without him.")
    owns = ("core.idle_author", "core.self_author")
    order = 90

    def __init__(self):
        super().__init__()
        self._task = None

    async def start(self):
        await super().start()
        from core import idle_author
        self._task = asyncio.create_task(idle_author.run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def diagnose(self):
        from core import idle_author
        from core.self_author import WHITELIST
        from db.db import fetch_proposals
        if self._task is None or self._task.done():
            return False, "the loop is not running"
        rows = await fetch_proposals(limit=100)
        open_rows = sum(1 for r in rows if r.get("status") in idle_author.OPEN_STATUSES)
        return True, (f"{'on' if idle_author.enabled() else 'off at the Controller switch'}; "
                      f"{open_rows} proposal(s) open of {idle_author.MAX_OPEN}; {len(WHITELIST)} targets")

    async def status_line(self) -> str:
        _ok, msg = await self.diagnose()
        return "Author — " + msg
