# core/retention.py
"""
What she forgets, on purpose.

2026-09-23, Craig: "does anything ever get removed? While this eventually
spiral into a massive pile of data and should we have it cleanup
irrelevant old data?" Measured that afternoon, after five months: the
database was 3.0 MB; memory grows ~160 rows a day (each with a 1.7 KB
embedding), decisions ~80 a day of which most are "tool" and "speaker"
bookkeeping; logs keep 20 files; voice and face samples roll at 15;
mood keeps 30 events. Nothing else was ever removed, and the Ollama log
was one 9.7 MB file that only grew.

Not a spiral at that rate — a few hundred megabytes a year at the
heaviest use so far — but glances (core/sight.py) add observations, and
a store that is never emptied is a bad habit. So, once a day:

    observations           older than 14 days       (a glance is not a memory)
    decisions tool/speaker older than 30 days       (bookkeeping, not her reasoning)
    memory, retracted      older than 30 days       (never read back; kept a month to audit)
    sessions               older than 180 days
    db/backups             all but the newest 10 files
    ollama_output.*.log    rotated copies beyond the newest 2 (the Controller
                           rotates the live log at Ollama start when it is
                           past 5 MB; cutting it in place was wrong — Ollama
                           writes at its own offset and refilled the gap with
                           zeros, 100% NUL from 1.5 MB to the tail, 2026-09-24)

What is never pruned: her conversation memory that is not retracted,
her decisions (conclusions, proposals, corrections, fabrications,
approvals), eval runs, curiosity, facts, profiles, personality history.
That is her record; the harness and the Controller read it.

Every run logs what it removed and keeps the summary in
system_learning.retention_last for Her -> Health.
"""
import glob
import json
import os
import time

from config.logger_config import logger

ALEX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

POLICY = {
    "observations_days": 14,
    "decisions_noise_days": 30,
    "decisions_noise_kinds": ("tool", "speaker"),
    "memory_retracted_days": 30,
    "sessions_days": 180,
    "backups_keep": 10,
    "ollama_logs_keep": 2,          # rotated copies; the live one is the Controller's
}

BACKUP_DIR = os.path.join(ALEX_DIR, "db", "backups")
OLLAMA_LOG = os.path.join(ALEX_DIR, "config", "Logs", "ollama_output.log")


def _prune_backups(keep: int) -> int:
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "*")), key=os.path.getmtime, reverse=True)
    removed = 0
    for f in files[keep:]:
        try:
            os.remove(f)
            removed += 1
        except OSError:
            pass
    return removed


def _prune_ollama_logs(keep: int) -> int:
    """Rotated Ollama logs beyond the newest `keep`. The live file is never
    touched here: a process holds it open and writes at its own offset."""
    pattern = OLLAMA_LOG.replace("ollama_output.log", "ollama_output.*.log")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    removed = 0
    for f in files[keep:]:
        try:
            os.remove(f)
            removed += 1
        except OSError:
            pass
    return removed


async def prune() -> dict:
    from db.db import retention_prune, set_retention_summary
    t0 = time.time()
    try:
        counts = await retention_prune(POLICY)
    except Exception as e:
        logger.warning(f"[RETENTION] database pass failed: {e}")
        counts = {"error": str(e)[:200]}
    counts["backups_removed"] = _prune_backups(POLICY["backups_keep"])
    counts["ollama_logs_removed"] = _prune_ollama_logs(POLICY["ollama_logs_keep"])
    summary = {"at": time.time(), "seconds": round(time.time() - t0, 2), "removed": counts}
    try:
        await set_retention_summary(summary)
    except Exception as e:
        logger.warning(f"[RETENTION] could not store the summary: {e}")
    removed = {k: v for k, v in counts.items() if v}
    logger.info(f"[RETENTION] {removed if removed else 'nothing to remove'}")
    return summary
