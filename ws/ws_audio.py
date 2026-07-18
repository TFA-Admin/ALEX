import asyncio
import re

from speech.stt_engine import transcribe_audio


MIN_AUDIO_BYTES = 6000
SPEECH_DEBOUNCE = 1.8


class AudioProcessor:

    def __init__(self):
        self.audio_buffer = b""
        self.last_audio_time = 0

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

        text = transcribe_audio(audio_bytes)

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

        if clean in {"now", "no now", "um", "uh", "okay", "ok"}:
            await send_debug(websocket, f"⚠️ Filtered filler word: '{clean}'")
            return None

        return prompt_text