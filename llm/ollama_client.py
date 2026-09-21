# llm/ollama_client.py

"""
Ollama Client (Clean Architecture)

Responsibilities:
- Ensure Ollama is running
- Send prompt to model
- Stream response back

NO:
- memory logic
- fact logic
- intent parsing
- embedding logic
"""

import os
import httpx
import json
import asyncio  # 🔥 REQUIRED

locked_fields = {}
pending_profile_changes = {}

# Swapped from mistral to qwen2.5:7b (2026-07-15) — mistral repeatedly
# produced unreliable JSON extraction (chat-template artifacts leaking into
# extracted values) under Ollama's JSON-constrained decoding. Env var lets
# this be changed without editing code.
DEFAULT_MODEL = os.getenv("ALEX_LLM_MODEL", "qwen2.5:7b")

# All three methods below must share this same num_ctx — confirmed live
# (2026-07-16) that Ollama fully reloads the model (~8s) any time num_ctx
# changes between calls, even for the same model. generate_json (used by
# every classifier — module-gap check, intent classification, personality
# reflection) previously used 512, generate_stream (real chat replies) used
# 1024, generate_text (inquiry synthesis) used 2048 — so a normal turn
# hitting a classifier then generation back to back paid that ~8s reload
# cost twice, every single turn. 4096 chosen from real measured prompt
# sizes (the LLM system prompt alone runs ~957 tokens before any real
# facts/memory content, self-reflection's real prompt measures 825) with
# margin for growth, verified affordable against live free VRAM.
SHARED_NUM_CTX = 4096

# Pool-level default only. Every method still passes its own per-request
# timeout, which is what actually applies — this just has to be large
# enough not to bound the streaming path.
DEFAULT_TIMEOUT_S = 300.0

# Same reasoning, same fix, different parameter — found live (2026-07-16)
# via the exact same reload-thrashing test used to discover the num_ctx
# bug above: generate_stream() was the only method setting num_batch (64);
# generate_json()/generate_text() left it unset (Ollama's own default,
# effectively a different value), so every classifier call before a
# generation call — i.e. nearly every real turn — paid the same ~8s
# reload cost via THIS parameter instead, even after num_ctx was unified.
# Confirmed directly: three calls (no num_batch -> num_batch=64 -> no
# num_batch again) reloaded on both switches, staying fast only when the
# value didn't change between consecutive calls.
#
# 512 (not 64) after a second real measurement: with reload eliminated,
# the real LLM system prompt (~1036 tokens) still took a genuine 8.01s to
# prefill at num_batch=64, vs 3.98s at num_batch=512 (Ollama's own
# default) — confirmed via prompt_eval_duration specifically, not
# confounded by reload cost. num_batch only affects how many PROMPT
# tokens get batched during prefill, not how output tokens stream one at
# a time, so this doesn't trade away streaming smoothness — it was
# picked as 64 for streaming's sake but only prefill throughput is
# actually affected by it.
SHARED_NUM_BATCH = 512


