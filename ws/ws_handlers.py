import asyncio
import uuid
import json
import re
import time

from fastapi import WebSocket, WebSocketDisconnect

from ws.ws_utils import enrich_profile, send_debug
from ws.ws_audio import AudioProcessor

from ws.ws_chat import handle_chat
from llm.ollama_client import locked_fields
from identity.identity_manager import identity_manager
from core import readiness
from core.voice import say
from db.db import remember_own_utterance, session_opened, session_verified, session_heard, session_closed
from core import idle_author
from config.logger_config import logger
from core.alex_core import alex_core
from db.db import (
    get_user_role, fetch_voice_samples,
    fetch_undelivered_curiosity_questions, mark_curiosity_question_asked
)


# 2026-09-20: one lock for the whole process, and reentrant per task.
# It used to be a plain asyncio.Lock here, which is why two of the four
# speaking paths skipped it — the clarification path runs INSIDE it
# (process_message holds it and calls down into AudioProcessor), so it
# could not take it without deadlocking on itself. See core/voice.py.
from core.voice import speech_lock as generation_lock

MIN_AUDIO_BYTES = 6000
SPEECH_DEBOUNCE = 1.8
CONFIRM_TIMEOUT = 30  # seconds

# 2026-07-18 (Craig: "would she ever ask something like 'are you still
# there?'... I've also to this day never received an unprompted
# question" / "if she's working on something in the background... would
# it show that?") — everything before this was request->response only;
# nothing could push a message into a session that was already open and
# just sitting idle. Curiosity questions (core/self_reflection.py) only
# ever got delivered inside the connect-time handshake below, so a
# session that stays open continuously (likely, given auto-listen) could
# sit on a queued question indefinitely. This registry is what makes a
# real push possible: session_id -> the live websocket + role, kept in
# sync with actual connect/disconnect (see ws_text()'s try/finally).
_active_connections = {}


async def push_to_creator(text: str, speak: bool = True) -> bool:
    """Sends `text` into every currently-connected, voice-verified creator
    session, unprompted — the actual mechanism behind proactive curiosity
    delivery and the idle check-in (see main.py's periodic_proactive_check()).
    Reuses the exact __START__/text/__END__ envelope a normal reply
    already uses, so the browser needs no new protocol to render it.

    Also refreshes last_addressed_at the same way a normal response does
    (core/response_handler.py) — she just said something unprompted, so a
    reply without repeating the wake word should still land, same as any
    other turn she just spoke in.

    Returns True if at least one live session received it, False if
    nobody's actually connected right now (nothing to push to — not an
    error, just means it'll have to wait for the next connect, same as
    before this existed)."""
    from core.alex_core import alex_core
    from speech.tts_engine import synthesize_speech


    return await _push_now(text, speak, alex_core, synthesize_speech)


async def _push_now(text, speak, alex_core, synthesize_speech) -> bool:
    delivered = False

    for session_id, conn in list(_active_connections.items()):
        if conn.get("role") != "creator":
            continue

        session = alex_core.get_session(session_id)
        if not session.get("creator_verified"):
            continue

        websocket = conn["websocket"]

        try:
            # core/voice.say() owns the lock, the envelope and the memory
            # record. This path used to do two of those and forget the
            # third, which is how she came to ask a question and then not
            # know she had asked it.
            if not await say(websocket, text, user_id=conn.get("user_id"),
                             speak=speak):
                continue

            session["last_addressed_at"] = time.time()
            # Keeps the client's "engaged" indicator honest — without
            # this, the server would correctly accept an unaddressed
            # follow-up reply (last_addressed_at is real), but the UI
            # would still show "engaged: no" since it never heard about
            # this push at all.
            await websocket.send_text("__ENGAGED__1")
            delivered = True

        except Exception as e:
            logger.warning(f"⚠️ push_to_creator failed for session {session_id}: {e}")

    return delivered


def get_active_creator_session_ids():
    """Session IDs of currently-connected role='creator' connections —
    used by core/proactive.py to decide which sessions might be worth an
    idle check-in, without reaching into _active_connections directly.
    Not filtered by creator_verified here (push_to_creator() re-checks
    that fresh at send time) — this only answers "is anyone claiming to
    be the creator connected right now"."""
    return [sid for sid, conn in _active_connections.items() if conn.get("role") == "creator"]


