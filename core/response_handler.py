# core/response_handler.py

"""
Response Handler

Responsible for:
- consuming system outputs
- handling streaming
- sending to websocket
- triggering TTS
- triggering post-response system hooks (memory, etc.)
"""

import json
import time

print("🔥 RESPONSE HANDLER LOADED")

from speech.tts_engine import synthesize_speech
from ws.ws_utils import split_speakable_text, enrich_profile
from core.alex_core import alex_core
from core.mood import derive_mood
from db.db import record_response_timing
from config.logger_config import logger

# tools/claude_client.py registers as this profile — a headless, text-only
# debugging client with no microphone and no physical presence in the
# room. speak() used to fire unconditionally for every response
# regardless of who asked, so a single verbose test response (a full
# memory-recall dump) ended up read aloud through the real server
# speakers for several minutes with nothing to indicate why, since the
# request itself never touched STT/audio at all. Excluding this one
# known identity is a targeted fix for the actual cause, not a general
# length cap — a real voice session giving a long real answer should
# still be spoken in full.
NO_SPEECH_USERS = {"claude"}


class ResponseHandler:

    def __init__(self):
        pass

    # -------------------------
    # MAIN OUTPUT ROUTER
    # -------------------------
    async def handle(self, websocket, result, user_id, session_id=None, input_data=None):

        print("🔥 HANDLE CALLED")

        if not result:
            return

        result_type = result.get("type")
        print(f"[DEBUG] result_type = {result_type}")

        if result_type == "stream":
            await self._handle_stream(websocket, result, user_id, session_id, input_data)
            return

        if result_type == "response":
            await self._handle_simple(websocket, result, user_id, session_id, input_data)
            return

    # -------------------------
    # STREAMING HANDLER
    # -------------------------
    async def _handle_stream(self, websocket, result, user_id, session_id, input_data):

        print("ENTERED STREAM HANDLER")

        # Falls back to now if missing (e.g. a direct/test call bypassing
        # alex_core.handle_input()) rather than crashing on a KeyError.
        turn_session = alex_core.get_session(session_id) if session_id else {}
        turn_start = turn_session.get("turn_start_time", time.time())
        tts_total = 0.0

        stream_fn = result.get("stream")

        if not stream_fn:
            return

        # Cleared at the start of every new response so a past interrupt
        # doesn't silently block all future ones — see ws/ws_handlers.py's
        # "__INTERRUPT__" handler for where this gets set to True.
        session = alex_core.get_session(session_id) if session_id else None
        if session is not None:
            session["interrupted"] = False

        await websocket.send_text("__START__")

        full_response = ""
        speech_buffer = ""
        spoke_any = False
        interrupted = False

        async for chunk in stream_fn():
            if session is not None and session.get("interrupted"):
                interrupted = True
                logger.info(f"[ACTION] Response to {user_id} interrupted mid-stream by barge-in")
                break

            full_response += chunk

            # 2026-07-16: speech now plays through the BROWSER (Web Audio
            # API), not the server's own speakers — real text/speech sync
            # means text can't stream ahead of speech anymore, so each
            # clause's text is sent right alongside its synthesized audio
            # instead of every raw LLM token going out immediately. The
            # headless test client (NO_SPEECH_USERS) has no speech to sync
            # against, so it keeps the old eager per-chunk text streaming.
            if user_id in NO_SPEECH_USERS:
                await websocket.send_text(chunk)
                continue

            speech_buffer += chunk

            while True:
                speakable, speech_buffer = split_speakable_text(speech_buffer)

                if not speakable:
                    break

                await websocket.send_text(speakable)
                tts_t0 = time.time()
                pcm = await synthesize_speech(speakable)
                tts_total += time.time() - tts_t0
                if pcm:
                    await websocket.send_bytes(pcm)
                spoke_any = True

        logger.info(f"[RESPONSE] to {user_id}: {full_response}")

        # 🔊 remaining text/speech — a trailing fragment with no
        # clause-ending punctuation never got sent inside the loop above
        # (only complete clauses are, now that text is paired with its
        # audio instead of streamed eagerly), so this is the only place
        # it's ever shown or spoken. Skipped entirely if interrupted.
        #
        # 2026-07-17: found live — "she's cutting off her text", meaning
        # multi-sentence replies were showing up split across two message
        # bubbles, with the tail end reading like an unrelated fragment.
        # Root cause: this used to run AFTER __END__ was sent below.
        # avatar.html's __END__ handler treats the response as fully over
        # and resets its message div to null — so this trailing fragment,
        # arriving afterward, had nowhere to append to and started a
        # brand-new bubble instead of continuing the same one. Sending it
        # BEFORE __END__ (and the profile update, which has no ordering
        # requirement either way) fixes that.
        remaining = speech_buffer.strip()

        if user_id not in NO_SPEECH_USERS and not interrupted and remaining:
            await websocket.send_text(remaining)
            tts_t0 = time.time()
            pcm = await synthesize_speech(remaining)
            tts_total += time.time() - tts_t0
            if pcm:
                await websocket.send_bytes(pcm)

        # 🔄 PROFILE UPDATE
        updated = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated))

        # 🎭 MOOD — computed after the real response is already fully on
        # its way, never blocks or slows it down. See core/mood.py.
        if not interrupted and full_response:
            mood = derive_mood(full_response)
            await websocket.send_text("__MOOD__" + mood)

        # 2026-07-17 (Craig: "over the course of a long response... when
        # I said I agree she didn't hear it") — the wake-word conversation
        # window (see ws/ws_handlers.py's CONVERSATION_WINDOW_S) only ever
        # got refreshed by Craig's OWN addressed utterance, never by how
        # long her reply took to finish. A response longer than the
        # window let it expire before he could even reply, so an
        # unaddressed follow-up like "I agree" was silently ignored.
        # Refreshing it here means the window always restarts from when
        # she stops talking, not from when he started.
        if session is not None:
            session["last_addressed_at"] = time.time()

        await websocket.send_text("__END__")

        total_duration = time.time() - turn_start
        logger.info(
            f"[TIMING] TOTAL turn (heard -> fully spoken) for {user_id}: "
            f"{total_duration:.2f}s (of which TTS synthesis: {tts_total:.2f}s)"
        )
        # 2026-07-16: recorded so diagnostic_tool can notice a real
        # slowdown (e.g. the GPU/VRAM-exhaustion hang found live tonight)
        # against her own recent history, not just log it for a human to
        # spot after the fact — see db.record_response_timing().
        await record_response_timing(total_duration)

        # 🔥 AFTER RESPONSE HOOK (THIS IS THE MISSING PIECE)
        if session_id and input_data:
            session = alex_core.get_session(session_id)

            await alex_core.systems.after_response(
                session,
                user_id,
                input_data,
                full_response
            )

    # -------------------------
    # SIMPLE RESPONSE
    # -------------------------
    async def _handle_simple(self, websocket, result, user_id, session_id, input_data):

        turn_session = alex_core.get_session(session_id) if session_id else {}
        turn_start = turn_session.get("turn_start_time", time.time())
        session = alex_core.get_session(session_id) if session_id else None

        content = result.get("content", "")

        logger.info(f"[RESPONSE] to {user_id}: {content}")

        await websocket.send_text("__START__")
        await websocket.send_text(content)

        updated = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated))

        if content:
            mood = derive_mood(content)
            await websocket.send_text("__MOOD__" + mood)

        # See the matching comment in _handle_stream — refreshes the
        # wake-word conversation window from when she finishes speaking,
        # not from when she was addressed.
        if session is not None:
            session["last_addressed_at"] = time.time()

        await websocket.send_text("__END__")

        tts_total = 0.0
        if content and user_id not in NO_SPEECH_USERS:
            tts_t0 = time.time()
            pcm = await synthesize_speech(content)
            tts_total = time.time() - tts_t0
            if pcm:
                await websocket.send_bytes(pcm)

        total_duration = time.time() - turn_start
        logger.info(
            f"[TIMING] TOTAL turn (heard -> fully spoken) for {user_id}: "
            f"{total_duration:.2f}s (of which TTS synthesis: {tts_total:.2f}s)"
        )
        await record_response_timing(total_duration)

        print("STREAM COMPLETE")

        # 🔥 AFTER RESPONSE HOOK
        if session_id and input_data:
            session = alex_core.get_session(session_id)

            await alex_core.systems.after_response(
                session,
                user_id,
                input_data,
                content
            )


# -------------------------
# SINGLETON
# -------------------------
response_handler = ResponseHandler()