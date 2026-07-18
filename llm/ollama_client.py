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

    async def init(self):
        while True:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get(self.host)
                    if r.status_code == 200:
                        self.ready = True
                        print("✅ Ollama ready")
                        return
            except:
                pass

            print("⏳ Waiting for Ollama...")
            await asyncio.sleep(2)

    async def generate_stream(self, prompt: str, model: str = DEFAULT_MODEL,
                               model_override: str = None, raw_mode: bool = False):

        if not self.ready:
            await self.init()

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

        async with httpx.AsyncClient(timeout=300.0) as client:

            async with client.stream("POST", url, json=payload) as response:

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
                                yield content

                        except Exception:
                            # partial / malformed JSON → ignore safely
                            continue

                    # 🔥 CRITICAL: yield control to event loop
                    await asyncio.sleep(0)

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
        if not self.ready:
            await self.init()

        options = {
            "num_ctx": SHARED_NUM_CTX,
            "num_batch": SHARED_NUM_BATCH,
            "num_predict": 200
        }

        if temperature is not None:
            options["temperature"] = temperature

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"{self.host}/api/chat",
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
        if not self.ready:
            await self.init()

        options = {
            "num_ctx": SHARED_NUM_CTX,
            "num_batch": SHARED_NUM_BATCH,
            "num_predict": num_predict
        }

        if temperature is not None:
            options["temperature"] = temperature

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(
                    f"{self.host}/api/chat",
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