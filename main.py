import asyncio
import os
import threading
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import sys
from api.routes import router as api_router
from ws.ws_handlers import register_ws
from utils.utils import get_lan_ip
from db.db import init_db, decay_memory, sessions_reset_on_boot
from llm.ollama_client import ollama_manager
from speech.tts_engine import shutdown_tts
from core.self_reflection import run_self_reflection
from core.proactive import periodic_proactive_check
from core import readiness
from config.logger_config import logger

sys.stdout.reconfigure(encoding='utf-8')
# -------------------------
# MEMORY DECAY LOOP
# -------------------------
# 2026-09-20: was hourly, which made the whole decay idea inert. It subtracts
# 1 per pass with a floor of 1, against signals that fire a few times a day at
# most, so it ran ~24x faster than anything could replenish. Measured before
# changing it: all 572 memory rows sat at weight 1.
#
# The memory-weight half has since been removed outright at Craig's call (see
# systems/memory/system.py). What remains here decays facts.importance only.
#
# Worth flagging: nothing currently READS facts.importance either, so this loop
# is arguably in the same position weight was in. Left in place rather than
# removed because update_fact() still sets it deliberately and it may be wanted
# for fact prioritisation later - but it is the next candidate if it stays
# unused.
DECAY_INTERVAL_S = 7 * 24 * 3600


async def periodic_decay():
    while True:
        try:
            logger.info("🧠 Running memory decay...")
            await decay_memory()
        except Exception as e:
            logger.exception(f"❌ Memory decay failed: {e}")

        await asyncio.sleep(DECAY_INTERVAL_S)


# -------------------------
# SELF-REFLECTION LOOP (personality — fully autonomous, no approval gate)
# -------------------------
async def periodic_self_reflection():
    # 2026-07-16: found live — this used to fire immediately on every
    # startup with zero delay, meaning its own real Ollama calls (curiosity
    # check + personality check, and up to 5 phrase re-voices if
    # personality actually changes) directly competed with whatever the
    # creator asked right after restarting — exactly the window Craig
    # tests in every time. A cold delay before the first pass keeps this
    # loop out of the way of the immediate post-restart conversation.
    await asyncio.sleep(180)

    while True:
        try:
            await run_self_reflection()
        except Exception as e:
            logger.exception(f"❌ Self-reflection failed: {e}")

        # 2026-07-17: shortened again, from 900s to 120s — run_self_reflection()
        # itself now gates the actual work behind a real idle check
        # (IDLE_BEFORE_REFLECTION_S in core/self_reflection.py: no LLM
        # call at all unless there's been a genuine lull in conversation),
        # so polling more often just means noticing a real downtime
        # window sooner, not doing more work more often. Craig: "have her
        # know if there's room for her to make changes, like a downtime,
        # and kick off the things she wants to adjust" — this is the
        # mechanism. The earlier 900s interval was a blind timer with no
        # idea whether Craig was mid-conversation; this one only ever
        # acts during an actual quiet stretch.
        await asyncio.sleep(120)


# -------------------------
# MODULE BUILDING (2026-07-16): no longer automated. Approved requests
# just wait in module_build_requests (status='approved') until Claude
# picks them up directly in an active session — reads module_name/prompt,
# writes the code, determines if it needs elevated access, and installs
# it. See SELF_MODIFICATION_ARCHITECTURE.md's Component 2 addendum and
# tools/pending_builds.py. This used to be a periodic loop calling local
# deepseek-coder generation (module_runtime/dormant/module_generator.py,
# still in the codebase but dormant) — removed because that generation approach
# had a real capability ceiling on anything beyond trivial scaffolds, not
# a tuning problem.
# -------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):

    # -------------------------
    # INIT DB
    # -------------------------
    await init_db()
    logger.info("✅ Database ready")
    try:
        stale = await sessions_reset_on_boot()
        if stale:
            logger.info(f"👥 Closed {stale} session row(s) left open by the previous process")
    except Exception as e:
        logger.warning(f"⚠️ could not reset session rows: {e}")

    # -------------------------
    # START OLLAMA (THREAD)
    # -------------------------
    def start_ollama():
        try:
            asyncio.run(ollama_manager.init())
        except Exception as e:
            logger.exception(f"❌ Ollama startup failed: {e}")

    threading.Thread(target=start_ollama, daemon=True).start()
    logger.info("🌐 Ollama starting in background")

    # 2026-09-20: pull the model into VRAM now, while the UI is still
    # connecting, instead of on the first thing anyone says. Craig measured
    # the old behaviour live — 9.3s of silence after "Alex." on a fresh
    # launch, all of it the model loading. Runs as a task so startup is not
    # blocked on it, and broadcasts its state so the page can say what is
    # happening rather than looking unresponsive. See core/readiness.py.
    asyncio.create_task(readiness.warm_up(ollama_manager))

    from core.alex_core import alex_core
    await alex_core.init_systems()
    logger.info("🧠 Core systems initialized")

    # -------------------------
    # START MEMORY DECAY LOOP
    # -------------------------
    asyncio.create_task(periodic_decay())
    asyncio.create_task(periodic_self_reflection())
    asyncio.create_task(periodic_proactive_check())
    # 2026-09-21 (roadmap item 6): her author, when nobody is talking to
    # her. Writes one 'authored' proposal row at a time; nothing more.
    from core import idle_author
    asyncio.create_task(idle_author.run())

    yield

    # 2026-07-18: the persistent Piper process (speech/tts_engine.py)
    # needs an explicit kill on shutdown — exactly the kind of orphaned
    # background process this project spent real effort hunting down
    # elsewhere tonight (see the Ollama runner-orphan fixes).
    shutdown_tts()

    logger.info("🔴 Shutdown complete")


# -------------------------
# APP INIT
# -------------------------
app = FastAPI(title="A.L.E.X Backend", lifespan=lifespan)

app.include_router(api_router)

register_ws(app)

app.mount("/static", StaticFiles(directory="static"), name="static")


# -------------------------
# AVATAR ROUTE
# -------------------------
@app.get("/avatar", include_in_schema=False)
async def avatar_page():
    from fastapi.responses import FileResponse
    # 2026-09-20: no-cache. This page changes most sessions, and a browser
    # holding a stale copy produces symptoms that look like server bugs —
    # a UI stuck on "Starting up…" while the server log says ready, for
    # instance. It is one small file on localhost; caching it buys nothing
    # and costs real debugging time.
    return FileResponse("static/avatar.html", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


# -------------------------
# COMMANDS REFERENCE (2026-09-21)
# -------------------------
# Craig: "I was hoping more for a reference either in the webui or the
# controller." COMMANDS.md, drawn by core/markdown_lite.py, at /commands;
# the avatar page's rail has a button to it. One source, no cache, so an
# edit to the file is what you see on the next open.
@app.get("/commands", include_in_schema=False)
async def commands_page():
    from fastapi.responses import HTMLResponse
    from core.markdown_lite import render_page
    with open("COMMANDS.md", encoding="utf-8") as fh:
        text = fh.read()
    return HTMLResponse(render_page(text, title="What you can say to her"), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    })


# -------------------------
# START LOG
# -------------------------
LAN_IP = get_lan_ip()
logger.info(f"🌐 A.L.E.X running on {LAN_IP}:{os.getenv('ALEX_PORT', '5000')}")