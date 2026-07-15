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

    async def process_end(self, websocket, send_debug):
        now = asyncio.get_event_loop().time()

        # debounce
        if now - self.last_audio_time < SPEECH_DEBOUNCE:
            await send_debug(websocket, "⚠️ Debounced — too soon after the last utterance.")
            self.audio_buffer = b""
            return None

        self.last_audio_time = now

        if not self.audio_buffer:
            await send_debug(websocket, "⚠️ No audio was buffered.")
            return None

        if len(self.audio_buffer) < MIN_AUDIO_BYTES:
            await send_debug(websocket, f"⚠️ Dropped short audio: {len(self.audio_buffer)} bytes")
            self.audio_buffer = b""
            return None

        await send_debug(websocket, f"🎙️ Captured {len(self.audio_buffer)} bytes, transcribing...")

        text = transcribe_audio(self.audio_buffer)
        self.audio_buffer = b""

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