async def broadcast_signal(raw_text: str):
    """Like send_signal_to_creator, but to EVERY connected session.

    2026-09-20, for startup readiness: whether the model is still loading is
    not creator-scoped information. Anyone who connects during the load sees
    the same silence and needs the same explanation."""
    for session_id, conn in list(_active_connections.items()):
        try:
            await conn["websocket"].send_text(raw_text)
        except Exception as e:
            logger.warning(f"⚠️ broadcast_signal failed for session {session_id}: {e}")


async def send_signal_to_creator(raw_text: str):
    """A raw status ping to every connected creator session — NOT routed
    through the chat display/speech envelope push_to_creator() uses.
    Behind core/self_reflection.py's __SELFWORK__0/1 (Craig: "if she's
    working on something in the background... would it show that?"),
    same pattern __ENGAGED__/__MOOD__ already use for other UI state that
    isn't part of the conversation transcript itself."""
    for session_id, conn in list(_active_connections.items()):
        if conn.get("role") != "creator":
            continue
        try:
            await conn["websocket"].send_text(raw_text)
        except Exception as e:
            logger.warning(f"⚠️ send_signal_to_creator failed for session {session_id}: {e}")


# 2026-07-17 (Craig: wants continuous listening without "generic
# background chaos" — nothing today distinguishes speech directed at her
# from speech that just happened near the mic). Word-boundary match, not
# a bare substring — "alex" alone would also match inside "Alexander"
# without \b. Deliberately just her name, not a fixed "hey alex"/"ok
# alex" phrase list — those already contain "alex" as a whole word, so
# the plain word-boundary check already catches them for free without
# needing to enumerate variants.
WAKE_WORD_RE = re.compile(r"\balex\b", re.IGNORECASE)

# How long an active conversation stays "addressed" after the last
# qualifying utterance before she tunes back out and requires the wake
# word again — Craig: saying "Alex" before every single sentence in a
# real back-and-forth would get annoying fast. Sliding window, not
# fixed-length: every qualifying utterance (the wake word, or one said
# inside an already-open window) pushes it back out again, so a real
# conversation with normal pauses doesn't get cut off mid-thought.
# Starting point, not tuned — can't be verified without live use.
CONVERSATION_WINDOW_S = 45

# 2026-07-17 (Craig: "she continues to respond to obvious end of
# discussion statements") — an unaddressed closing remark ("that's all
# for now") still passes the in-window check above and gets a real
# reply, and core/response_handler.py refreshes last_addressed_at again
# once that reply finishes (added earlier tonight so a long response
# doesn't eat the window before Craig can reply) — the two combined mean
# a conversation that's actually over never lets the window expire on
# its own, as long as anything keeps getting said within it. Deterministic
# phrase list, same convention as systems/command/system.py's trigger
# lists (kept deterministic there for the same reliability-over-cost
# reason) rather than a per-turn classifier call.
# "Stop listening" is a direct, explicit command — checked against the
# REAL transcript log (db/memory.db) and confirmed Craig actually says
# this exact phrase, twice, and it did nothing (still got a chatty
# reply, still stayed "in conversation" afterward). Unlike the softer
# phrases below, this one closes the window regardless of where in the
# utterance it appears — there's no ambiguous, unrelated-topic reading
# of "stop listening" the way there is for e.g. "I'm done".
_STOP_LISTENING_RE = re.compile(r"\bstop listening\b|\bquit listening\b", re.IGNORECASE)

_END_OF_DISCUSSION_PHRASES_RE = re.compile(
    r"\b(that'?s (all|it|enough)|that is (all|it|enough)|that'?ll be all|"
    r"we'?re done|i'?m done|never ?mind|good ?bye|good ?night|"
    r"talk (to you )?later|catch you later)\b",
    re.IGNORECASE
)


def _is_closing_remark(text: str) -> bool:
    if _STOP_LISTENING_RE.search(text):
        return True

    match = _END_OF_DISCUSSION_PHRASES_RE.search(text)
    if not match:
        return False
    # Only counts if the phrase is near the end of what was actually
    # said — trailing filler ("...for now") is fine, but this avoids
    # matching mid-sentence about something unrelated (e.g. "I'm done
    # with this task, keep going on the next one").
    return len(text[match.end():].strip()) <= 15


