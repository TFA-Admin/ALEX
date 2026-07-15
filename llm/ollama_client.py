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

import httpx
import json
import asyncio  # 🔥 REQUIRED

locked_fields = {}
pending_profile_changes = {}


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

    async def generate_stream(self, prompt: str, model: str = "mistral",
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
                    "num_ctx": 1024,
                    "num_batch": 64
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
                    "num_ctx": 1024,
                    "num_batch": 64
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

    async def generate_json(self, prompt: str, model: str = "mistral", timeout: float = 15.0,
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
            "num_ctx": 512,
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


ollama_manager = OllamaManager()