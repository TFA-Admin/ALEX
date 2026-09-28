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
    "value_signals_days": 90,
    "decisions_noise_days": 30,
    "decisions_noise_kinds": ("tool", "speaker"),
    "memory_retracted_days": 30,
    "sessions_days": 180,
    "backups_keep": 14,             # one a day from the pass below, so ~two weeks
    "ollama_logs_keep": 2,          # rotated copies; the live one is the Controller's
}

BACKUP_DIR = os.path.join(ALEX_DIR, "db", "backups")
BACKUP_STALE_H = 36.0           # older than this and the sweep calls it a problem
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


def backup_database(tag: str = "daily") -> dict:
    """A copy of her database, taken safely while she is running.

    2026-09-25/28 (Craig: "she could potentially break herself and there
    would be nothing we could do?" — then "sounds like we need an automated
    db backup built"). He was right and my earlier "recoverable from backups"
    was wrong: the only copies on disk were two files from 2026-09-20, made
    as a side effect of a maintenance tool run by hand. Nothing took one on a
    schedule, `db/*.db` is gitignored so git held none of it, and retention
    was already pruning this directory to the newest N — the design assumed
    backups existed and nothing ever wrote one. Meanwhile retention itself
    deletes rows every day, unattended.

    sqlite3's own backup API, not a file copy: it takes a consistent snapshot
    of a live database while her process is writing to it, where `shutil.copy`
    can catch a torn page. The copy is then opened and integrity-checked,
    because a corrupt backup is worse than an honest absence.
    """
    import shutil
    import sqlite3
    from db.db import DB_PATH

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    out = os.path.join(BACKUP_DIR, f"{tag}_{stamp}.db")
    try:
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(out)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as e:
        logger.warning(f"[RETENTION] backup failed: {e}")
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
        return {"ok": False, "error": str(e)[:200]}

    # Verify it before anything is deleted on the strength of it.
    try:
        check = sqlite3.connect(out)
        try:
            verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
            rows = check.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        finally:
            check.close()
    except Exception as e:
        verdict, rows = f"unreadable: {e}", 0
    size = os.path.getsize(out) if os.path.exists(out) else 0
    if verdict != "ok":
        logger.warning(f"[RETENTION] backup {os.path.basename(out)} failed its integrity check: {verdict}")
        return {"ok": False, "error": verdict, "path": out}
    logger.info(f"[RETENTION] backed up to {os.path.basename(out)} ({size / 1e6:.1f} MB, {rows} turns)")
    return {"ok": True, "path": out, "mb": round(size / 1e6, 2), "turns": rows}


def newest_backup() -> dict:
    """The most recent backup and its age in hours; {} when there is none."""
    try:
        files = [f for f in glob.glob(os.path.join(BACKUP_DIR, "*.db"))]
    except OSError:
        files = []
    if not files:
        return {}
    newest = max(files, key=os.path.getmtime)
    return {"path": newest, "name": os.path.basename(newest),
            "hours": (time.time() - os.path.getmtime(newest)) / 3600.0,
            "mb": round(os.path.getsize(newest) / 1e6, 2),
            "count": len(files)}


async def prune() -> dict:
    from db.db import retention_prune, set_retention_summary
    t0 = time.time()
    # 2026-09-28: the save comes FIRST, so the one process that deletes her
    # history can never delete anything that was not just written to disk.
    backup = backup_database("daily")
    if not backup.get("ok"):
        logger.warning("[RETENTION] skipping the prune: nothing was deleted because the backup did not succeed")
        summary = {"at": time.time(), "seconds": round(time.time() - t0, 2),
                   "removed": {}, "backup": backup, "skipped": "the backup failed"}
        try:
            await set_retention_summary(summary)
        except Exception:
            pass
        return summary
    try:
        counts = await retention_prune(POLICY)
    except Exception as e:
        logger.warning(f"[RETENTION] database pass failed: {e}")
        counts = {"error": str(e)[:200]}
    counts["backups_removed"] = _prune_backups(POLICY["backups_keep"])
    counts["ollama_logs_removed"] = _prune_ollama_logs(POLICY["ollama_logs_keep"])
    summary = {"at": time.time(), "seconds": round(time.time() - t0, 2), "removed": counts,
               "backup": backup}
    try:
        await set_retention_summary(summary)
    except Exception as e:
        logger.warning(f"[RETENTION] could not store the summary: {e}")
    removed = {k: v for k, v in counts.items() if v}
    logger.info(f"[RETENTION] {removed if removed else 'nothing to remove'}")
    return summary
