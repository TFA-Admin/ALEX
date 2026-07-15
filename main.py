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
    while True:
        try:
            await run_self_reflection()
        except Exception as e:
            logger.exception(f"❌ Self-reflection failed: {e}")

        await asyncio.sleep(3600)  # every hour


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