def is_unlocked(user_id):
    state = locked_fields.get(user_id)
    return not (state and state.get("all"))


# -------------------------
# MAIN WS ENTRY
# -------------------------
async def ws_text(websocket: WebSocket):

    try:
        await websocket.accept()
    except:
        return

    session_id = str(uuid.uuid4())
    logger.info(f"🟢 WS connected: {session_id}")
    await send_debug(websocket, f"🟢 Connected: {session_id}")

    user_id = None
    audio = AudioProcessor()

    try:
        # -------------------------
        # INITIAL HANDSHAKE
        # -------------------------
        first_message = None

        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                logger.info(f"🔴 WS disconnected: {session_id}")
                return

            # raw receive() doesn't always raise WebSocketDisconnect — it can
            # just return this dict. Calling receive() again after that raises
            # RuntimeError, so bail out cleanly here instead.
            if msg.get("type") == "websocket.disconnect":
                logger.info(f"🔴 WS disconnected: {session_id}")
                return

            if "text" in msg and msg["text"]:
                text = msg["text"]

                if text.startswith("__"):
                    # the user started talking before any handshake text
                    # arrived (e.g. "__END_AUDIO__"). This loop only knows
                    # how to check for a JSON auto-login handshake — it
                    # can't transcribe anything. Stop waiting here and let
                    # onboarding take over properly with its own prompt,
                    # which *does* buffer and transcribe real speech.
                    break

                first_message = text
                break

            # raw audio bytes arriving before any handshake text — same
            # reasoning: don't keep waiting, hand off to onboarding now.
            break

        claimed_name = None

        if first_message and first_message.strip().startswith("{"):
            try:
                data = json.loads(first_message)
                claimed_name = data.get("user_name")
            except:
                pass

        session = alex_core.get_session(session_id)

        user_id = await identity_manager.resolve_user_passive(
            claimed_name,
            session_id
        )

        # 🔒 default lock
        locked_fields[user_id] = {"all": True}

        if user_id.startswith("pending_user_"):
            user_id, onboard_heard_text = await identity_manager.onboard_new_user(
                websocket,
                user_id,
                session
            )

            # 2026-07-18 (Craig: "my first utterance which she used to
            # verify me is being eaten again") — the sibling of the
            # 2026-07-17 verify_voice() fix: onboard_new_user()'s own
            # voice-first recognition path had the exact same bug in a
            # path that fix never touched (hit whenever the browser's
            # cached username isn't recognized, same root cause as
            # before, routing here instead of the later separate
            # verification block). See identity_manager.py's docstring.
            clean = re.sub(r'[^a-z ]', '', onboard_heard_text.lower()).strip()
            if clean and clean not in {"now", "no now", "um", "uh", "okay", "ok", "hmm", "hm"}:
                async with generation_lock:
                    await process_message(websocket, onboard_heard_text, user_id, session_id, audio)

        # send profile
        facts = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(facts))

        # Current startup state, so a page that connects mid-load knows why
        # she is quiet instead of assuming she is ignoring it. Broadcasts
        # cover later transitions; this covers arriving late.
        await websocket.send_text(readiness.signal_text())

        # -------------------------
        # PRIVILEGED VERIFICATION (voice, once per session — creator AND
        # super_user both need to prove it's really them before any
        # privileged action; role alone is just a claimed identity)
        # -------------------------
        role = await get_user_role(user_id)

        # Registered here (not at connection start) since push_to_creator()
        # only ever targets a verified creator, and role is unknown until
        # now. Cleared in the finally block below regardless of how this
        # connection ends.
        # 2026-09-21: user_id included. push_to_creator() hands
        # conn.get("user_id") to core/voice.say(), which records what she
        # said only when it has one — and this dict never carried it, so
        # every question pushed mid-session was spoken and then forgotten.
        # Confirmed in the data: the two curiosity questions pushed
        # mid-session on 2026-09-21 have no memory row; the one delivered at
        # connect, which passes user_id explicitly, does. This was the last
        # empty cell in the table in core/voice.py.
        _active_connections[session_id] = {
            "websocket": websocket, "role": role, "user_id": user_id}

        # 2026-09-21: who this is, by name, for the Controller's People
        # view (db.sessions). Fails soft: a bookkeeping row must never cost
        # a connection.
        try:
            await session_opened(session_id, user_id, role)
            if session.get("creator_verified"):
                await session_verified(session_id, True)
        except Exception as e:
            logger.warning(f"⚠️ could not record session: {e}")
        idle_author.note_activity()

        if role in ("creator", "super_user") and not session.get("creator_verified"):
            # not already verified above (voice-first recognition during
            # resolution/onboarding already counts — no need to ask twice)
            enrolled = await fetch_voice_samples(user_id)

            if not enrolled:
                # first time this profile has connected — bootstrap voice enrollment
                collected = await identity_manager.enroll_voice(websocket, user_id)
                session["creator_verified"] = collected > 0
                try:
                    await session_verified(session_id, session["creator_verified"])
                except Exception:
                    pass

                if session["creator_verified"]:
                    await send_debug(websocket, f"✅ Voice enrolled ({collected} sample(s)) — verified for this session.")
                else:
                    await send_debug(websocket, "⚠️ Voice enrollment failed — privileged actions unavailable this session.")
            else:
                matched, score, heard_text = await identity_manager.verify_voice(websocket, user_id)
                session["creator_verified"] = matched
                try:
                    await session_verified(session_id, matched)
                except Exception:
                    pass

                if matched:
                    await send_debug(websocket, f"✅ Voice verified (score={score:.2f})")
                else:
                    await send_debug(websocket, f"⚠️ Voice did not match (score={score:.2f}) — privileged actions unavailable this session.")

                # 2026-07-17: found live — "she did not respond at all to
                # what I said, just verified." The audio captured for the
                # voice-match check used to be thrown away right after
                # embedding it — whatever Craig actually said during
                # verification never got answered, only judged as a
                # biometric sample. Real risk once auto-listen-on-join
                # started the mic immediately: talking naturally right as
                # the page loads looks identical to answering the
                # verification prompt, with no way to know which mode
                # you're in. identity_manager.verify_voice() now also
                # transcribes that same audio; route it through the real
                # pipeline here so nothing said gets silently dropped —
                # regardless of whether the match itself succeeded, since
                # what he said is independent of whether his voice matched.
                clean = re.sub(r'[^a-z ]', '', heard_text.lower()).strip()
                if clean and clean not in {"now", "no now", "um", "uh", "okay", "ok", "hmm", "hm"}:
                    async with generation_lock:
                        await process_message(websocket, heard_text, user_id, session_id, audio)

            # Briefings are creator-only — ALEX's own security/personality
            # oversight is the creator's business, not a super_user's.
            if role == "creator":
                # -------------------------
                # SECURITY EVENTS, PERSONALITY CHANGE LOG, PROACTIVE FAULT
                # CHECK — 2026-07-17, moved OUT of the live chat entirely
                # (Craig: "her showing me what she changed dismissed what
                # she said prior"). These used to fire as their own
                # __START__/text/__END__ sequences right here at connect —
                # each one pushes whatever was actually just said (a real
                # answer, possibly the one the verification fix above just
                # made possible) into "Previous Messages" and replaces it
                # with an administrative notice. That's a real, jarring
                # UX problem, not a misunderstanding — these are audit
                # information, not conversation, and don't belong
                # interleaved with it. Now surfaced in the Controller's
                # own Notifications tab instead (reads the same
                # fetch_unacknowledged_security_events()/
                # fetch_unacknowledged_personality_changes(), acknowledges
                # on a real action there instead of automatically here).
                # Curiosity questions below are deliberately NOT moved —
                # those are framed as her own genuine conversational
                # curiosity, not an audit report, so they stay part of
                # live chat.
                # -------------------------

                # -------------------------
                # CURIOSITY QUESTION (2026-07-16, informational — she's
                # just asking, not gated) — queued by core/self_reflection.py
                # when she notices a real knowledge gap during her hourly
                # pass. Only ever delivers one at a time; a queue of
                # unrelated questions dumped at connect would be noise,
                # not curiosity.
                # -------------------------
                questions = await fetch_undelivered_curiosity_questions()

                if questions:
                    q = questions[0]

                    # 2026-09-20: sent as she wrote it. The "While you were
                    # away, I noticed I don't really know about X." wrapper
                    # this used to carry was a hardcoded line of mine around
                    # a question of hers — see core/proactive.py for the same
                    # fix and the reasoning.
                    logger.info(f"[ACTION] Delivered curiosity question: {q['question']}")

                    # Through say() like everything else — this sent a bare
                    # envelope with no lock and no memory record, so she
                    # could open a session by asking something and then not
                    # know she had asked it.
                    await say(websocket, q["question"], user_id=user_id)

                    await mark_curiosity_question_asked(q["topic"])

                    # She asked something; whatever he says next is very
                    # likely the answer. See systems/llm/system.py for the
                    # capture and why it is positional.
                    alex_core.get_session(session_id)["awaiting_curiosity_answer"] = q["topic"]

        # -------------------------
        # MAIN LOOP
        # -------------------------
        while True:

            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                logger.info(f"🔴 WS disconnected: {session_id}")
                break
            except RuntimeError as e:
                logger.warning(f"⚠️ WS runtime error: {e}")
                break

            # -------------------------
            # AUDIO
            # -------------------------
            if message.get("bytes") is not None:
                audio.add_audio(message["bytes"])
                continue

            # -------------------------
            # TEXT
            # -------------------------
            msg = message.get("text")

            if msg is None:
                continue

            # 🎙️ Barge-in: user started talking, cut her off immediately
            # rather than waiting for the response pipeline. No-op if she
            # wasn't speaking.
            #
            # 2026-07-16: speech now plays through the BROWSER (Web Audio
            # API), not a server-side speaker — the browser silences its
            # own playback instantly the moment it sends this, with no
            # round-trip needed. All that's left to do server-side is stop
            # core/response_handler.py's _handle_stream() loop from
            # producing (and sending) any more chunks — this flag is what
            # tells it to stop (found live: without it, a longer response
            # just kept streaming right through an interrupt, since
            # nothing told the loop itself to stop).
            if msg == "__INTERRUPT__":
                alex_core.get_session(session_id)["interrupted"] = True
                continue

            # "__END_AUDIO__" is the one "__"-prefixed message the client
            # actually sends — it MUST reach process_message() below, which
            # is what triggers audio.process_end() to transcribe it. Every
            # other "__"-prefixed string is a genuine stray control signal.
            if msg.startswith("__") and msg != "__END_AUDIO__":
                continue

            # 🔥 CAPTURE VALUE (CRITICAL FIX)
            captured_msg = msg

            # Snapshot+clear the audio buffer HERE, synchronously, the
            # instant "__END_AUDIO__" arrives — not later, inside
            # process_message(), which only runs once generation_lock is
            # free. During a barge-in that can be several seconds away,
            # and the client's recorder restarts almost immediately for
            # the NEXT utterance; without taking this snapshot now, that
            # next utterance's audio was landing in the same buffer before
            # this one had been read. See AudioProcessor.take_buffer().
            captured_audio = audio.take_buffer() if captured_msg == "__END_AUDIO__" else None

            async def safe_process(local_msg, local_audio):
                async with generation_lock:
                    await process_message(
                        websocket,
                        local_msg,
                        user_id,
                        session_id,
                        audio,
                        local_audio
                    )

            asyncio.create_task(safe_process(captured_msg, captured_audio))

    except WebSocketDisconnect:
        logger.info(f"🔴 WS disconnected: {session_id}")

    finally:
        # Safe no-op if this session never got far enough to register
        # (e.g. disconnected mid-handshake).
        _active_connections.pop(session_id, None)

        # 2026-09-20: and the session dict behind it, which nothing had
        # ever removed. See alex_core.end_session() for the measurement and
        # for why the race with a still-streaming response is fine.
        alex_core.end_session(session_id)
        try:
            await session_closed(session_id)
        except Exception:
            pass


