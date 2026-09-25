# features/base.py
"""
One shape for every built-in part of her that can be switched off, taken
offline, changed, and brought back.

2026-09-25, Craig: "maximum modularity... in the future I can say to her
to make a change to her personality module, and she would take it
offline, perform the change and bring it back up as needed." And, on the
things built this week (mood, sight, the pet, what he values, curiosity,
retention): "Are we still placing these additions into modules or have we
lost sight of that?"

We had. Each of those was wired by hand: its own loop in main.py, its own
block in the LLM system's prompt, its own tool in core/tools.py, its own
send in the WebSocket handler. Nothing could be switched off short of
editing four files, and "reload" meant a restart of her.

A Feature is that wiring in one place. The ENGINE — the arithmetic, the
state, the model calls — stays where it is (core/mood.py, core/sight.py,
core/pet.py, ...): those files are what the harness tests and what the
Controller and her author read, and moving them would be packaging, not
modularity (SELF_MODIFICATION_ARCHITECTURE.md: "'Everything is a module'
is about self-adjustment, not packaging"). A feature declares which
engine modules it OWNS; the registry reloads those with it, and watches
their files for changes the same way systems/ are watched.

What a feature may contribute (all optional):

  start / stop        its background work (a task, a resource)
  tick                periodic work, every `tick_every_s`
  prompt_block        text into her system prompt for this turn
  tools / run_tool    tools she may call (core/tools.py offers them)
  on(event, ...)      something happened: connect, turn, reply, thanked,
                      corrected
  diagnose            its own self-check, for the sweep
  status_line         one line for the Controller and "check the X module"

Every call is guarded by the registry (features/registry.py); a broken
feature is reported, never fatal to a turn. A feature that is OFF
contributes nothing anywhere, and its engine's gate (core/mood.note,
for one) does nothing, so "off" is really off.

In her vocabulary these are modules — "disable module mood", "check the
pet module" — alongside the sandboxed command modules under modules/.
In code they are features, to keep the two kinds apart.
"""
import time


class Feature:
    # ---- identity -------------------------------------------------------
    name = ""                 # equals the file name: features/<name>.py
    summary = ""              # one sentence for the Controller and the sweep
    owns = ()                 # dotted engine modules reloaded with it: ("core.mood",)
    order = 50                # where its prompt block goes, lowest first

    # ---- periodic work --------------------------------------------------
    tick_every_s = None       # None: no tick
    first_tick_after_s = 60.0

    def __init__(self):
        self.started_at = 0.0

    # ---- lifecycle ------------------------------------------------------
    async def start(self):
        self.started_at = time.time()

    async def stop(self):
        pass

    async def tick(self):
        pass

    # ---- what she gets from it ------------------------------------------
    async def prompt_block(self, **kw) -> str:
        """kw: user_id, session, text, context_blocks, creator. Return ""
        when there is nothing to say; a block begins with "\\n\\n    "."""
        return ""

    def tools(self) -> list:
        """Tool specs in core/tools._fn's shape."""
        return []

    def tool_timeout(self, name: str):
        """Seconds for one call of this tool, or None for the default."""
        return None

    async def run_tool(self, name: str, args: dict, user_id: str):
        """A plain string she can read, or None if the tool is not hers."""
        return None

    # ---- what happens to her --------------------------------------------
    async def on(self, event: str, **kw):
        """connect(websocket, session, user_id) · turn(user_id, session,
        text, creator) · reply(websocket, user_id, session, text,
        interrupted, looked) · thanked(user_id, session, creator) ·
        corrected(user_id, session, creator)."""
        pass

    # ---- looking at itself ----------------------------------------------
    async def diagnose(self):
        """(ok, message). Reads the thing itself; never an opinion."""
        return True, ""

    async def status_line(self) -> str:
        return ""
