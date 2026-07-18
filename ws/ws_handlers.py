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
from config.logger_config import logger
from core.alex_core import alex_core
from db.db import (
    get_user_role, fetch_voice_samples,
    fetch_undelivered_curiosity_questions, mark_curiosity_questions_delivered
)


generation_lock = asyncio.Lock()

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

    delivered = False

    for session_id, conn in list(_active_connections.items()):
        if conn.get("role") != "creator":
            continue

        session = alex_core.get_session(session_id)
        if not session.get("creator_verified"):
            continue

        websocket = conn["websocket"]

        try:
            await websocket.send_text("__START__")
            await websocket.send_text(text)

            if speak:
                pcm = await synthesize_speech(text)
                if pcm:
                    await websocket.send_bytes(pcm)

            await websocket.send_text("__END__")
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
            user_id = await identity_manager.onboard_new_user(
                websocket,
                user_id,
                session
            )

        # send profile
        facts = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(facts))

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
        _active_connections[session_id] = {"websocket": websocket, "role": role}

        if role in ("creator", "super_user") and not session.get("creator_verified"):
            # not already verified above (voice-first recognition during
            # resolution/onboarding already counts — no need to ask twice)
            enrolled = await fetch_voice_samples(user_id)

            if not enrolled:
                # first time this profile has connected — bootstrap voice enrollment
                collected = await identity_manager.enroll_voice(websocket, user_id)
                session["creator_verified"] = collected > 0

                if session["creator_verified"]:
                    await send_debug(websocket, f"✅ Voice enrolled ({collected} sample(s)) — verified for this session.")
                else:
                    await send_debug(websocket, "⚠️ Voice enrollment failed — privileged actions unavailable this session.")
            else:
                matched, score, heard_text = await identity_manager.verify_voice(websocket, user_id)
                session["creator_verified"] = matched

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
                    curiosity_summary = (
                        f"🤔 While you were away, I noticed I don't really know about "
                        f"{q['topic']}. {q['question']}"
                    )

                    logger.info(f"[ACTION] Delivered curiosity question: {q['question']}")

                    await websocket.send_text("__START__")
                    await websocket.send_text(curiosity_summary)
                    await websocket.send_text("__END__")

                    await mark_curiosity_questions_delivered()

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
            prompt_text = await audio.process_end(audio_bytes, websocket, send_debug)
            if not prompt_text:
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

            addressed = bool(WAKE_WORD_RE.search(prompt_text))
            in_window = (now - last_addressed) < CONVERSATION_WINDOW_S

            if not (addressed or in_window):
                await send_debug(websocket, f"🙉 Not addressed, ignored: {prompt_text!r}")
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
        await handle_chat(websocket, msg, user_id, session_id)

        print("🔥 HANDLE_CHAT RETURNED")

    except Exception as e:
        print("💥 process_message error:", e)


# -------------------------
# REGISTER
# -------------------------
def register_ws(app):
    app.websocket("/ws")(ws_text)