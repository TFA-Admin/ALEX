# core/proactive.py

"""
Proactive/unprompted messaging.

2026-07-18 (Craig: "would she ever ask something like 'are you still
there?'... I've also to this day never received an unprompted question"
/ "if she's working on something in the background like tuning herself
would it show that?") — everything before this was request->response
only: nothing could push a message into a session that was already open
and just sitting idle. Curiosity questions (core/self_reflection.py)
only ever got delivered inside the connect-time handshake in
ws/ws_handlers.py, so a session that stays open continuously (likely,
given auto-listen-on-join) could sit on a queued question indefinitely.

This module decides WHEN there's something worth pushing; the actual
send is ws/ws_handlers.py's push_to_creator(), which reuses the same
__START__/text/__END__ envelope a normal reply already uses.
"""

import asyncio
import time

from db.db import fetch_undelivered_curiosity_questions, mark_curiosity_question_asked
from core.alex_core import alex_core
from ws.ws_handlers import push_to_creator, get_active_creator_session_ids
from config.logger_config import logger

PROACTIVE_CHECK_INTERVAL_S = 60

# Starting point, not tuned — how long a creator session can sit
# connected with no real addressed exchange before she checks in. Craig
# floated this as a hypothetical ("would she ever ask if I'm still
# there") rather than a firm spec, so this is a reasoned first guess, not
# something measured against real usage yet.
IDLE_CHECKIN_THRESHOLD_S = 900

# How long the room has to be quiet before she raises a question of her
# own mid-session. Starting point; the failure it prevents was measured
# at 2s (2026-09-21 19:52:00, right after a 10s turn).
CURIOSITY_QUIET_S = 120


async def _check_curiosity_delivery():
    """Connect-time delivery (ws_handlers.py) already covers "just
    reconnected" — this covers "been connected the whole time and a
    question got queued since." Skips the DB read entirely when nobody's
    even connected, since connect-time delivery will handle it whenever
    they do."""
    if not get_active_creator_session_ids():
        return

    # 2026-09-21 (Craig: "she jump over herself again"): this fired two
    # seconds after she finished a reply, on the heels of her own voice,
    # and his next words were taken as the answer and answered again. An
    # unprompted question belongs in a lull. Nothing for CURIOSITY_QUIET_S
    # since anyone last spoke to her, and she is not speaking now.
    from core import idle_author
    from core.voice import speech_lock
    if idle_author.idle_for() < CURIOSITY_QUIET_S or speech_lock.locked():
        return

    # 2026-09-23: his questions only — the mid-session push reaches the
    # creator's sessions, so it must never carry a question raised by
    # somebody else's conversation.
    from core.self_reflection import get_creator_name
    questions = await fetch_undelivered_curiosity_questions(user=await get_creator_name(), creator=True)
    if not questions:
        return

    q = questions[0]

    # 2026-09-20: the "While you were away, I noticed I don't really know
    # about X." preamble that used to wrap this is gone. It was a hardcoded
    # English sentence of mine bolted onto a question of hers — exactly the
    # thing Craig objected to when a diagnostic did the same ("This is an
    # advisory not something hard coded into her though correct?"), and he
    # described the result here as a "disconnect". The question in
    # curiosity_queue was written by her during reflection; it is already
    # in her own voice and stands on its own. An unprompted question IS
    # abrupt — that is what makes it unprompted.
    delivered = await push_to_creator(q["question"])
    if delivered:
        # Same as the connect-time path: the next thing he says is
        # probably the answer.
        for session_id in get_active_creator_session_ids():
            s = alex_core.get_session(session_id)
            s["awaiting_curiosity_answer"] = q["topic"]
            # 2026-09-25: what he says before she has finished asking is
            # not the answer (see ws/ws_handlers._ask_when_quiet)
            s["curiosity_asked_until"] = time.time() + 1.0 + 0.45 * len(q["question"].split())

        logger.info(f"[ACTION] Proactively delivered curiosity question mid-session: {q['question']}")
        await mark_curiosity_question_asked(q["topic"])


async def _check_idle_checkin():
    now = time.time()

    for session_id in get_active_creator_session_ids():
        session = alex_core.get_session(session_id)

        last_addressed = session.get("last_addressed_at", 0)
        if last_addressed <= 0:
            # Never actually had a real addressed exchange yet this
            # session — nothing to be "idle" relative to, and checking
            # in on someone who's never said a word yet would be odd.
            continue

        if now - last_addressed < IDLE_CHECKIN_THRESHOLD_S:
            continue

        last_checkin = session.get("last_checkin_at", 0)
        if now - last_checkin < IDLE_CHECKIN_THRESHOLD_S:
            # Already checked in for this same idle stretch — don't nag
            # every single poll interval once triggered.
            continue

        session["last_checkin_at"] = now
        sent = await push_to_creator("Still there? Just checking in.")
        if sent:
            logger.info(f"[ACTION] Sent idle check-in for session {session_id}")


async def periodic_proactive_check():
    while True:
        await asyncio.sleep(PROACTIVE_CHECK_INTERVAL_S)
        try:
            await _check_curiosity_delivery()
            await _check_idle_checkin()
        except Exception as e:
            logger.exception(f"❌ Proactive check failed: {e}")
