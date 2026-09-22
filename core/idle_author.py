# core/idle_author.py
"""
Her author, on her own time.

2026-09-21, Craig: "Can we give her access to the larger model whenever
she is not being used directly but still active. Like right now for
instance, I'm not interacting with her but she's on."

This loop runs in her process. When nobody has spoken to her for
IDLE_AFTER_S and nothing is generating, it picks the whitelisted target
she has looked at least recently (core/self_author.WHITELIST), asks the
author model for a proposal, and writes ONE row to `proposals` with
status 'authored': target, value, rationale. That is the whole of what
this loop can do. The Controller (controller/versions.py, protected)
turns the row into a branch and a worktree, and Craig decides.

The author model is ALEX_AUTHOR_MODEL if set, otherwise her own model
with thinking switched ON — the mode that was measured at 20-30s a turn
and ruled out for conversation is exactly right for work nobody is
waiting on. A larger model (14B-32B with CPU offload, the recorded
decision) drops in through that same variable once one is pulled; with
one model slot, using it means her conversational model is evicted and
reloads (~5-10s) on the first thing anyone says afterwards.

Interruption: ws/ws_handlers.py calls note_activity() when anyone
connects or speaks. That cancels a proposal in progress at once — the
HTTP request to Ollama is dropped, and Ollama stops generating — so the
cost of an idle job to a person arriving is the reload, never a wait for
the job to finish.

Guards, the same as the module-proposal loop's: one open proposal at a
time (requested/authored/proposed/gated), one proposal per target per
COOLDOWN_DAYS, and the switch `idle_author` in
config/controller_settings.json (Run view), read every cycle so it
applies without a restart.
"""
import os
import json
import time
import asyncio
from datetime import datetime, timezone, timedelta

from config.logger_config import logger

IDLE_AFTER_S = int(os.getenv("ALEX_IDLE_AUTHOR_AFTER_S", str(15 * 60)))
CHECK_EVERY_S = 60
COOLDOWN_DAYS = 7
OPEN_STATUSES = ("requested", "authored", "proposed", "gated")

_ALEX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS = os.path.join(_ALEX_DIR, "config", "controller_settings.json")

_last_activity = time.time()
_task = None            # the proposal in progress, if any
_busy = False
_attempted = {}         # target -> time of the last attempt that left no row
RETRY_AFTER_S = 30 * 60


def note_activity():
    """Anyone connecting or speaking. Cancels a proposal in progress."""
    global _last_activity
    _last_activity = time.time()
    if _task is not None and not _task.done():
        _task.cancel()
        logger.info("[IDLE AUTHOR] interrupted — someone is here")


def idle_for() -> float:
    return time.time() - _last_activity


def enabled() -> bool:
    env = os.getenv("ALEX_IDLE_AUTHOR")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    try:
        with open(_SETTINGS, encoding="utf-8") as fh:
            return bool(json.load(fh).get("idle_author", True))
    except (OSError, ValueError):
        return True


def author_model():
    """None means her own model, with thinking on (see propose()). The
    environment wins; otherwise `author_model` in
    config/controller_settings.json (Run view), read each time so a
    change applies without a restart."""
    env = os.getenv("ALEX_AUTHOR_MODEL")
    if env:
        return env
    try:
        with open(_SETTINGS, encoding="utf-8") as fh:
            value = (json.load(fh).get("author_model") or "").strip()
        return value or None
    except (OSError, ValueError):
        return None


