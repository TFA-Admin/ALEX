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
import re
import time

print("🔥 RESPONSE HANDLER LOADED")

from speech.tts_engine import synthesize_speech
from ws.ws_utils import split_speakable_text, enrich_profile
from core.text_utils import strip_markdown
from core.voice import playback_seconds
from core.alex_core import alex_core

_SENTENCE_ENDS = ".!?\u2026\"'\u201d\u2019)]*"
_ANY_SENTENCE_END = re.compile(r"[.!?\u2026]")
from core import mood as her_mood
from db.db import record_response_timing
from config.logger_config import logger


_LINE_BREAKS_RE = re.compile(r"[ \t]*\n+[ \t]*")
_LINE_END_NO_PUNCT_RE = re.compile(r"([^.!?:;,\s])[ \t]*\n+")


def shown_and_spoken(text: str):
    """One clause, two renderings (2026-09-21). The screen keeps bullets
    and line breaks (the page wraps with pre-wrap); the voice gets a full
    stop where a line ended without one, so a list is read as items with
    a pause between them rather than one run-on breath."""
    shown = strip_markdown(text, keep_bullets=True).strip()
    spoken = strip_markdown(text).strip()
    spoken = _LINE_END_NO_PUNCT_RE.sub(r"\1. ", spoken)
    spoken = _LINE_BREAKS_RE.sub(" ", spoken)
    return shown, spoken

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
        interrupted = False
        first_audio_at = None    # when her first clip went out
        audio_seconds = 0.0      # how much audio this turn sent, by duration

        async for chunk in stream_fn():
            if session is not None and session.get("interrupted"):
                interrupted = True
                logger.info(f"[ACTION] Response to {user_id} interrupted mid-stream by barge-in")
                # 2026-09-23: THIS is being talked over — cut off while
                # still composing — not every __INTERRUPT__ the page sends
                # when he starts speaking as her last words play out.
                try:
                    await her_mood.note("talked_over", who=user_id)
                except Exception:
                    pass
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

                # 2026-09-21: markdown out before it is shown or spoken —
                # the 9b writes "*do*" and bulleted bold lists, and Piper
                # read the asterisks aloud. See core/text_utils.strip_markdown.
                # Later the same day: the screen keeps bullets and line
                # breaks; the voice gets a pause where the line breaks.
                shown, spoken = shown_and_spoken(speakable)
                if not spoken.strip():
                    continue

                await websocket.send_text(shown)
                tts_t0 = time.time()
                pcm = await synthesize_speech(spoken)
                tts_total += time.time() - tts_t0
                if pcm:
                    await websocket.send_bytes(pcm)
                    if first_audio_at is None:
                        first_audio_at = time.time()
                    audio_seconds += playback_seconds(pcm)

        # Stored and logged clean too: her own replies come back as her
        # context, and markdown there teaches her to write more of it.
        full_response = strip_markdown(full_response)
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
        # 2026-09-23 (Craig: "a sentence was cut off"): the token cap ends
        # a reply mid-sentence — "Do you want me to analyze the". A tail
        # with no sentence end, after at least one complete sentence, is
        # the cap's fragment: dropped and logged, never shown, spoken or
        # remembered. She says less; she does not stop mid-word.
        tail = speech_buffer.strip()
        if (tail and not interrupted and tail[-1] not in _SENTENCE_ENDS
                and _ANY_SENTENCE_END.search(full_response[:-len(tail)] if full_response.endswith(tail) else full_response)):
            logger.info(f"[VERBOSITY] dropped a cut-off fragment: {tail[:80]!r}")
            if full_response.endswith(tail):
                full_response = full_response[:-len(tail)].rstrip()
            speech_buffer = ""

        remaining, remaining_spoken = shown_and_spoken(speech_buffer)

        if user_id not in NO_SPEECH_USERS and not interrupted and remaining_spoken:
            await websocket.send_text(remaining)
            tts_t0 = time.time()
            pcm = await synthesize_speech(remaining_spoken)
            tts_total += time.time() - tts_t0
            if pcm:
                await websocket.send_bytes(pcm)
                if first_audio_at is None:
                    first_audio_at = time.time()
                audio_seconds += playback_seconds(pcm)

        # 🔄 PROFILE UPDATE
        updated = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated))

        # 🎭 MOOD — computed after the real response is already fully on
        # its way, never blocks or slows it down. See core/mood.py.
        # 2026-09-23: her reply's tone is one input to a real mood state;
        # what the orb gets is the state (core/mood.py).
        if not interrupted and full_response:
            await her_mood.note_reply(full_response, user_id)
            await websocket.send_text("__MOOD__" + her_mood.payload(await her_mood.state()))

        # 2026-07-17 (Craig: "over the course of a long response... when
        # I said I agree she didn't hear it") — the wake-word conversation
        # window (see ws/ws_handlers.py's CONVERSATION_WINDOW_S) only ever
        # got refreshed by Craig's OWN addressed utterance, never by how
        # long her reply took to finish. A response longer than the
        # window let it expire before he could even reply, so an
        # unaddressed follow-up like "I agree" was silently ignored.
        # Refreshing it here means the window always restarts from when
        # she stops talking, not from when he started.
        #
        # 2026-07-17 (Craig: "she continues to respond to obvious end of
        # discussion statements") — this refresh was undoing
        # ws/ws_handlers.py's own attempt to close the window on a
        # closing remark ("that's all for now"): that utterance still
        # gets a real reply, and this line would immediately re-open the
        # window right after, so the conversation never actually ended.
        # conversation_closing is set on exactly (and only) that turn.
        # 2026-07-18 (Craig: "her presence in the UI still says
        # listening" — wants it to reflect whether she's actually
        # engaged, not just whether the mic is armed) — ws/ws_handlers.py
        # already sends __ENGAGED__1 the moment a turn starts, based on
        # the window at THAT time; this re-sends it here too because the
        # window's real expiry (what the browser mirrors locally) just
        # got pushed out further, to whenever she finished talking, which
        # can be well after the turn started for a long response.
        # 2026-09-23 (Craig: "make it so she times out from listening based
        # on when she's done talking versus the last user vocalization").
        # This refreshed the window when the last clip was SENT, but the
        # browser plays clips back to back after they arrive, so on a long
        # reply the window was already running out while she was still
        # audible. The clips' own durations say when she actually stops;
        # the window starts then.
        if session is not None:
            if session.pop("conversation_closing", False):
                session["last_addressed_at"] = 0
            else:
                still_playing = 0.0
                if first_audio_at is not None:
                    still_playing = max(0.0, first_audio_at + audio_seconds - time.time())
                session["last_addressed_at"] = time.time() + still_playing
                await websocket.send_text("__ENGAGED__1")
            # 2026-09-25: the shape of this reply, for core/values.py — what
            # his next thanks or correction is about.
            try:
                from core import values as her_values
                session["last_reply"] = her_values.reply_shape(full_response, session.pop("last_reply_looked", False))
                hist = list(session.get("reply_history") or [])
                hist.append(full_response[:400])
                session["reply_history"] = hist[-3:]      # for "you made that up" (core/claims.py)
            except Exception:
                pass

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

        # 2026-09-25: a reply may carry a shorter spoken form.
        spoken_form = (result.get("spoken") or "").strip()
        content = strip_markdown(result.get("content", ""), keep_bullets=bool(spoken_form))

        logger.info(f"[RESPONSE] to {user_id}: {content}")

        await websocket.send_text("__START__")
        await websocket.send_text(content)

        # 2026-09-21: audio INSIDE the envelope, right after its text, the
        # same order _handle_stream and core/voice.say() use. This path
        # sent __END__ first and the audio after it. The page reveals a
        # clause's text when its audio starts, so at __END__ the text was
        # still pending, the bubble looked blank and was removed as such,
        # and the reply never appeared — every phrase, diagnostic and
        # refusal she spoke was missing from the transcript (Craig: "the
        # conversation list is currently empty"). Audio after __END__ is
        # also exactly what the page discards once he has interrupted her
        # even once (see core/voice.py).
        tts_total = 0.0
        spoken_for = 0.0
        if content and user_id not in NO_SPEECH_USERS:
            tts_t0 = time.time()
            pcm = await synthesize_speech(spoken_form or strip_markdown(content))
            tts_total = time.time() - tts_t0
            if pcm:
                await websocket.send_bytes(pcm)
                spoken_for = playback_seconds(pcm)

        updated = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated))

        if content:
            await her_mood.note_reply(content, user_id)
            await websocket.send_text("__MOOD__" + her_mood.payload(await her_mood.state()))

        # See the matching comments in _handle_stream — refreshes the
        # wake-word conversation window from when she finishes speaking,
        # not from when she was addressed, unless this turn was itself a
        # closing remark (in which case the window should stay closed).
        if session is not None:
            if session.pop("conversation_closing", False):
                session["last_addressed_at"] = 0
            else:
                # the window starts when the clip finishes playing, not when it was sent
                session["last_addressed_at"] = time.time() + spoken_for
                await websocket.send_text("__ENGAGED__1")

        await websocket.send_text("__END__")

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