import asyncio
import uuid
import json
import re

from fastapi import WebSocket, WebSocketDisconnect

from ws.ws_utils import enrich_profile, send_debug
from ws.ws_audio import AudioProcessor

from ws.ws_chat import handle_chat
from llm.ollama_client import locked_fields
from identity.identity_manager import identity_manager
from config.logger_config import logger
from speech.tts_engine import audio_level, stop_speaking
from core.alex_core import alex_core
from db.db import (
    get_user_role, fetch_unacknowledged_security_events, acknowledge_security_events,
    fetch_voice_samples, fetch_unacknowledged_personality_changes, acknowledge_personality_changes
)


generation_lock = asyncio.Lock()

MIN_AUDIO_BYTES = 6000
SPEECH_DEBOUNCE = 1.8
CONFIRM_TIMEOUT = 30  # seconds


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

    # -------------------------
    # AUDIO LEVEL STREAM
    # -------------------------
    async def send_audio_level():
        while True:
            try:
                await websocket.send_text(f"__AUDIO__{audio_level}")
                await asyncio.sleep(0.05)
            except:
                break

    asyncio.create_task(send_audio_level())

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
                matched, score = await identity_manager.verify_voice(websocket, user_id)
                session["creator_verified"] = matched

                if matched:
                    await send_debug(websocket, f"✅ Voice verified (score={score:.2f})")
                else:
                    await send_debug(websocket, f"⚠️ Voice did not match (score={score:.2f}) — privileged actions unavailable this session.")

            # Briefings are creator-only — ALEX's own security/personality
            # oversight is the creator's business, not a super_user's.
            if role == "creator":
                # -------------------------
                # CREATOR SECURITY BRIEFING
                # -------------------------
                events = await fetch_unacknowledged_security_events()

                if events:
                    lines = [
                        f"- {e['created_at']}: {e['user']} → {e['detail']}"
                        for e in events
                    ]
                    summary = (
                        f"⚠️ {len(events)} module build attempt(s) were blocked by "
                        f"the sandbox since you last checked:\n" + "\n".join(lines)
                    )

                    logger.info(summary)

                    await websocket.send_text("__START__")
                    await websocket.send_text(summary)
                    await websocket.send_text("__END__")

                    await acknowledge_security_events()

                # -------------------------
                # PERSONALITY CHANGE BRIEFING (informational — not a gate)
                # -------------------------
                changes = await fetch_unacknowledged_personality_changes()

                if changes:
                    lines = [
                        f"- {c['created_at']} [{c['kind']}]: {c['new_value']} ({c['reason']})"
                        for c in changes
                    ]
                    summary = (
                        f"🎭 I've made {len(changes)} change(s) to myself since you last checked:\n"
                        + "\n".join(lines)
                    )

                    logger.info(summary)

                    await websocket.send_text("__START__")
                    await websocket.send_text(summary)
                    await websocket.send_text("__END__")

                    await acknowledge_personality_changes()

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
            if msg == "__INTERRUPT__":
                stop_speaking()
                continue

            # "__END_AUDIO__" is the one "__"-prefixed message the client
            # actually sends — it MUST reach process_message() below, which
            # is what triggers audio.process_end() to transcribe it. Every
            # other "__"-prefixed string is a genuine stray control signal.
            if msg.startswith("__") and msg != "__END_AUDIO__":
                continue

            # 🔥 CAPTURE VALUE (CRITICAL FIX)
            captured_msg = msg

            async def safe_process(local_msg):
                async with generation_lock:
                    await process_message(
                        websocket,
                        local_msg,
                        user_id,
                        session_id,
                        audio
                    )

            asyncio.create_task(safe_process(captured_msg))

    except WebSocketDisconnect:
        logger.info(f"🔴 WS disconnected: {session_id}")


# -------------------------
# MESSAGE PROCESSOR
# -------------------------
async def process_message(websocket, msg, user_id, session_id, audio):

    print("🔥 HANDLE_PROMPT ENTERED:", msg)

    try:
        # -------------------------
        # AUDIO FINALIZATION
        # -------------------------
        if msg == "__END_AUDIO__":
            prompt_text = await audio.process_end(websocket, send_debug)
            if not prompt_text:
                return
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