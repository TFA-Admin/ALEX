import os

import httpx

from core.alex_core import alex_core
from db.db import (
    fetch_recent_memory_all, list_module_registry, get_response_timings,
    get_personality, get_last_personality_log_value
)
from module_runtime.module_loader import load_module

OLLAMA_HOST = "http://127.0.0.1:11434"

# 2026-07-16: found live — orphaned Ollama runner processes silently ate
# nearly all the GPU's VRAM, and every turn just hung with no exception
# to catch, no error to log — the existing "ollama unreachable" check
# below couldn't see it (Ollama was reachable, just starved). Comparing
# her own recorded turn times against her own recent history is the one
# way she can notice this class of problem herself. Needs a real sample
# to compare against (too few turns and one slightly-slow response looks
# like an anomaly when it isn't), and both a relative AND an absolute
# floor (a baseline that's already slow shouldn't need to get almost-
# recursively worse to trip; a fast baseline shouldn't trip on ordinary
# jitter) — neither number is tuned yet, both are reasoned starting
# points from tonight's real incident (healthy turns ~10-15s, the actual
# hang was 82s).
RESPONSE_TIMING_MIN_SAMPLES = 6
RESPONSE_TIMING_MULTIPLIER = 2.5
RESPONSE_TIMING_ABSOLUTE_FLOOR = 25.0


def _discover_system_names():
    """Every systems/*/system.py folder, scanned from disk rather than
    a hardcoded list — a newly-added system is automatically expected
    here without ever editing this file. See diagnose() convention
    below for the deeper, per-system health check."""
    root = "systems"
    if not os.path.isdir(root):
        return []
    return [
        entry for entry in os.listdir(root)
        if os.path.isfile(os.path.join(root, entry, "system.py"))
    ]


async def _call_diagnose(target):
    """Runs a system's/module's own diagnose() if it implements one —
    a real, self-reported functional check ("did my actual logic just
    work"), not just "is it present/loaded." Returns (ok, message), or
    None if this target hasn't opted into the convention — absence is
    not treated as a failure, just as "no extra scrutiny available yet."
    """
    if not hasattr(target, "diagnose"):
        return None

    try:
        result = target.diagnose()
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, tuple) and len(result) == 2:
            return result
        return (bool(result), "")
    except Exception as e:
        return (False, f"diagnose() raised: {e}")


def init():
    return "diagnostic_tool module ready — checks real system state"


def diagnose():
    """Self-check for the aggregator itself: confirms its own core
    mechanism (scanning systems/ from disk) actually sees something,
    rather than silently returning an empty list and reporting a
    false all-clear."""
    if not _discover_system_names():
        return False, "system discovery found zero systems — check the systems/ path"
    return True, ""


async def handle(command, state, user_id=None):
    if state is None:
        state = {}

    cmd = command.lower().strip()

    if "start" in cmd or "status" in cmd or "diagnostic" in cmd or "check" in cmd:
        issues = []

        # --- systems: existence discovered from disk; health from each
        # system's own diagnose() when it implements one, otherwise the
        # baseline "is it actually loaded" check ---
        loaded = alex_core.systems.systems
        for name in _discover_system_names():
            if name not in loaded:
                issues.append(f"system '{name}' not loaded")
                continue

            outcome = await _call_diagnose(loaded[name])
            if outcome is not None:
                ok, message = outcome
                if not ok:
                    issues.append(f"system '{name}': {message or 'self-check failed'}")

        # --- modules: existence from the real registry; health from
        # each module's own diagnose() when it implements one ---
        for entry in await list_module_registry():
            if entry["status"] != "enabled":
                continue

            mod = await load_module(entry["name"])
            if mod is None:
                issues.append(f"module '{entry['name']}' failed to load")
                continue

            outcome = await _call_diagnose(mod)
            if outcome is not None:
                ok, message = outcome
                if not ok:
                    issues.append(f"module '{entry['name']}': {message or 'self-check failed'}")

        # --- ollama ---
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(OLLAMA_HOST)
                ollama_ok = r.status_code == 200
        except Exception:
            ollama_ok = False
        if not ollama_ok:
            issues.append("ollama unreachable")

        # --- database ---
        try:
            await fetch_recent_memory_all(limit=1)
            db_ok = True
        except Exception:
            db_ok = False
        if not db_ok:
            issues.append("database unreachable")

        # --- response time vs her own recent history ---
        timings = await get_response_timings()
        if len(timings) >= RESPONSE_TIMING_MIN_SAMPLES:
            latest = timings[-1]
            baseline = sum(timings[:-1]) / len(timings[:-1])
            if latest > baseline * RESPONSE_TIMING_MULTIPLIER and latest > RESPONSE_TIMING_ABSOLUTE_FLOOR:
                issues.append(
                    f"response time degraded: last turn took {latest:.1f}s vs a "
                    f"recent average of {baseline:.1f}s — possible resource "
                    f"exhaustion (e.g. orphaned Ollama processes holding GPU "
                    f"memory)"
                )

        # --- does her actual behavior match her intended design? ---
        # 2026-07-17 (Craig: "can we make it so she could scan herself and
        # check for discrepancies"): a real, deterministic version of that
        # — not a semantic "is she staying in character" judgment call,
        # which would hit the same small-model reliability wall every
        # classifier in this project has hit. Every personality change
        # writes to BOTH personality_log (the audit trail) and the live
        # personality_description in the same logical operation
        # (db.set_personality() + db.log_personality_change(), called
        # together everywhere this happens); if those two ever disagree,
        # one of those writes silently failed partway and her actual
        # behavior is running on a value that doesn't match what the log
        # — and Craig, reading it — believes is current.
        last_logged = await get_last_personality_log_value()
        current = await get_personality()
        if last_logged is not None and last_logged != current:
            issues.append(
                "personality drift: the last logged personality change doesn't "
                "match what's actually stored — a write likely failed partway "
                f"(logged: {last_logged!r}, actual: {current!r})"
            )

        if not issues:
            return "All core systems online. Ollama and the database are both reachable.", state

        report = "Diagnostic found a problem:\n" + "\n".join(f"- {i}" for i in issues)
        return report, state

    return "Ask me to check systems or run a diagnostic.", state


def help():
    return ("Runs a real diagnostic check — discovers every system and module "
            "that actually exists, runs each one's own self-check when it has "
            "one, and gives a single clean sign-off if everything's healthy or "
            "an itemized list of exactly what's wrong if it isn't.")
