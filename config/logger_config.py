#logger_config.py
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

ROOT_DIR = os.path.dirname(__file__)
LOG_DIR = os.path.join(ROOT_DIR, "Logs")
os.makedirs(LOG_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOG_FILE = os.path.join(LOG_DIR, f"alex_{timestamp}.log")

# A brand-new alex_*.log is created on every process start (restarts are
# frequent during development) and RotatingFileHandler's backupCount only
# rotates within a single one of those once it hits maxBytes — nothing
# ever cleaned up the older per-run files themselves, so they piled up
# indefinitely (185 files found live, 2026-07-16). Keep only the
# MAX_LOG_FILES most recent.
MAX_LOG_FILES = 5


def _prune_old_logs():
    logs = sorted(
        f for f in os.listdir(LOG_DIR)
        if f.startswith("alex_") and f.endswith(".log")
    )
    for old in logs[:-MAX_LOG_FILES]:
        try:
            os.remove(os.path.join(LOG_DIR, old))
        except OSError:
            pass


logger = logging.getLogger("ALEX")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5*1024*1024,
    backupCount=3,
    encoding="utf-8"
)

_prune_old_logs()
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)