# -------------------------
# MESSAGE PROCESSOR
# -------------------------
async def process_message(websocket, msg, user_id, session_id, audio, audio_bytes=None):

    print("🔥 HANDLE_PROMPT ENTERED:", msg)

    try:
        # -------------------------
        # AUDIO FINALIZATION
        # -------------------------
        if msg == "__END_AUDIO__":
            prompt_text = await audio.process_end(
                audio_bytes, websocket, send_debug, user_id=user_id)
            if not prompt_text:
                # 2026-09-21: if she just asked "did you say X?", the
                # conversation is open — his answer must not need the wake
                # word, whatever the window said.
                if audio.pending_clarification is not None:
                    alex_core.get_session(session_id)["last_addressed_at"] = time.time()
                return

            # -------------------------
            # WAKE-WORD / ADDRESSEE GATE — voice input only, never
            # applied to typed text (typing directly to her is already
            # unambiguous). Requires her name OR being inside the active-
            # conversation window; otherwise this is background noise/a
            # conversation not meant for her, discarded before it ever
            # reaches a real system — no response, no fact/memory writes,
            # nothing stored.
            # -------------------------
            session = alex_core.get_session(session_id)
            now = time.time()
            last_addressed = session.get("last_addressed_at", 0)

            # An answer to her own clarification is addressed to her by
            # definition (see AudioProcessor.answered_clarification).
            answered_her = audio.answered_clarification
            audio.answered_clarification = False
            addressed = bool(WAKE_WORD_RE.search(prompt_text)) or answered_her
            in_window = (now - last_addressed) < CONVERSATION_WINDOW_S

            if not (addressed or in_window):
                await send_debug(websocket, f"🙉 Not addressed, ignored: {prompt_text!r}")

                # 2026-09-20: tell the PAGE, not just the debug panel.
                #
                # Craig, live: "she then ignored another thing I stated. and
                # seemed to continue to do so." The log shows exactly that —
                # three consecutive utterances dropped over two minutes
                # ("Okay, I'm turning your personality back on.", "No, I did
                # not.", "your personality should be restored.") until he
                # happened to say her name again.
                #
                # The gating itself is correct and deliberate: the window had
                # genuinely lapsed (59s against CONVERSATION_WINDOW_S=45). The
                # failure is that it is INVISIBLE. `send_debug` goes to a
                # collapsible debug panel, and the "engaged: no" telemetry row
                # lives in a rail that can be collapsed entirely. So from the
                # outside, correct behaviour and a hung assistant look
                # identical — and the natural response is to keep talking,
                # which never re-engages her.
                await websocket.send_text("__UNADDRESSED__" + prompt_text)
                # 2026-07-18 (Craig: "her presence in the UI still says
                # listening" after "stop listening" worked server-side) —
                # the mic staying armed (continuous VAD, needed to catch
                # the next wake word) and actually being addressed/engaged
                # are two different things the UI used to conflate into
                # one "listening" indicator. This tells the browser the
                # true engaged state the instant something gets silently
                # dropped, not just after the next reply.
                await websocket.send_text("__ENGAGED__0")
                return

            # A closing remark still gets a real reply (it's already
            # addressed/in-window), but the window closes right now
            # instead of extending — response_handler.py checks this same
            # flag and skips its own end-of-turn refresh, so the reply
            # itself can't re-open what Craig just closed.
            closing = _is_closing_remark(prompt_text)
            session["last_addressed_at"] = 0 if closing else now
            session["conversation_closing"] = closing
            await websocket.send_text("__ENGAGED__0" if closing else "__ENGAGED__1")
            msg = prompt_text

        # -------------------------
        # NORMALIZATION
        # -------------------------
        normalized = msg.strip().lower()
        normalized_clean = re.sub(r'[^a-z]', '', normalized)

        _ = normalized_clean.startswith((
            "yes", "y", "yeah", "yep", "confirm",
            "no", "n", "nope", "nah"
        ))

        print("🔥 ROUTING TO HANDLE_CHAT")

        # -------------------------
        # CORE PIPELINE
        # -------------------------
        try:
            await session_heard(session_id)
        except Exception:
            pass
        idle_author.note_activity()
        await handle_chat(websocket, msg, user_id, session_id)

        print("🔥 HANDLE_CHAT RETURNED")

    except Exception as e:
        print("💥 process_message error:", e)


# -------------------------
# REGISTER
# -------------------------
def register_ws(app):
    app.websocket("/ws")(ws_text)