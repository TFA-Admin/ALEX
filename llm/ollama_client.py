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
def _model_from_controller_settings() -> str:
    """2026-09-21: the Controller's model selector writes
    config/controller_settings.json; read it here too, so the choice holds
    however she is launched (an older Controller build, ALEX.py by hand,
    the harness driver). ALEX_LLM_MODEL in the environment still wins."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "controller_settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh).get("alex_llm_model", "")
        return str(value).strip()
    except (OSError, ValueError, AttributeError):
        return ""


DEFAULT_MODEL = (os.getenv("ALEX_LLM_MODEL") or _model_from_controller_settings()
                 or "qwen2.5:7b")

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
# 2026-09-21 (Craig approved): 8192. The 4096 above was chosen with margin
# for a ~960-token system prompt; the real prompt now measures ~2700 tokens
# before memory, so two thirds of what she could see was instruction and
# the 12-turn memory window had to be size-capped to keep the front of the
# prompt from being truncated. K/V at q8_0 measured 119 MiB at 4096, so
# this costs ~120 MiB more against ~2.6 GB of headroom with Whisper
# resident. Prefill cost depends on tokens actually sent, not on num_ctx,
# so an ordinary turn is not slower. Tool results (the next build) need
# the room as well.
# Per-model, because the K/V cache is not the same size per token across
# models. Measured 2026-09-21 from Ollama's own load report, q8_0, 8192:
# qwen2.5:7b K/V 224 MiB; qwen3.5:9b K/V 1.4 GiB plus a 767 MiB compute
# graph, 8.3 GiB in all. With distil-large-v3 resident (2.2 GiB, and it
# has to be: CPU int8 transcription measured ~9s for 6s of speech on this
# CPU against 0.3s on the card) the 9b at 8192 left the card at 9975 of
# 10240 MiB — everything offloaded, no headroom for Whisper's working
# memory. 6144 keeps ~2000 tokens spare over the ~2700-token prompt, the
# 1000-token memory window and a 300-token reply, and returns ~0.6 GiB.
# ALEX_NUM_CTX in the environment overrides both.
_NUM_CTX_BY_MODEL = {"qwen3.5:9b": 6144}
SHARED_NUM_CTX = int(os.getenv("ALEX_NUM_CTX") or _NUM_CTX_BY_MODEL.get(DEFAULT_MODEL, 8192))

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

# 2026-09-21: keep the model resident. Ollama's default keep_alive unloads a
# model after five idle minutes, and the first request afterwards pays a
# full reload. Measured in her own logs: after a ~30 minute pause the
# runner restarted at 02:33:49, the 4.1 GiB of weights loaded again, and
# the intent classification for that turn took 17.25s against its usual
# ~0.7s — sixteen seconds of silence on the first thing said after a
# break, every time. core/readiness.py already warms the model at startup
# on exactly this reasoning; this stops it going cold again. -1 means
# "indefinitely". It is sent per request, not set in the environment,
# because OLLAMA_MAX_LOADED_MODELS taught us a machine-level variable can
# silently outrank the Controller's. Model swaps still work: with one
# model slot, loading another evicts this one regardless.
KEEP_ALIVE = -1

# Qwen3-family models default to thinking mode ON: hundreds of hidden
# tokens before the first visible one, several seconds on a voice turn.
# Off for every conversational call, so a model swap measures the model and
# not its scratchpad; the deliberation pass (roadmap item 4) is where
# thinking is switched on deliberately, per call. Sent only for models that
# understand the parameter — Ollama rejects `think` on models that do not.
_THINK_KW = {"think": False} if DEFAULT_MODEL.lower().startswith("qwen3") else {}


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
                "keep_alive": KEEP_ALIVE,
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
                "keep_alive": KEEP_ALIVE,
                **_THINK_KW,
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

    async def chat_stream(self, messages, tools=None, model: str = DEFAULT_MODEL):
        """Streams a chat turn built from a real message list, optionally
        with tools she may call. Yields ("text", str) for content and
        ("tool_calls", list) when the model decides to call something.

        2026-09-21, roadmap item 1. Kept separate from generate_stream():
        that one takes a bare prompt and yields bare strings, and three
        callers depend on it. This one is what systems/llm/system.py uses
        for the tool loop — see core/tools.py for what she can call and
        why the set is what it is. Same pooled client, same keep_alive,
        same thinking switch, same reconnect-once rule."""
        if not self.ready and not await self.init(timeout=self.READY_WAIT_S):
            raise RuntimeError("Ollama is not reachable")

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            **_THINK_KW,
            "options": {
                "num_ctx": SHARED_NUM_CTX,
                "num_batch": SHARED_NUM_BATCH,
                "num_predict": 300,
            },
        }
        if tools:
            payload["tools"] = tools

        attempt = 0
        while True:
            yielded = False
            client = self._get_client()
            try:
                async with client.stream("POST", f"{self.host}/api/chat", json=payload,
                                         timeout=300.0) as response:
                    buffer = ""
                    async for raw_chunk in response.aiter_raw():
                        if not raw_chunk:
                            await asyncio.sleep(0)
                            continue
                        try:
                            buffer += raw_chunk.decode("utf-8")
                        except Exception:
                            continue
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            if not line.strip():
                                continue
                            try:
                                data = json.loads(line)
                            except Exception:
                                continue
                            if data.get("error"):
                                raise RuntimeError(str(data["error"]))
                            msg = data.get("message") or {}
                            calls = msg.get("tool_calls")
                            if calls:
                                yielded = True
                                yield ("tool_calls", calls)
                            content = msg.get("content", "")
                            if content:
                                yielded = True
                                yield ("text", content)
                        await asyncio.sleep(0)
                return
            except (httpx.RemoteProtocolError, httpx.ConnectError,
                    httpx.ReadError, httpx.WriteError) as e:
                if yielded or attempt >= 1:
                    raise
                attempt += 1
                print(f"⚠️ Ollama chat stream connection dropped ({e}) — reconnecting once")
                await self.aclose()

    async def generate_json(self, prompt: str, model: str = DEFAULT_MODEL, timeout: float = 15.0,
                             temperature: float = None, think: bool = None, num_predict: int = 200):
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
            "num_predict": num_predict
        }

        if temperature is not None:
            options["temperature"] = temperature

        # 2026-09-21: think=None keeps the model's default as this file
        # sets it (_THINK_KW: off for qwen3, because thinking costs 20-30s
        # a turn and is unusable for conversation). think=True is for work
        # nobody is waiting on — core/idle_author.py — and needs the
        # budget that comes with it (num_predict), since the thinking
        # tokens are counted against the same limit.
        think_kw = dict(_THINK_KW)
        if think is not None and model.lower().startswith("qwen3"):
            think_kw = {"think": bool(think)}

        try:
            r = await self._get_client().post(
                f"{self.host}/api/chat",
                timeout=timeout,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": KEEP_ALIVE,
                    **think_kw,
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
                    "keep_alive": KEEP_ALIVE,
                    **_THINK_KW,
                    "options": options
                }
            )
            data = r.json()
            return data.get("message", {}).get("content", "") or None

        except Exception as e:
            print(f"⚠️ generate_text failed: {e}")
            return None


ollama_manager = OllamaManager()