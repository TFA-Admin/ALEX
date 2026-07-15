# Setting up A.L.E.X. on a new machine

This repo is the Python source only. A few things are deliberately **not**
in it, and a few external pieces need to be in place before she'll run.

## Not in this repo (on purpose)

- `db/memory.db` — personal data (facts, conversation memory, voice
  embeddings). The schema auto-creates on first run (`init_db()`); there's
  just nothing in it yet.
- `certs/*.pem` — a local self-signed TLS cert/key. Regenerate your own.
- `config/Logs/` — runtime logs.
- Ollama itself and the pulled model, and the Piper binary + voice model —
  these are large external tools/binaries, not part of the codebase.

## 1. Prerequisites

- Python 3.12
- [Ollama](https://ollama.com), with the model pulled: `ollama pull qwen2.5:7b`
  (set `ALEX_LLM_MODEL` if you use a different tag/size)
- [Piper](https://github.com/rhasspy/piper) binary + a voice model (this
  project uses the GLaDOS voice from
  [DavesArmoury/GLaDOS_TTS](https://huggingface.co/DavesArmoury/GLaDOS_TTS),
  CC-BY-4.0)

## 2. Install Python dependencies

```
pip install -r requirements.txt
```

## 3. Point ALEX at Ollama / Piper

Defaults assume this machine's original layout (`D:\project_ALEX\...`).
On a different machine, set these environment variables instead of editing
code:

| Variable | Used by | Default |
|---|---|---|
| `ALEX_OLLAMA_EXE` | `ALEX_Controller.py` (Start Ollama button) | `D:/project_ALEX/Ollama/ollama.exe` |
| `ALEX_PIPER_PATH` | `speech/tts_engine.py` | `D:\project_ALEX\piper\piper.exe` |
| `ALEX_PIPER_MODEL` | `speech/tts_engine.py` | `D:\project_ALEX\piper\models\glados_piper_medium.onnx` |
| `ALEX_STT_MODEL` | `speech/stt_engine.py` | `base` (faster-whisper model size) |
| `ALEX_LLM_MODEL` | `llm/ollama_client.py` | `qwen2.5:7b` (Ollama model tag) |

(If you launch `ALEX.py` directly rather than through
`ALEX_Controller.py`, Ollama just needs to already be running —
`llm/ollama_client.py` connects to `http://127.0.0.1:11434`, no path
needed for that.)

## 4. Pre-cache the ML models (one-time, needs network)

ALEX is designed to **never** reach the network once running —
`speech/stt_engine.py` and `core/embedding_engine.py` both force
`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`. That means on a brand new
machine, the two Hugging Face–backed models need to already be cached
locally, or the very first run will fail to fetch them:

- `sentence-transformers` model `all-MiniLM-L6-v2` (used for memory
  similarity)
- `faster-whisper` model, size set by `ALEX_STT_MODEL` (default `base`)

Easiest path: temporarily unset `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`
(or comment out those two lines) for one run with internet access so the
models download into the normal Hugging Face cache
(`~/.cache/huggingface`), then restore offline mode. Resemblyzer (voice
ID) doesn't need this step — its model ships inside the pip package.

## 5. Create the creator profile

```
python bootstrap_creator.py <your name>
```

This is deliberately a standalone script — granting the `creator` role
can only happen with local access to the machine, never over chat/WS.

## 6. Run

```
python -X utf8 ALEX.py
```

or launch `ALEX_Controller.py` (PySide6 desktop GUI) and use its Start
buttons, which also gives you live logs, connection tracking, and a
Personality tab for resets.
