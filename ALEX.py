# ALEX.py
"""
Launcher stub for A.L.E.X.
Ensures that crashes on startup are logged immediately.
"""
import os
import sys
from config.logger_config import logger

try:
    logger.info("🚀 Starting A.L.E.X...")

    if __name__ == "__main__":
        import uvicorn
        from main import app  # Import app only at runtime

        # 2026-09-21: ALEX_PORT lets a staging copy of her (a proposal's
        # worktree, launched by the Controller — controller/versions.py)
        # run beside the live one. Default unchanged.
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.getenv("ALEX_PORT", "5000")),
            log_level="info",
            access_log=True,

            # 🔥 HTTPS ENABLED
            ssl_keyfile="certs/key.pem",
            ssl_certfile="certs/cert.pem"
        )

except Exception:
    logger.exception("💥 Fatal error on A.L.E.X startup")
    sys.exit(1)