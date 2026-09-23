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
    ollama_output.log      cut to its last megabyte when past 5 MB

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
    "ollama_log_mb": 5,
    "ollama_log_keep_mb": 1,
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


def _trim_ollama_log(max_mb: float, keep_mb: float) -> int:
    """Bytes cut. The Controller holds this file open for append; cutting
    the head and rewriting the tail is fine for an append-only writer."""
    try:
        size = os.path.getsize(OLLAMA_LOG)
    except OSError:
        return 0
    if size <= max_mb * 1e6:
        return 0
    keep = int(keep_mb * 1e6)
    try:
        with open(OLLAMA_LOG, "rb") as f:
            f.seek(-keep, os.SEEK_END)
            tail = f.read()
        nl = tail.find(b"\n")
        tail = tail[nl + 1:] if nl >= 0 else tail
        with open(OLLAMA_LOG, "wb") as f:
            f.write(b"[... earlier output removed by core/retention.py ...]\n" + tail)
        return size - len(tail)
    except OSError as e:
        logger.warning(f"[RETENTION] could not trim the Ollama log: {e}")
        return 0


async def prune() -> dict:
    from db.db import retention_prune, set_retention_summary
    t0 = time.time()
    try:
        counts = await retention_prune(POLICY)
    except Exception as e:
        logger.warning(f"[RETENTION] database pass failed: {e}")
        counts = {"error": str(e)[:200]}
    counts["backups_removed"] = _prune_backups(POLICY["backups_keep"])
    counts["ollama_log_bytes_cut"] = _trim_ollama_log(POLICY["ollama_log_mb"], POLICY["ollama_log_keep_mb"])
    summary = {"at": time.time(), "seconds": round(time.time() - t0, 2), "removed": counts}
    try:
        await set_retention_summary(summary)
    except Exception as e:
        logger.warning(f"[RETENTION] could not store the summary: {e}")
    removed = {k: v for k, v in counts.items() if v}
    logger.info(f"[RETENTION] {removed if removed else 'nothing to remove'}")
    return summary
