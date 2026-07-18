import os
import re
import asyncio
import subprocess

# Piper reads "A.L.E.X." as individual letters (the dots read as sentence
# breaks). Normalize it to the plain word so it's spoken as a name, not
# spelled out — every route to speech funnels through synthesize_speech()
# below.
ALEX_ACRONYM = re.compile(r"\bA\.\s*L\.\s*E\.\s*X\.?", re.IGNORECASE)

# The LLM sometimes writes roleplay-style stage directions/actions wrapped
# in asterisks (e.g. "*pauses dramatically*"). Reads fine as chat text —
# Craig likes the effect there. First attempt (2026-07-17) just deleted
# these before synthesis, which stopped her saying the words but also
# collapsed the moment to nothing. Craig's actual ask: he wants the pause
# itself — real silence — not the words describing one. synthesize_speech()
# below now splits the clause on this pattern and synthesizes each side
# separately, splicing in actual silent PCM where the stage direction was.
STAGE_DIRECTION_RE = re.compile(r"\*[^*]+\*")

# Piper's prosody spikes noticeably on "!" — reads as far more excited/
# elevated than her tuned personality actually calls for, even on a short
# exclamation like "Got it!" (2026-07-17: "seems out of character... the
# voice elevates substantially"). Downgrade to a period for speech only;
# the chat text keeps the real punctuation.
EXCLAMATION_RE = re.compile(r"!+")


def _normalize_for_speech(text: str) -> str:
    text = ALEX_ACRONYM.sub("Alex", text)
    return EXCLAMATION_RE.sub(".", text)

# Piper (and its voice model) live outside this repo — env vars let this
# run on a machine with a different drive/folder layout without editing
# code; defaults match this machine's current setup.
PIPER_PATH = os.getenv("ALEX_PIPER_PATH", r"D:\project_ALEX\piper\piper.exe")
MODEL_PATH = os.getenv(
    "ALEX_PIPER_MODEL",
    r"D:\project_ALEX\piper\models\glados_piper_medium.onnx"
)
# previous voice: r"D:\project_ALEX\piper\models\en_US-amy-medium.onnx"

# Output sample format Piper's --output-raw mode always produces — the
# browser-side playback code needs this exact value to build correct
# AudioBuffers from the raw bytes.
SAMPLE_RATE = 22050

# How long a "*stage direction*" becomes as real silence. A flat duration
# regardless of the actual words inside the asterisks (e.g. "*pauses*" vs
# "*sighs dramatically*") — good enough for "be quiet for a second" without
# trying to interpret the action text itself. 16-bit mono PCM, so 2 bytes
# per sample.
PAUSE_MS = 500
_PAUSE_SILENCE = b"\x00\x00" * int(SAMPLE_RATE * PAUSE_MS / 1000)


def _run_piper_sync(text: str):
    """Blocking — MUST be called via asyncio.to_thread(), never directly
    from async code. Uses the exact same subprocess.Popen pattern proven
    reliable all session (identical flags/behavior to the old thread-based
    speak() pipeline), deliberately NOT asyncio.create_subprocess_exec:
    found live (2026-07-16) that switching to the asyncio subprocess API
    caused synthesize_speech() to hang indefinitely with no exception ever
    raised — uvicorn's WebSocket layer forces a SelectorEventLoop on
    Windows, which does not support asyncio subprocess creation at all.
    Running the plain, well-understood blocking subprocess API in a worker
    thread sidesteps that entirely while still being awaitable from async
    callers."""
    process = subprocess.Popen(
        [
            PIPER_PATH,
            "-m", MODEL_PATH,
            "--output-raw",
            "--length_scale", "1.15",   # was 1.05 — still spoke too fast
            "--noise_scale", "0.4",     # smoother tone
            "--noise_w", "0.6",         # reduces harshness
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout, _ = process.communicate(text.encode("utf-8"))
    return stdout


async def synthesize_speech(text: str):
    """2026-07-16: replaces the old thread/queue/sounddevice pipeline that
    played audio through the SERVER's own speakers. Craig asked for real
    text/speech sync, which meant moving playback into the browser (Web
    Audio API) — the browser can only sync against audio it's actually
    playing itself, not a signal relayed from a separate server-side
    process. core/response_handler.py now sends the returned PCM bytes
    over the same websocket the text goes to, instead of this module
    playing them locally.

    Runs Piper (via _run_piper_sync, in a worker thread — see its
    docstring for why not a direct asyncio subprocess) for this one clause
    and returns its complete raw PCM (16-bit signed, mono, SAMPLE_RATE Hz
    — Piper's --output-raw convention) or None if there's nothing to say
    or synthesis failed.

    A clause containing "*stage directions*" is split around each one and
    synthesized in pieces, with real silence (_PAUSE_SILENCE) spliced in
    between — so a line like "Sure. *pauses* Fine, whatever." comes out
    as speech-silence-speech, not the words "pauses" spoken aloud."""
    text = _normalize_for_speech(text)
    if not text.strip():
        return None

    segments = STAGE_DIRECTION_RE.split(text)
    pcm_chunks = []

    for i, segment in enumerate(segments):
        segment = re.sub(r"\s{2,}", " ", segment).strip()

        if segment:
            try:
                stdout = await asyncio.to_thread(_run_piper_sync, segment)
            except Exception as e:
                print("TTS error:", e)
                stdout = None
            if stdout:
                pcm_chunks.append(stdout)

        if i < len(segments) - 1:
            pcm_chunks.append(_PAUSE_SILENCE)

    return b"".join(pcm_chunks) if pcm_chunks else None
