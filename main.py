import asyncio
import threading
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import sys
from api.routes import router as api_router
from ws.ws_handlers import register_ws
from utils.utils import get_lan_ip
from db.db import init_db, decay_memory
from llm.ollama_client import ollama_manager
from core.self_reflection import run_self_reflection
from config.logger_config import logger

sys.stdout.reconfigure(encoding='utf-8')
# -------------------------
# MEMORY DECAY LOOP
# -------------------------
async def periodic_decay():
    while True:
        try:
            logger.info("🧠 Running memory decay...")
            await decay_memory()
        except Exception as e:
            logger.exception(f"❌ Memory decay failed: {e}")

        await asyncio.sleep(3600)  # every hour


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

    from core.alex_core import alex_core
    await alex_core.init_systems()
    logger.info("🧠 Core systems initialized")

    # -------------------------
    # START MEMORY DECAY LOOP
    # -------------------------
    asyncio.create_task(periodic_decay())
    asyncio.create_task(periodic_self_reflection())

    yield

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
    return FileResponse("static/avatar.html")


# -------------------------
# START LOG
# -------------------------
LAN_IP = get_lan_ip()
logger.info(f"🌐 A.L.E.X running on {LAN_IP}:5000")