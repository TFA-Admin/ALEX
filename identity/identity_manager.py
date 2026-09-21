# identity_manager.py

import re
import asyncio

from fastapi import WebSocketDisconnect

from db.db import (
    remember_own_utterance,
    update_fact, migrate_user, create_profile, profile_exists,
    reinforce_voice_sample, fetch_voice_samples, fetch_all_voice_profiles, find_profile_by_prefix
)
from speech.tts_engine import synthesize_speech
from speech.stt_engine import transcribe_audio
from speech.voice_id_engine import embed_voice_bytes, best_match, identify_speaker, MATCH_THRESHOLD
from ws.ws_utils import send_debug
from llm.ollama_client import ollama_manager
from core.phrasebook import get_phrase
from core.voice import say
from core.override_code import is_creator_override_code

ENROLL_TARGET_SAMPLES = 3
ENROLL_MAX_ATTEMPTS = 6
RECEIVE_TIMEOUT = 25.0  # a silent mic must never hang a connection forever

# Raw PCM from Piper: 16-bit signed, mono. Imported rather than redefined so
# it cannot drift away from what the engine actually produces.
from speech.tts_engine import SAMPLE_RATE as TTS_SAMPLE_RATE

# Breathing room after the audio ends before she starts listening — covers
# the browser's own scheduling latency and stops the tail of her last word
# being recorded as the start of his answer. Untuned starting point.
SPEECH_SETTLE_S = 0.4


async def _speak(websocket, text, user_id=None):
    """The handshake voice. 2026-09-20: now a thin wrapper over
    core/voice.say(), which owns the lock, the envelope, waiting out the
    playback and recording it.

    wait=True here and nowhere else: the conversational client mutes its
    own recorder during playback and supports barge-in, which is wanted
    there. Being interrupted — or recording her own voice as the reply —
    while asking who you are is exactly what must not happen, and was the
    reason Craig could not get through login at all."""
    await say(websocket, text, user_id=user_id, remember=bool(user_id), wait=True)


