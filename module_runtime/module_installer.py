import os

from module_runtime.validator import check_safety
from db.db import log_security_event
from config.logger_config import logger

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "modules")


async def install_module(name, code, user_id=None):
    module_dir = os.path.join(MODULES_PATH, name)

    if not os.path.exists(module_dir):
        os.makedirs(module_dir)

    module_file = os.path.join(module_dir, "module.py")
    print("📁 Writing module to:", module_file)

    safe, reason = check_safety(code)

    if not safe:
        print("❌ VALIDATION FAILED:", reason)
        print(code)

        # syntax_error is routine LLM sloppiness, not a security concern.
        # blocked_import/blocked_call means the code tried to exceed the
        # sandbox — that's worth the creator knowing about.
        if reason and not reason.startswith("syntax_error"):
            await log_security_event(
                user_id or "unknown",
                "module_build_blocked",
                f"module={name} reason={reason}"
            )
            logger.warning(f"[ACTION] Blocked module build '{name}' (requested by {user_id}): {reason}")

        return False, "Module failed safety validation."

    with open(module_file, "w", encoding="utf-8") as f:
        f.write(code)

    return True, module_file