# identity_manager.py

import re
import asyncio

from fastapi import WebSocketDisconnect

from db.db import (
    fetch_user_facts, update_fact, migrate_user, create_profile, profile_exists,
    reinforce_voice_sample, fetch_voice_samples, fetch_all_voice_profiles, find_profile_by_prefix
)
from speech.tts_engine import synthesize_speech
from speech.stt_engine import transcribe_audio
from speech.voice_id_engine import embed_voice_bytes, best_match, identify_speaker, MATCH_THRESHOLD
from ws.ws_utils import send_debug
from llm.ollama_client import ollama_manager
from core.phrasebook import get_phrase

ENROLL_TARGET_SAMPLES = 3
ENROLL_MAX_ATTEMPTS = 6
RECEIVE_TIMEOUT = 25.0  # a silent mic must never hang a connection forever


async def _speak(websocket, text):
    """2026-07-16: speech now plays through the browser (Web Audio API),
    not a server-side speaker — every former speak(text) call site here
    already sends the same text via websocket.send_text() right before
    calling this, so the audio just needs to follow it over the same
    connection."""
    pcm = await synthesize_speech(text)
    if pcm:
        await websocket.send_bytes(pcm)


class IdentityManager:

    def __init__(self):
        pass

    # -------------------------
    # CLEAN INPUT
    # -------------------------
    def clean_text(self, text: str) -> str:
        if not text:
            return ""
        # remove punctuation, keep letters only
        return re.sub(r'[^a-zA-Z]', '', text.lower())

    # -------------------------
    # RESOLVE USER
    # -------------------------
    async def resolve_user_passive(self, claimed_name, session_id):

        if claimed_name:
            clean_name = self.clean_text(claimed_name)

            if await profile_exists(clean_name):
                return clean_name

            # short/partial name ("craig") -> full profile ("craignorton"),
            # only when exactly one existing profile matches
            prefix_match = await find_profile_by_prefix(clean_name)

            if prefix_match:
                return prefix_match

        return f"pending_user_{session_id}"

    # -------------------------
    # LOW-LEVEL RECEIVE (never blocks forever — a silent mic must not hang the connection)
    # -------------------------
    async def _receive(self, websocket, timeout=RECEIVE_TIMEOUT):
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        # raw receive() doesn't always raise WebSocketDisconnect on disconnect —
        # it can just return this dict. Calling receive() again after that
        # raises RuntimeError, so treat it as a real disconnect right here.
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))

        return message

    # -------------------------
    # RECEIVE INPUT (TEXT OR VOICE -> TRANSCRIBED TEXT)
    # -------------------------
    async def receive_input(self, websocket):

        audio_buffer = b""

        while True:
            message = await self._receive(websocket)

            if message is None:
                await send_debug(websocket, "⚠️ Timed out waiting for a response.")
                return ""  # timed out — caller re-prompts rather than hanging

            # ---------------- TEXT ----------------
            if "text" in message and message["text"]:
                text = message["text"].strip().lower()

                # 🔥 END AUDIO SIGNAL → TRANSCRIBE
                if text == "__end_audio__":
                    if audio_buffer:
                        await send_debug(websocket, f"🎙️ Captured {len(audio_buffer)} bytes, transcribing...")

                        result = transcribe_audio(audio_buffer)
                        print("🎤 Onboarding heard:", result)

                        audio_buffer = b""

                        if result.strip():
                            clean = re.sub(r'[^a-z ]', '', result.lower()).strip()

                            if clean in {"now", "no now", "um", "uh", "okay", "ok", "hmm", "hm"}:
                                await send_debug(websocket, f"⚠️ Ignored filler sound: {result.strip()}")
                                continue

                            await send_debug(websocket, f"🎤 Heard: {result.strip()}")
                            return result.strip().lower()

                        await send_debug(websocket, "⚠️ Couldn't make out any words in that.")

                    continue

                # ❌ ignore system signals
                if text.startswith("__"):
                    continue

                # ✅ normal typed input
                return text

            # ---------------- AUDIO ----------------
            if "bytes" in message and message["bytes"]:
                audio_buffer += message["bytes"]

    # -------------------------
    # RECEIVE RAW VOICE SAMPLE (for enrollment/verification, not transcription)
    # -------------------------
    async def receive_voice_sample(self, websocket) -> bytes:
        audio_buffer = b""

        while True:
            message = await self._receive(websocket)

            if message is None:
                await send_debug(websocket, "⚠️ Timed out waiting for audio — no data received.")
                return audio_buffer

            if "text" in message and message["text"]:
                text = message["text"].strip().lower()

                if text == "__end_audio__":
                    await send_debug(websocket, f"🎙️ Captured {len(audio_buffer)} bytes of audio")
                    return audio_buffer

                if text.startswith("__"):
                    continue

                # typed text where a voice sample was expected — give up on this attempt
                await send_debug(websocket, "⚠️ Got typed text instead of a voice sample.")
                return audio_buffer

            if "bytes" in message and message["bytes"]:
                audio_buffer += message["bytes"]

    # -------------------------
    # RECEIVE GREETING RESPONSE (text/name + raw audio, for voice-first recognition)
    # -------------------------
    async def receive_greeting_response(self, websocket):
        """Returns (text, raw_audio_bytes). raw_audio_bytes is b'' for typed input."""

        audio_buffer = b""

        while True:
            message = await self._receive(websocket)

            if message is None:
                await send_debug(websocket, "⚠️ Timed out waiting for a response.")
                return "", b""

            if "text" in message and message["text"]:
                text = message["text"].strip().lower()

                if text == "__end_audio__":
                    if audio_buffer:
                        await send_debug(websocket, f"🎙️ Captured {len(audio_buffer)} bytes, transcribing...")

                        result = transcribe_audio(audio_buffer)
                        print("🎤 Onboarding heard:", result)

                        captured = audio_buffer
                        audio_buffer = b""

                        if result.strip():
                            # a throat-clear/cough can still transcribe to a
                            # filler interjection ("uh", "um") AND still
                            # acoustically voice-match (same person, same
                            # mic) — filter these out before they can ever
                            # reach recognize_voice(), same noise-list the
                            # normal chat path already uses.
                            clean = re.sub(r'[^a-z ]', '', result.lower()).strip()

                            if clean in {"now", "no now", "um", "uh", "okay", "ok", "hmm", "hm"}:
                                await send_debug(websocket, f"⚠️ Ignored filler sound: {result.strip()}")
                                continue

                            await send_debug(websocket, f"🎤 Heard: {result.strip()}")
                            return result.strip().lower(), captured

                        await send_debug(websocket, "⚠️ Couldn't make out any words in that.")

                    continue

                if text.startswith("__"):
                    continue

                return text, b""

            if "bytes" in message and message["bytes"]:
                audio_buffer += message["bytes"]

    # -------------------------
    # VOICE RECOGNITION (identify who is speaking, across all enrolled profiles)
    # -------------------------
    async def recognize_voice(self, audio_bytes: bytes):
        """Returns (owner_or_None, score)."""

        embedding = embed_voice_bytes(audio_bytes)

        if embedding is None:
            return None, 0.0

        profiles = await fetch_all_voice_profiles()
        owner, score = identify_speaker(embedding, profiles)

        if owner:
            # a confident recognition IS a confirmed-genuine sample —
            # keep the profile adapting instead of frozen at enrollment
            await reinforce_voice_sample(owner, embedding)

        return owner, score

    # -------------------------
    # VOICE ENROLLMENT
    # -------------------------
    async def enroll_voice(self, websocket, user_id,
                            target_samples=ENROLL_TARGET_SAMPLES,
                            max_attempts=ENROLL_MAX_ATTEMPTS) -> int:
        """Collects up to target_samples voice embeddings. Returns how many succeeded."""

        msg = await get_phrase("voice_enroll_intro")
        await websocket.send_text(msg)
        await _speak(websocket, msg)

        collected = 0
        attempts = 0
        empty_in_a_row = 0

        while collected < target_samples and attempts < max_attempts:
            attempts += 1

            prompt = "Say a short sentence." if collected == 0 else "One more — say another sentence."
            await websocket.send_text(prompt)
            await _speak(websocket, prompt)

            audio = await self.receive_voice_sample(websocket)

            if not audio:
                empty_in_a_row += 1

                if empty_in_a_row >= 2:
                    hint = "I'm not hearing any audio — check that Auto Listen is on and your mic is allowed."
                    await websocket.send_text(hint)
                    await _speak(websocket, hint)

                continue

            empty_in_a_row = 0
            embedding = embed_voice_bytes(audio)

            if embedding is None:
                continue

            await reinforce_voice_sample(user_id, embedding)
            collected += 1

        print(f"🎙️ Voice enrollment for {user_id}: {collected}/{target_samples} samples")
        return collected

    # -------------------------
    # VOICE VERIFICATION (used to gate creator trust per session)
    # -------------------------
    async def verify_voice(self, websocket, user_id, max_attempts=3):
        """
        Returns (matched: bool, score: float, heard_text: str). A single
        utterance is noisy (background noise, mic quality, phrase length
        all shift the score a fair bit) so a real match right at the
        threshold shouldn't get permanently rejected off one unlucky
        sample — retry a few times and take the best score seen.

        2026-07-17: found live — "she did not respond at all to what I
        said, just verified." Root cause: this only ever used the
        captured audio for the voice EMBEDDING — never transcribed it,
        so whatever Craig actually said while verifying was silently
        discarded with no real answer, ever. Became a much more likely
        thing to hit once auto-listen-on-join started the mic
        immediately, since anyone talking naturally as soon as the page
        loads has no way to know they're mid-verification rather than
        already in a normal conversation. Now transcribes the same
        captured audio (it's already the standard webm format
        transcribe_audio() expects) and returns the text so the caller
        can route it through the real pipeline afterward — nothing said
        during verification is thrown away anymore, whether it's the
        expected confirmation phrase or a genuine request.
        """

        best_score = 0.0
        heard_text = ""

        for attempt in range(1, max_attempts + 1):
            msg = (
                await get_phrase("voice_verify_prompt")
                if attempt == 1 else
                "Didn't quite match — try saying a bit more, a full sentence."
            )
            await websocket.send_text(msg)
            await _speak(websocket, msg)

            audio = await self.receive_voice_sample(websocket)
            embedding = embed_voice_bytes(audio)

            if audio:
                try:
                    heard_text = (transcribe_audio(audio) or "").strip()
                except Exception:
                    heard_text = ""

            enrolled = await fetch_voice_samples(user_id)
            score = best_match(embedding, enrolled)
            best_score = max(best_score, score)

            print(f"🎙️ Voice verification for {user_id}, attempt {attempt}: score={score:.3f}")

            if score >= MATCH_THRESHOLD:
                # confirmed-genuine sample — reinforce the profile with it
                await reinforce_voice_sample(user_id, embedding)
                return True, score, heard_text

        return False, best_score, heard_text

    # -------------------------
    # NAME COLLECTION (falls back to this only if voice isn't recognized)
    # -------------------------

    # strip lead-in phrasing so "This is Craig" / "Alex, it's Craig" -> "Craig"
    # rather than being kept whole and treated as a literal (garbage) name
    NAME_LEADIN_PATTERNS = [
        r"^(?:hey|hi|hello)?\s*alex[,]?\s*",
        r"^(?:this is|it's|it is|i'm|i am|my name is|call me|the name's)\s+",
    ]

    # single words that mean this isn't a name attempt at all (checked as
    # whole words, not substrings — "how" must not reject "Howard")
    NAME_REJECT_WORDS = {
        "what", "who", "why", "how", "mean", "sorry", "wrong",
        "again", "correct", "understand", "listening",
        "hello", "hi", "hey", "yes", "no",
    }

    def _extract_name_text(self, raw_text: str) -> str:
        text = (raw_text or "").strip().lower()

        for pattern in self.NAME_LEADIN_PATTERNS:
            text = re.sub(pattern, "", text).strip()

        return text

    async def _llm_extract_name(self, raw_text: str):
        """
        LLM-based name extraction — handles phrasings no fixed regex list
        ever fully covers ("you can call me X", "X here", etc). Returns the
        extracted name, or None if the LLM found nothing usable or the call
        failed (Ollama unavailable, malformed output) — callers must fall
        back to the deterministic regex extractor in that case, not treat
        None as "no name was given" when it might just mean "LLM is down."
        """
        if not raw_text or not raw_text.strip():
            return None

        prompt = f"""You are A.L.E.X, an AI assistant. A person just answered your question "who am I speaking with?"

Their reply: "{raw_text}"

Extract ONLY the human speaker's own name from their reply. Note: they may address you as "Alex" first (e.g. "Alex, this is Craig") — "Alex" is YOUR name, not theirs, and must never be extracted as the answer.

Respond with ONLY a JSON object, nothing else:
{{"name": "<their name>"}} if a name is stated, or {{"name": null}} if no name was given (e.g. it's a question or unrelated reply)."""

        result = await ollama_manager.generate_json(prompt)

        if not result:
            return None

        name = result.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None

    async def _collect_valid_name(self, websocket, first_candidate: str) -> str:
        candidate = first_candidate

        while True:
            # LLM extraction first — falls back to the deterministic regex
            # extractor below if the LLM finds nothing or is unavailable
            llm_name = await self._llm_extract_name(candidate) if candidate else None

            if llm_name:
                name = self.clean_text(llm_name)

                if name and name not in ["the", "a", "an", "and"] and 2 <= len(name) <= 20:
                    return name

            stripped = self._extract_name_text(candidate) if candidate else ""
            words = re.findall(r"[a-z']+", stripped)

            if not words:
                msg = await get_phrase("onboard_name_not_caught")
                await websocket.send_text(msg)
                await _speak(websocket, msg)
                candidate = await self.receive_input(websocket)
                continue

            if any(w in self.NAME_REJECT_WORDS for w in words):
                msg = await get_phrase("onboard_name_rejected")
                await websocket.send_text(msg)
                await _speak(websocket, msg)
                candidate = await self.receive_input(websocket)
                continue

            name = self.clean_text(stripped)

            if name in ["the", "a", "an", "and"]:
                candidate = await self.receive_input(websocket)
                continue

            if len(name) < 2:
                msg = await get_phrase("onboard_name_too_short")
                await websocket.send_text(msg)
                await _speak(websocket, msg)
            elif len(name) > 20:
                msg = await get_phrase("onboard_name_rejected")
                await websocket.send_text(msg)
                await _speak(websocket, msg)
            else:
                return name

            candidate = await self.receive_input(websocket)

    # -------------------------
    # ONBOARDING (VOICE-FIRST, FALLS BACK TO NAME)
    # -------------------------
    async def onboard_new_user(self, websocket, temp_user_id, session=None):

        while True:
            # ---------------- GREETING ----------------
            msg = await get_phrase("greeting_new_session")
            await websocket.send_text(msg)
            await _speak(websocket, msg)

            raw_response, raw_audio = await self.receive_greeting_response(websocket)

            # 🎙️ VOICE-FIRST — if we already know this voice, skip the name dance entirely
            if raw_audio:
                recognized_owner, score = await self.recognize_voice(raw_audio)

                if recognized_owner:
                    print(f"🎙️ Recognized returning voice: {recognized_owner} (score={score:.3f})")

                    # this IS a voice verification — no need to ask again later this session
                    if session is not None:
                        session["creator_verified"] = True

                    welcome = await get_phrase("greeting_returning_user", name=recognized_owner)
                    await websocket.send_text(welcome)
                    await _speak(websocket, welcome)

                    return recognized_owner

            name = await self._collect_valid_name(websocket, raw_response)

            # ---------------- CONFIRM ----------------
            confirm_msg = await get_phrase("onboard_confirm_name", name=name)
            await websocket.send_text(confirm_msg)
            await _speak(websocket, confirm_msg)

            raw_confirm = await self.receive_input(websocket)
            confirm = self.clean_text(raw_confirm)

            # 🔥 HANDLE REPEATED / STT DUPLICATES
            confirm_words = ["yes", "y", "yeah", "yep", "correct", "right"]

            if any(word in confirm for word in confirm_words):
                break

            retry_msg = await get_phrase("onboard_confirm_retry")
            await websocket.send_text(retry_msg)
            await _speak(websocket, retry_msg)
            # loop back and ask for the name again

        # ---------------- MIGRATION ----------------
        await migrate_user(temp_user_id, name)

        # ---------------- CREATE PROFILE (CRITICAL) ----------------
        await create_profile(name)

        # ---------------- STORE FACT ----------------
        await update_fact(name, "user_name", name)

        # ---------------- VOICE ENROLLMENT ----------------
        collected = await self.enroll_voice(websocket, name)

        if collected > 0:
            learned_msg = "Voice learned."
            await websocket.send_text(learned_msg)
            await _speak(websocket, learned_msg)

        # ---------------- FINAL ----------------
        final_msg = f"Confirmed."
        await websocket.send_text(final_msg)
        await _speak(websocket, final_msg)

        return name


# -------------------------
# SINGLETON
# -------------------------
identity_manager = IdentityManager()