def _is_just_the_prompt(heard: str, prompt: str) -> bool:
    """True if everything heard is already in the prompt she just spoke.

    Ignores filler that surrounds compliance ("okay", "alex", "it's",
    "craig") so "Alex, it's Craig — verify access" still counts as simply
    doing as asked."""
    filler = {"alex", "its", "it", "is", "im", "craig", "okay", "ok", "sure",
              "yeah", "yes", "um", "uh", "and", "the", "a", "my", "name",
              "please", "here", "this", "hey", "hello", "hi", "thats"}

    def words(text):
        # Apostrophes stripped BEFORE tokenising. Leaving them in split
        # "it's" into {"it", "s"} and "I'll" into {"i", "ll"}, and the
        # orphan letters were never in the prompt, so a perfectly
        # compliant "Alex, it's Craig. Verify access" failed the test.
        flat = (text or "").lower().replace("'", "").replace("’", "")
        return {w for w in re.findall(r"[a-z]+", flat) if len(w) > 1} - filler

    said, asked = words(heard), words(prompt)

    if not heard.strip():
        return False

    # Nothing left after filler means he answered with his name and
    # pleasantries — "My name is Craig" against "state your name". That is
    # compliance, not a request, and must not be routed either.
    if not said:
        return True

    return said <= asked


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

                        result, _confidence = transcribe_audio(audio_buffer)
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
    async def receive_voice_sample(self, websocket):
        """Returns (audio_bytes, typed_text). typed_text is "" unless the
        person answered by typing instead of speaking.

        2026-09-20 (Craig, on trying to use her from chat for the first
        time): "she's not accepting my responses and returning some odd
        responses." Confirmed in her own log —

            19:32:28  Timed out waiting for a response.
            19:32:53  Timed out waiting for a response.
            19:33:27  Got typed text instead of a voice sample.

        This used to throw the typed text away and return an empty buffer,
        which scored 0 against the enrolled profile. verify_voice() then
        retried twice more and failed, and every message he typed was
        swallowed by the loop. **There was no way through this gate from a
        text client at all** — not a hard path, no path.

        Returning the text instead of discarding it is the same fix applied
        to verify_voice() on 2026-07-17 for a different swallow ("nothing
        said during verification is thrown away anymore"), which this path
        never got."""
        audio_buffer = b""

        while True:
            message = await self._receive(websocket)

            if message is None:
                await send_debug(websocket, "⚠️ Timed out waiting for audio — no data received.")
                return audio_buffer, ""

            if "text" in message and message["text"]:
                raw = message["text"].strip()
                text = raw.lower()

                if text == "__end_audio__":
                    await send_debug(websocket, f"🎙️ Captured {len(audio_buffer)} bytes of audio")
                    return audio_buffer, ""

                if text.startswith("__"):
                    continue

                # Typed where a voice sample was expected. Hand it back
                # rather than dropping it — the caller decides what it means.
                await send_debug(websocket, f"⌨️ Typed instead of spoken: {raw!r}")
                return audio_buffer, raw

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

                        result, _confidence = transcribe_audio(audio_buffer)
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
        await _speak(websocket, msg, user_id=user_id)

        collected = 0
        attempts = 0
        empty_in_a_row = 0

        while collected < target_samples and attempts < max_attempts:
            attempts += 1

            prompt = "Say a short sentence." if collected == 0 else "One more — say another sentence."
            await websocket.send_text(prompt)
            await _speak(websocket, prompt, user_id=user_id)

            audio, typed = await self.receive_voice_sample(websocket)

            if typed:
                # Enrollment genuinely cannot be done by typing — there is
                # no voice to learn. Say so plainly instead of silently
                # retrying, which is what made this look broken.
                note = ("I can't learn your voice from typed text — I need to "
                        "hear you. Turn Auto Listen on, or keep going in text "
                        "and I'll work without voice recognition.")
                await websocket.send_text(note)
                await _speak(websocket, note, user_id=user_id)
                return collected

            if not audio:
                empty_in_a_row += 1

                if empty_in_a_row >= 2:
                    hint = "I'm not hearing any audio — check that Auto Listen is on and your mic is allowed."
                    await websocket.send_text(hint)
                    await _speak(websocket, hint, user_id=user_id)

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
            await _speak(websocket, msg, user_id=user_id)

            audio, typed = await self.receive_voice_sample(websocket)

            if typed:
                # 2026-09-20: a text client has no voice to offer, so voice
                # cannot be the only way through. Two outcomes, both of them
                # already part of the design:
                #
                #   the override code  -> proof. core/override_code.py and
                #       systems/controller/_role_gates.py already treat the
                #       code alone as sufficient creator proof, independent
                #       of whatever voice this session resolved to. This is
                #       the same rule, applied one step earlier.
                #
                #   anything else      -> not proof, and not a failure
                #       either. She stops asking, carries on unverified, and
                #       what he typed is returned so it gets a real answer
                #       instead of being eaten by this loop. Privileged
                #       actions stay gated by require_creator(), which is
                #       where that decision belongs.
                if await is_creator_override_code(typed):
                    print(f"🎙️ Voice verification for {user_id}: override code accepted in text")
                    return True, 1.0, typed
                return False, best_score, typed

            embedding = embed_voice_bytes(audio)

            if audio:
                try:
                    heard_text = (transcribe_audio(audio)[0] or "").strip()
                except Exception:
                    heard_text = ""

            enrolled = await fetch_voice_samples(user_id)
            score = best_match(embedding, enrolled)
            best_score = max(best_score, score)

            print(f"🎙️ Voice verification for {user_id}, attempt {attempt}: score={score:.3f}")

            # 2026-09-20 — do not hand back the phrase she just asked for.
            #
            # Craig: "she asked me to say something so I did. then she gave
            # me diagnostic information... She seems unaware of her own
            # utterence to start." Exactly that, in her log:
            #
            #   00:39:03  Voice verified (score=0.76)
            #   00:39:05  intent 'status_check' (from: 'Verify access.')
            #   00:39:06  All core systems online...
            #
            # She asked him to say "verify access", he said it, and the
            # pipeline read her own requested passphrase as a command. The
            # 2026-07-17 change that routes this transcript is right —
            # nothing said during verification should be thrown away — but
            # it routed BLIND, with nothing knowing the words came from her.
            #
            # Subset test rather than equality: speech recognition will not
            # return the prompt verbatim, and he may add "Alex" or "it's
            # Craig" around it. If everything he said already appears in
            # what she asked for, he was complying, not requesting.
            if heard_text and _is_just_the_prompt(heard_text, msg):
                print(f"🎙️ Heard back the phrase she asked for; not routing it")
                heard_text = ""

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
        r"^(?:hey|hi|hello)?[,]?\s*alex[,.]?\s*",
        r"^(?:this is|it's|it is|i'm|i am|my name is|call me|the name's)\s+",
    ]

    # 2026-07-18 (Sadie's onboarding got stuck in a loop): the anchored
    # patterns above only strip a lead-in at the very START of the reply.
    # Rambling, conversational speech ("Hello, Alex. Oh, it's not here...
    # my name is Sadie.") buries the actual "my name is X" well past the
    # start, so nothing got stripped, the leftover "hello" tripped
    # NAME_REJECT_WORDS below, and the whole valid name got rejected. This
    # searches for the same lead-in phrases ANYWHERE in the text and, if
    # found, keeps only what follows the LAST one — the part of a rambling
    # reply closest to "and that's my actual name" is the part after the
    # last "my name is"/"it's"/etc, not the start of the sentence.
    NAME_LEADIN_ANYWHERE = re.compile(
        r"(?:this is|it's|it is|i'm|i am|my name is|call me|the name's)\s+",
        re.IGNORECASE,
    )

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

        matches = list(self.NAME_LEADIN_ANYWHERE.finditer(text))
        if matches:
            text = text[matches[-1].end():].strip()

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
        """Returns (user_id, heard_text). heard_text is whatever the
        person actually said during the voice-first recognition greeting
        — empty string for the name-collection path below, since that
        content is identity setup (their name), not a real request.

        2026-07-18 (Craig: "my first utterance which she used to verify
        me is being eaten again") — this is the sibling of the
        2026-07-17 verify_voice() fix, in a path that fix never touched.
        resolve_user_passive() failing to recognize a claimed name (same
        root cause as before — the browser's cached username not being
        sent) routes here instead of the later separate verification
        block, and THIS path had the exact same bug: raw_response (what
        was actually said) was captured and then only ever used to
        recognize the VOICE, never answered. Whatever prompted the
        original fix in verify_voice() applies here too."""

        while True:
            # ---------------- GREETING ----------------
            msg = await get_phrase("greeting_new_session")
            await websocket.send_text(msg)
            # 2026-09-21: found live — every _speak() in this function passed a
            # `user_id` that does not exist here (the parameter is temp_user_id),
            # so the first greeting raised NameError and the socket closed with
            # nothing in her log. Unhit since the 09-20 rewrite because a browser
            # with a saved name never onboards. Identity-setup lines are not
            # remembered (no user yet); the welcome and the closing lines are,
            # under the name they belong to.
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
                    await _speak(websocket, welcome, user_id=recognized_owner)

                    return recognized_owner, raw_response

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
            await _speak(websocket, learned_msg, user_id=name)

        # ---------------- FINAL ----------------
        final_msg = "Confirmed."
        await websocket.send_text(final_msg)
        await _speak(websocket, final_msg, user_id=name)

        return name, ""


# -------------------------
# SINGLETON
# -------------------------
identity_manager = IdentityManager()