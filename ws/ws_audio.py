import asyncio
import re

from speech.stt_engine import transcribe_audio
from speech.tts_engine import synthesize_speech
from core.text_utils import first_word


MIN_AUDIO_BYTES = 6000
SPEECH_DEBOUNCE = 1.8

# 2026-07-18 (Craig: "tell me no" got misheard as "tell me now" — asked
# whether she can check her own confidence and ask instead of guessing).
# See speech/stt_engine.py's transcribe_audio() docstring for where this
# number comes from (a clear synthetic-speech test measured ~-0.31;
# Whisper's own internal decoding-retry cutoff is -1.0; this sits between
# the two) — a starting point, not tuned against a genuinely ambiguous
# real recording, same caveat as every other untuned threshold added
# tonight.
LOW_CONFIDENCE_THRESHOLD = -0.6

# Reused both for the clarification follow-up below and nowhere else —
# deliberately broader than a strict yes/no (a clarification answer is
# often a quick "okay"/"mhm", not a formal "yes").
CONFIRM_WORDS = {"yes", "yeah", "yep", "yup", "y", "correct", "right", "affirmative", "okay", "ok", "mhm"}


class AudioProcessor:

    def __init__(self):
        self.audio_buffer = b""
        self.last_audio_time = 0
        # The low-confidence transcript awaiting a yes/no (or a repeat) —
        # set by process_end() itself, consumed on the very next call.
        self.pending_clarification = None

    def add_audio(self, chunk: bytes):
        self.audio_buffer += chunk

    def take_buffer(self) -> bytes:
        """Snapshots and clears the buffer atomically, in whatever
        event-loop tick the caller runs this in — no `await` between the
        read and the reset, so nothing can interleave.

        2026-07-16: found live — "she hears me while talking but doesn't
        process it until after, and only after I repeat myself." Root
        cause: process_end() used to read self.audio_buffer directly, but
        it only runs after acquiring ws_handlers.py's generation_lock,
        which during a barge-in can still be held for several seconds by
        the PREVIOUS turn's still-unwinding response (interrupt is
        noticed on the next streamed-chunk check, not instantly, plus
        profile/after_response bookkeeping still has to finish). Meanwhile
        add_audio() keeps appending to this same buffer, unthrottled, as
        the client's recorder restarts almost immediately for the NEXT
        utterance. So the utterance that actually triggered the barge-in
        got its audio silently mixed with the start of whatever the user
        said next by the time process_end() finally ran — producing a
        garbled transcript that failed the noise/quality filters and
        looked like nothing happened. The caller now takes this snapshot
        the moment "__END_AUDIO__" arrives in the main receive loop,
        BEFORE scheduling the (possibly lock-delayed) processing task —
        see ws_handlers.py."""
        buf = self.audio_buffer
        self.audio_buffer = b""
        return buf

    async def _ask_clarification(self, websocket, prompt_text):
        question = f'Did you say "{prompt_text}"?'
        await websocket.send_text(question)
        pcm = await synthesize_speech(question)
        if pcm:
            await websocket.send_bytes(pcm)

    async def process_end(self, audio_bytes: bytes, websocket, send_debug):
        now = asyncio.get_event_loop().time()

        # debounce
        if now - self.last_audio_time < SPEECH_DEBOUNCE:
            await send_debug(websocket, "⚠️ Debounced — too soon after the last utterance.")
            return None

        self.last_audio_time = now

        if not audio_bytes:
            await send_debug(websocket, "⚠️ No audio was buffered.")
            return None

        if len(audio_bytes) < MIN_AUDIO_BYTES:
            await send_debug(websocket, f"⚠️ Dropped short audio: {len(audio_bytes)} bytes")
            return None

        await send_debug(websocket, f"🎙️ Captured {len(audio_bytes)} bytes, transcribing...")

        text, confidence = transcribe_audio(audio_bytes)

        if not text or len(text.strip()) < 2:
            await send_debug(websocket, "⚠️ Couldn't make out any words in that.")
            return None

        # reject noise
        if re.fullmatch(r"[a-zA-Z\. ]{1,20}", text) and text.count(".") > 3:
            await send_debug(websocket, f"⚠️ Ignored noisy: {text}")
            return None

        await send_debug(websocket, f"🎤 Heard: {text}")

        clean = re.sub(r'[^a-z ]', '', text.lower()).strip()
        prompt_text = re.sub(r"[^a-zA-Z0-9' .?!,]", "", text).strip().lower()

        if not prompt_text:
            await send_debug(websocket, "⚠️ Heard text had no usable content.")
            return None

        # -------------------------
        # CLARIFICATION FOLLOW-UP (2026-07-18) — checked BEFORE the
        # filler-word filter below, since a real answer to "did you say
        # X?" is very often exactly one of those short words ("okay",
        # "yeah") that filter would otherwise silently eat. The prior
        # utterance was already flagged low-confidence, so this one is
        # either a yes/no confirmation or a repeat/correction — never
        # itself subject to a fresh confidence check (a short "yes" is
        # inherently easy to mishear on its own terms, and re-asking about
        # the confirmation would just loop forever).
        # -------------------------
        if self.pending_clarification is not None:
            pending_text = self.pending_clarification
            self.pending_clarification = None

            if first_word(clean) in CONFIRM_WORDS:
                return pending_text

            # Anything else — treat THIS utterance as the real, corrected
            # one; falls through to the normal checks below rather than
            # assuming it's automatically trustworthy.

        if clean in {"now", "no now", "um", "uh", "okay", "ok"}:
            await send_debug(websocket, f"⚠️ Filtered filler word: '{clean}'")
            return None

        # -------------------------
        # CONFIDENCE CHECK (2026-07-18) — see LOW_CONFIDENCE_THRESHOLD
        # above for where this number comes from.
        # -------------------------
        if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
            await send_debug(websocket, f"🤔 Low confidence ({confidence:.2f}), asking for clarification: {prompt_text!r}")
            self.pending_clarification = prompt_text
            await self._ask_clarification(websocket, prompt_text)
            return None

        return prompt_text