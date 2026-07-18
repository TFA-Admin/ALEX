import os
import re
import asyncio
import subprocess
import threading
import queue

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


# 2026-07-18 (roadmap: "TTS respawns piper.exe per chunk — real, measured
# gap between sentences") — keeps ONE piper.exe process alive for the
# life of the server instead of paying its ~0.7s model-load cost on every
# clause (confirmed live via a direct probe: "Loaded voice in
# 0.7360964 second(s)" printed exactly once at startup, never again).
#
# Piper's --output_raw mode gives no explicit per-utterance framing on
# stdout — confirmed by the same probe: feeding it two lines back-to-back
# produces one continuous, undelimited byte stream, no marker between
# them. But its stderr reliably logs one "Real-time factor: ..." line the
# instant each utterance's audio finishes writing to stdout (confirmed:
# the two events landed about 1ms apart in testing) — used here purely
# as a completion BARRIER, not for byte-counting (its reported duration
# was close to, but not exactly, the real byte count — almost certainly
# --sentence_silence padding — so counting bytes off of it would risk an
# off-by-a-few-dozen-milliseconds cut). After writing a line to stdin,
# wait for the next barrier, then drain whatever accumulated on stdout
# since the last one — that's the complete, correct utterance.
class _PersistentPiper:

    def __init__(self):
        self._process = None
        self._lifecycle_lock = threading.Lock()  # guards spawning/killing the process itself
        self._call_lock = asyncio.Lock()          # one utterance in flight at a time — the barrier above only means anything for a single request
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._utterance_ready = queue.Queue()

    def _spawn(self):
        process = subprocess.Popen(
            [
                PIPER_PATH,
                "-m", MODEL_PATH,
                "--output_raw",
                "--length_scale", "1.15",   # was 1.05 — still spoke too fast
                "--noise_scale", "0.4",     # smoother tone
                "--noise_w", "0.6",         # reduces harshness
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._read_stdout, args=(process,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
        return process

    def _read_stdout(self, process):
        while True:
            try:
                chunk = process.stdout.read(4096)
            except (ValueError, OSError):
                return
            if not chunk:
                return
            with self._buffer_lock:
                self._buffer.extend(chunk)

    def _read_stderr(self, process):
        try:
            for raw_line in process.stderr:
                if b"Real-time factor" in raw_line:
                    self._utterance_ready.put(True)
        except (ValueError, OSError):
            return

    def _ensure_alive(self):
        with self._lifecycle_lock:
            if self._process is None or self._process.poll() is not None:
                self._process = self._spawn()
            return self._process

    def _kill(self, process):
        with self._lifecycle_lock:
            try:
                process.kill()
            except Exception:
                pass
            if self._process is process:
                self._process = None

    def _synthesize_sync(self, text: str, timeout: float = 20.0):
        process = self._ensure_alive()

        # Defensive only — should already be empty between calls, since
        # _call_lock serializes access and every call drains its own
        # output before returning.
        with self._buffer_lock:
            self._buffer.clear()
        while not self._utterance_ready.empty():
            try:
                self._utterance_ready.get_nowait()
            except queue.Empty:
                break

        try:
            process.stdin.write((text.replace("\n", " ") + "\n").encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            # Died between _ensure_alive() and this write — respawn on
            # the NEXT call rather than retry inline here; this one
            # utterance is just lost, same as any other synthesis
            # failure already handled by synthesize_speech()'s caller.
            self._kill(process)
            return None

        try:
            self._utterance_ready.get(timeout=timeout)
        except queue.Empty:
            # Hung or crashed mid-utterance — kill it so the NEXT call
            # gets a fresh process instead of talking to a dead one.
            self._kill(process)
            return None

        with self._buffer_lock:
            data = bytes(self._buffer)
            self._buffer.clear()

        return data or None

    async def synthesize(self, text: str):
        async with self._call_lock:
            return await asyncio.to_thread(self._synthesize_sync, text)

    def shutdown(self):
        """Called from main.py's lifespan shutdown — without this, the
        persistent process would be exactly the kind of orphaned
        background process this whole project spent real effort hunting
        down elsewhere tonight (see the Ollama runner-orphan fixes)."""
        with self._lifecycle_lock:
            process, self._process = self._process, None

        if process is not None and process.poll() is None:
            try:
                process.stdin.close()
            except Exception:
                pass
            try:
                process.terminate()
            except Exception:
                pass


_persistent_piper = _PersistentPiper()


def shutdown_tts():
    _persistent_piper.shutdown()


async def synthesize_speech(text: str):
    """2026-07-16: replaces the old thread/queue/sounddevice pipeline that
    played audio through the SERVER's own speakers. Craig asked for real
    text/speech sync, which meant moving playback into the browser (Web
    Audio API) — the browser can only sync against audio it's actually
    playing itself, not a signal relayed from a separate server-side
    process. core/response_handler.py now sends the returned PCM bytes
    over the same websocket the text goes to, instead of this module
    playing them locally.

    Runs Piper via the persistent process above (_PersistentPiper — see
    its docstring for how utterance boundaries are detected without an
    explicit framing protocol) for this one clause and returns its
    complete raw PCM (16-bit signed, mono, SAMPLE_RATE Hz — Piper's
    --output_raw convention) or None if there's nothing to say or
    synthesis failed.

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
                stdout = await _persistent_piper.synthesize(segment)
            except Exception as e:
                print("TTS error:", e)
                stdout = None
            if stdout:
                pcm_chunks.append(stdout)

        if i < len(segments) - 1:
            pcm_chunks.append(_PAUSE_SILENCE)

    return b"".join(pcm_chunks) if pcm_chunks else None
