# core/voice.py
"""
One way for her to speak.

2026-09-20 (Craig, after a run of faults that all looked different): "it
seems like we're running into a lot of systems clashing. Why are all these
systems stepping on one another? Can we improve this in some way?"

They were clashing because **a turn was not a thing in this codebase.**
Four separate paths could make her speak, and each reimplemented part of
what speaking means while forgetting the rest:

                       lock   wait for   remember   envelope
                              playback   it
    normal reply        yes   n/a         yes        yes
    identity prompts    no    no          no         no
    clarification       no    no          no         no
    proactive push      no    no          no         yes

Every fault that evening is a "no" in that table:

  - two prompts spoken over each other during login        (no lock)
  - her own voice recorded as his verification sample      (no wait)
  - "she acted as though I was the one starting"           (no memory)
  - audio silently dropped after any barge-in              (no envelope)

Each was fixed where it surfaced, one cell at a time, which is how you get
a new fault every time you close one. This module is the table collapsed
into a single function: everything that makes her speak goes through
`say()`, and `say()` owns all four properties at once.

## What does NOT go through this

`core/response_handler.py` still writes its own envelope, and should. It
streams a reply chunk by chunk as the model produces it, so there is no
finished string to hand to `say()` — the whole point of that path is that
the first words are spoken before the last ones exist. It already has all
four properties by other means: it runs under the shared lock via
`process_message`, records through `after_response`, and the client mutes
its own recorder for it. `say()` is for everything that speaks a complete,
already-known line, which is every other path.

## The lock had to be fixed first

`generation_lock` is a plain `asyncio.Lock`, which is not reentrant. The
clarification path runs INSIDE it — `process_message` holds the lock and
calls down into `AudioProcessor.process_end` — so that path could not take
the lock even though it needed to, and simply didn't. That is not an
oversight anyone made twice; it is the only thing that could be done with
a non-reentrant lock.

`SpeechLock` tracks which task holds it, so a nested acquire by the same
task passes straight through while a different task still waits. It keeps
`async with` semantics exactly, so every existing call site works unchanged
and there is one lock rather than one-per-path.
"""
import asyncio

from config.logger_config import logger


class SpeechLock:
    """One utterance at a time, reentrant per task.

    Drop-in for the `asyncio.Lock` this replaced — `async with lock:` reads
    and behaves the same. The difference is that a task already holding it
    does not deadlock on itself, which is what made a single shared lock
    possible at all."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._holder = None

    async def __aenter__(self):
        task = asyncio.current_task()
        if self._holder is task:
            self._reentered = getattr(self, "_reentered", 0) + 1
            return self
        await self._lock.acquire()
        self._holder = task
        return self

    async def __aexit__(self, *exc):
        if getattr(self, "_reentered", 0):
            self._reentered -= 1
            return False
        self._holder = None
        self._lock.release()
        return False

    @property
    def held(self) -> bool:
        return self._holder is not None


# The single lock. ws/ws_handlers.py imports this as `generation_lock` so
# there is exactly one, shared by generation and by every speaking path.
speech_lock = SpeechLock()


# Raw PCM from Piper: 16-bit signed, mono.
from speech.tts_engine import SAMPLE_RATE as _RATE

# Breathing room after audio ends before listening resumes — covers the
# browser's own scheduling latency and stops the tail of her last word
# being recorded as the start of an answer. Untuned starting point.
SETTLE_S = 0.4


def playback_seconds(pcm: bytes) -> float:
    """How long that audio takes to play. The browser never reports
    end-of-playback — it only ever sends the handshake, text, audio,
    __END_AUDIO__ and __INTERRUPT__ — so this is the only way to know, and
    it is exact rather than a guess."""
    return len(pcm) / (_RATE * 2) if pcm else 0.0


async def say(websocket, text: str, *, user_id: str = None,
              remember: bool = True, wait: bool = False,
              speak: bool = True) -> bool:
    """Say something to one connection, properly.

    Owns, in one place, everything a turn has to do:

      * takes the speech lock, so nothing else can speak over it
      * sends the __START__/text/audio/__END__ envelope — outside it the
        browser silently discards audio for the rest of the session once
        the user has interrupted her even once
      * waits out the playback when `wait` is set, so she is not listening
        while she is still audible
      * records it with `remember`, so her next turn knows she spoke

    `wait` defaults to False because the conversational client mutes its
    own recorder during playback and supports barge-in, which is wanted
    there. The handshake passes wait=True: being interrupted while asking
    who you are is exactly what must not happen.

    Returns True if it was delivered.
    """
    from speech.tts_engine import synthesize_speech
    from db.db import remember_own_utterance

    if not text:
        return False

    async with speech_lock:
        try:
            await websocket.send_text("__START__")
            await websocket.send_text(text)

            pcm = await synthesize_speech(text) if speak else None
            if pcm:
                await websocket.send_bytes(pcm)

            await websocket.send_text("__END__")
        except Exception as e:
            logger.warning(f"⚠️ could not say {text[:40]!r}: {e}")
            return False

        if wait and pcm:
            await asyncio.sleep(playback_seconds(pcm) + SETTLE_S)

    if remember and user_id:
        try:
            await remember_own_utterance(user_id, text)
        except Exception as e:
            logger.warning(f"⚠️ could not record her own utterance: {e}")

    return True
