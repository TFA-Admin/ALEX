import asyncio
import threading
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import sys
from api.routes import router as api_router
from ws.ws_handlers import register_ws
from utils.utils import get_lan_ip
from db.db import (
    init_db, decay_memory,
    fetch_approved_module_build_requests, resolve_module_build_request
)
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


# -------------------------
# MODULE BUILD APPROVAL LOOP (creator approves via the Controller's DB
# tab or in-conversation; this is what actually performs the build once
# approved — the Controller is a separate process and can only mark the
# DB row, not run in-process module_runtime code itself)
# -------------------------
async def periodic_module_builds():
    from core.alex_core import alex_core

    while True:
        try:
            requests = await fetch_approved_module_build_requests()
            modules_system = alex_core.systems.systems.get("modules")

            if modules_system:
                for req in requests:
                    logger.info(
                        f"[ACTION] Building approved module '{req['module_name']}' "
                        f"(request #{req['id']}, originally requested by {req['requested_by']})"
                    )

                    result = await modules_system._build_module(
                        req["module_name"], req["requested_by"], req["prompt"]
                    )
                    content = result.get("content", "") if result else "Build failed."
                    success = "failed" not in content.lower()

                    await resolve_module_build_request(
                        req["id"], "built" if success else "failed", content
                    )
        except Exception as e:
            logger.exception(f"❌ Module build approval loop failed: {e}")

        await asyncio.sleep(10)


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
    asyncio.create_task(periodic_module_builds())

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