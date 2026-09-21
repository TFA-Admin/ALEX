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
#
# 2026-09-21 (ANOMALIES: "Logs vanish before the incident is read"): the
# count was 5 and the prune ran at IMPORT, so every helper script, test
# or Controller that imported this module created a new, usually empty,
# alex_*.log and pushed a real one out. Found live: after one afternoon
# of tooling, the only surviving logs were the live one and four empty
# files, and the night's incidents were gone. Two changes: the file is
# opened on the first record actually written (delay=True), so a process
# that never logs creates nothing; and the prune runs only then, keeps
# more, and drops empty files first.
MAX_LOG_FILES = 20


def _prune_old_logs():
    paths = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
             if f.startswith("alex_") and f.endswith(".log")]
    kept = []
    for path in paths:
        try:
            if os.path.getsize(path) == 0 and path != LOG_FILE:
                os.remove(path)
                continue
        except OSError:
            continue
        kept.append(path)
    kept.sort(key=os.path.getmtime)
    for old in kept[:-MAX_LOG_FILES]:
        try:
            os.remove(old)
        except OSError:
            pass


class _LazyRotatingFileHandler(RotatingFileHandler):
    """Opens the file on the first record, and prunes the folder then."""

    def _open(self):
        _prune_old_logs()
        return super()._open()


logger = logging.getLogger("ALEX")
logger.setLevel(logging.DEBUG)

file_handler = _LazyRotatingFileHandler(
    LOG_FILE,
    maxBytes=5*1024*1024,
    backupCount=3,
    encoding="utf-8",
    delay=True,
)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)