async def _pick_target():
    """The whitelisted target she has proposed on least recently, skipping
    any inside its cooldown. None if an open proposal exists or nothing
    is due."""
    from core.self_author import WHITELIST
    from db.db import fetch_proposals

    rows = await fetch_proposals(limit=200)
    if any(r.get("status") in OPEN_STATUSES for r in rows):
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)
    last = {}
    for r in rows:
        t = r.get("target")
        if not t:
            continue
        try:
            when = datetime.strptime(str(r.get("created_at"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if t not in last or when > last[t]:
            last[t] = when
    now = time.time()
    due = [k for k in WHITELIST
           if (k not in last or last[k] < cutoff)
           and now - _attempted.get(k, 0) > RETRY_AFTER_S]
    if not due:
        return None
    due.sort(key=lambda k: last.get(k, datetime.min.replace(tzinfo=timezone.utc)))
    return due[0]


async def run_once(force_target: str = None) -> str:
    """One attempt. Returns a short line saying what happened (logged by
    the loop; also handy from a test)."""
    from core import self_author
    from db.db import create_proposal, update_proposal, record_decision

    target = force_target or await _pick_target()
    if not target:
        return "nothing due (an open proposal exists, or every target is in cooldown)"

    reason = ("Idle time. Look at this setting against your scores and what he has said; "
              "propose a change only if you can say what it will do and why that is better.")
    result = await self_author.propose(target, reason, model=author_model(),
                                       think=None if author_model() else True)
    if not result.get("ok"):
        error = result.get("error", "")
        # Her own "no change" or a self-contradiction she was refused on is
        # a real look at the target: it goes in as a 'declined' row so the
        # target rests for COOLDOWN_DAYS like a proposal would (found on
        # 2026-09-21 before it ran: without this she would re-think the
        # same setting every minute). A failure to answer at all is not a
        # look; the target is retried after RETRY_AFTER_S.
        looked = error.startswith("she proposes no change") or error.startswith("refused")
        if looked:
            pid = await create_proposal(f"(looked, no change) {target}", error, "alex",
                                        target=target, status="declined")
            outcome = f"no change proposed; {target} rests for {COOLDOWN_DAYS} days"
            ref = f"proposals#{pid}"
        else:
            _attempted[target] = time.time()
            outcome = f"no answer; {target} is retried in {RETRY_AFTER_S // 60} minutes"
            ref = None
        await record_decision(
            "proposal", f"While idle she looked at {target} and proposed nothing",
            reasoning=error, evidence=f"model={author_model() or 'hers, thinking on'}",
            outcome=outcome, actor="alex", ref=ref)
        return f"{target}: {error[:160]}"

    title = f"{target}: {result['current']} -> {result['value']}"
    pid = await create_proposal(title, result.get("rationale", ""), "alex",
                                target=target, status="authored")
    await update_proposal(pid, value=str(result["value"]))
    await record_decision(
        "proposal", f"While idle she proposed #{pid}: {title}",
        reasoning=result.get("rationale", ""),
        evidence=f"effect she expects: {result.get('effect') or 'n/a'}; model={author_model() or 'hers, thinking on'}",
        outcome="waiting for the Controller to build it and for him to decide",
        actor="alex", ref=f"proposals#{pid}")
    return f"proposed #{pid}: {title}"


async def run():
    """The loop main.py starts. Never raises."""
    global _task, _busy
    logger.info(f"[IDLE AUTHOR] armed: after {IDLE_AFTER_S // 60} min with nobody speaking, "
                f"one target per pass, one open proposal at a time"
                + ("" if enabled() else " — switched OFF in the Controller"))
    await asyncio.sleep(120)          # stay out of the way of the post-restart minutes
    while True:
        try:
            if enabled() and idle_for() >= IDLE_AFTER_S and not _busy:
                from core.voice import speech_lock
                speaking = getattr(speech_lock, "locked", lambda: False)()
                if not speaking:
                    _busy = True
                    _task = asyncio.create_task(run_once())
                    try:
                        outcome = await _task
                        logger.info(f"[IDLE AUTHOR] {outcome}")
                    except asyncio.CancelledError:
                        pass
                    finally:
                        _task = None
                        _busy = False
        except Exception as e:
            logger.warning(f"[IDLE AUTHOR] failed: {e}")
            _busy = False
        await asyncio.sleep(CHECK_EVERY_S)