class OllamaManager:

    def __init__(self):
        self.host = "http://127.0.0.1:11434"
        self.ready = False
        self._client = None

    # -------------------------
    # SHARED CONNECTION (2026-09-20)
    # -------------------------
    def _get_client(self) -> httpx.AsyncClient:
        """One pooled client for the process instead of a fresh
        `httpx.AsyncClient` per call.

        Measured, not assumed. Craig asked where the ~5s turn goes; the
        logs said intent classification 1.00s mean, and a *minimal* prompt
        ("Reply with only {"x":1}") through generate_json cost 0.76s — so
        the prompt was never the cost. Isolating the call:

            new client per call, 127.0.0.1 : 0.78 0.84 0.80 0.79
            one reused client, 127.0.0.1   : 0.45 0.43 0.44 0.46

        **~0.33s per call is TCP connection setup**, paid on every single
        Ollama request in the process. Ollama's own reported
        total_duration for that request is 378ms, so roughly half of every
        short classification call was connection overhead.

        This is per-request, so it applies to `generate_stream` too — the
        turn's time-to-first-chunk (1.31s mean) pays the same 0.33s before
        a single token is generated. Expected saving on an ordinary turn
        is intent + generation, i.e. ~0.6s off 5.01s.

        Created lazily because a client must be bound to the running event
        loop, and re-created if something closed it. Timeouts stay
        per-request (httpx allows it), so the 300s streaming budget and
        the 15-20s classification budgets are unchanged."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT_S,
                # keepalive_expiry is deliberately finite. Ollama sets no
                # idle timeout today, but if a future version does, a
                # connection the pool believes is alive would fail on the
                # next send. Recycling once a minute makes that window
                # small, and the retry below covers what is left.
                limits=httpx.Limits(max_keepalive_connections=8,
                                    max_connections=16,
                                    keepalive_expiry=60.0),
            )
        return self._client

    async def aclose(self):
        """Not called on the normal path — the process outliving the pool
        is fine. Exists so tests and the harness can release sockets
        deterministically."""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None

    async def init(self, timeout: float = None) -> bool:
        """Waits for Ollama. Returns True once it answers.

        `timeout=None` waits forever, which is right at startup — main.py
        blocks on this before serving, and there is nothing useful to do
        until the model is up.

        **A number bounds the wait, and every generate_* path passes one.**
        2026-09-20: this was an unbounded `while True` on every path,
        including mid-conversation. If Ollama went away — crashed,
        restarting, being swapped — the next generation call blocked
        forever with no error, no timeout and no log beyond "Waiting for
        Ollama" every two seconds. From outside, she simply stopped
        answering.

        Craig hit exactly that ("she's seemingly stopped responding") on
        the same day get_phrase() started composing every scripted line
        through generate_json, which put this landmine in front of the
        connect handshake rather than only in front of a chat reply. The
        bug was older than that change; the change is what made it easy to
        stand on. tools/memory_hygiene.py had already had to work around
        it locally an hour earlier — which should have been the signal to
        fix it here instead."""
        deadline = None if timeout is None else (
            asyncio.get_event_loop().time() + timeout)

        while True:
            try:
                r = await self._get_client().get(self.host, timeout=2.0)
                if r.status_code == 200:
                    self.ready = True
                    print("✅ Ollama ready")
                    return True
            except Exception:
                pass

            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                print(f"⚠️ Ollama unreachable after {timeout:.0f}s — giving up on this call")
                return False

            print("⏳ Waiting for Ollama...")
            await asyncio.sleep(2)

    # How long a single generation call will wait for a missing Ollama
    # before failing instead of hanging. Short on purpose: if it is not
    # there within this, it is not coming back inside the life of one
    # request, and every caller has a fallback for None.
    READY_WAIT_S = 5.0

    async def generate_stream(self, prompt: str, model: str = DEFAULT_MODEL,
                               model_override: str = None, raw_mode: bool = False):

        if not self.ready and not await self.init(timeout=self.READY_WAIT_S):
            # Fails as a normal failure rather than hanging. Streaming
            # raises (its consumer already handles a dead generation);
            # the single-shot paths fall through to their own
            # try/except and return None, which every caller expects.
            raise RuntimeError("Ollama is not reachable")

        active_model = model_override or model

        if raw_mode:
            # Plain text completion, no chat template — used for code continuation.
            url = f"{self.host}/api/generate"
            payload = {
                "model": active_model,
                "prompt": prompt,
                "raw": True,
                "stream": True,
                "options": {
                    "num_ctx": SHARED_NUM_CTX,
                    "num_batch": SHARED_NUM_BATCH,
                    # See the chat-mode branch below for why this cap exists.
                    "num_predict": 300
                }
            }
        else:
            url = f"{self.host}/api/chat"
            payload = {
                "model": active_model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "stream": True,
                "options": {
                    "num_ctx": SHARED_NUM_CTX,
                    "num_batch": SHARED_NUM_BATCH,
                    # 300 tokens is generous for a normal conversational
                    # reply (confirmed real replies run ~80-150 tokens) —
                    # bounds worst-case rambling/verbosity without cutting
                    # off a normal answer. Not present before tonight;
                    # nothing capped how long she could keep generating.
                    "num_predict": 300
                }
            }

        # Shared pooled client — see _get_client(). A generation is the one
        # call where the ~0.33s saved connection setup is visible to the
        # user directly, since it comes off time-to-first-chunk.
        #
        # The retry exists only because the connection is now reused: a
        # pooled socket the server has quietly dropped fails on send, which
        # a fresh-client-per-call design could never hit. It retries ONCE,
        # and only while nothing has been yielded yet — re-running a
        # generation that already streamed half an answer would replay text
        # the user has already heard. `yielded` is what enforces that.
        attempt = 0

        while True:
            yielded = False
            client = self._get_client()

            try:
                async with client.stream("POST", url, json=payload,
                                         timeout=300.0) as response:

                    buffer = ""

                    async for raw_chunk in response.aiter_raw():

                        if not raw_chunk:
                            await asyncio.sleep(0)
                            continue

                        try:
                            buffer += raw_chunk.decode("utf-8")
                        except:
                            continue

                        # 🔥 Process complete JSON lines
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)

                            if not line.strip():
                                continue

                            try:
                                data = json.loads(line)

                                if raw_mode:
                                    content = data.get("response", "")
                                else:
                                    content = data.get("message", {}).get("content", "")

                                if content:
                                    yielded = True
                                    yield content

                            except Exception:
                                # partial / malformed JSON → ignore safely
                                continue

                        # 🔥 CRITICAL: yield control to event loop
                        await asyncio.sleep(0)

                return

            except (httpx.RemoteProtocolError, httpx.ConnectError,
                    httpx.ReadError, httpx.WriteError) as e:
                if yielded or attempt >= 1:
                    raise
                attempt += 1
                print(f"⚠️ Ollama stream connection dropped ({e}) — reconnecting once")
                await self.aclose()

    async def generate_json(self, prompt: str, model: str = DEFAULT_MODEL, timeout: float = 15.0,
                             temperature: float = None):
        """
        Single-shot (non-streaming) call for short structured-extraction
        tasks (name parsing, fact extraction, command parameters) — these
        don't need the streaming path, just a bounded, fast round-trip.

        temperature=None leaves Ollama's default sampling in place (some
        callers, like self-reflection's personality evolution, want that
        variance). Pass temperature=0 for calls where the same input should
        reliably produce the same classification — confirmed via repeated
        live testing that intent classification could flip between correct
        and wrong output on an identical input at default sampling.

        Returns the parsed dict, or None on any failure (bad JSON, timeout,
        Ollama unavailable). Callers MUST have a safe fallback for None —
        this is an interpretation aid, not a source of truth, and the
        actual security-relevant decisions must never depend solely on it.
        """
        if not self.ready and not await self.init(timeout=self.READY_WAIT_S):
            # Fails as a normal failure rather than hanging. Streaming
            # raises (its consumer already handles a dead generation);
            # the single-shot paths fall through to their own
            # try/except and return None, which every caller expects.
            raise RuntimeError("Ollama is not reachable")

        options = {
            "num_ctx": SHARED_NUM_CTX,
            "num_batch": SHARED_NUM_BATCH,
            "num_predict": 200
        }

        if temperature is not None:
            options["temperature"] = temperature

        try:
            r = await self._get_client().post(
                f"{self.host}/api/chat",
                timeout=timeout,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": "json",
                    "options": options
                }
            )
            data = r.json()
            content = data.get("message", {}).get("content", "")
            return json.loads(content)

        except Exception as e:
            print(f"⚠️ generate_json failed: {e}")
            return None

    async def generate_text(self, prompt: str, model: str = DEFAULT_MODEL, timeout: float = 30.0,
                             temperature: float = None, num_predict: int = 400):
        """
        Single-shot (non-streaming) plain-text call — same shape as
        generate_json() but without the JSON-format constraint, for
        callers that want one complete string back, not a stream and not
        structured extraction. First use: the inquiry module's grounded
        synthesis (2026-07-16) — summarizing real fetched search content
        into one answer, not free generation, so a longer num_predict
        default than generate_json's 200 (a summary needs more room than
        a short classification).

        Returns the plain response string, or None on any failure —
        same "caller must have a safe fallback" contract as generate_json.
        """
        if not self.ready and not await self.init(timeout=self.READY_WAIT_S):
            # Fails as a normal failure rather than hanging. Streaming
            # raises (its consumer already handles a dead generation);
            # the single-shot paths fall through to their own
            # try/except and return None, which every caller expects.
            raise RuntimeError("Ollama is not reachable")

        options = {
            "num_ctx": SHARED_NUM_CTX,
            "num_batch": SHARED_NUM_BATCH,
            "num_predict": num_predict
        }

        if temperature is not None:
            options["temperature"] = temperature

        try:
            r = await self._get_client().post(
                f"{self.host}/api/chat",
                timeout=timeout,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": options
                }
            )
            data = r.json()
            return data.get("message", {}).get("content", "") or None

        except Exception as e:
            print(f"⚠️ generate_text failed: {e}")
            return None


ollama_manager = OllamaManager()