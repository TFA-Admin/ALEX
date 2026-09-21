import tempfile
import os

# ALEX must never reach the network. Force the Hugging Face stack to only
# ever use the local model cache and fail loudly instead of phoning home.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch
import re
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

# 2026-09-20: base → distil-large-v3, the intended use of the VRAM freed
# by the 3080 swap. An English-only distillation of large-v3 at roughly
# half the size and twice the speed — chosen over large-v3 itself because
# this model now shares a 10GB card with the LLM. English-only is not a
# constraint ALEX hits today, but it IS one: supporting another language
# means going back to a multilingual model (large-v3 or medium).
def _stt_model_from_controller_settings() -> str:
    """2026-09-21: config/controller_settings.json can name the STT model
    too (key "alex_stt_model"), so the choice travels with the LLM choice
    and holds however she is launched. ALEX_STT_MODEL in the environment
    still wins. Why it exists: qwen3.5:9b plus distil-large-v3 fills the
    card to 9961 of 10240 MiB (the 9b's K/V cache runs at ~1.3 GiB under
    Ollama's new engine, which does not quantize it), and CPU
    transcription is not an option — distil-large-v3 int8 measured ~9s
    for 6s of speech on this CPU against 0.3s on the GPU. `base` costs
    ~0.2 GiB on the card and produced identical text on every synthetic
    test in the 2026-09-20 A/B; its confidence scale differs, which
    ws/ws_audio.py accounts for."""
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "controller_settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return str(json.load(fh).get("alex_stt_model", "")).strip()
    except (OSError, ValueError, AttributeError):
        return ""


MODEL_SIZE = (os.getenv("ALEX_STT_MODEL") or _stt_model_from_controller_settings()
              or "distil-large-v3")
STT_DEVICE = os.getenv("ALEX_STT_DEVICE", "auto").strip().lower()
MIN_AUDIO_BYTES = 6000


# -------------------------
# LOAD MODEL
# -------------------------
#
# 2026-09-20 (RTX 3080 swap): STT runs on the GPU for the first time.
# A FORCE_STT_CPU flag used to sit here, pinned True. CTranslate2
# (faster-whisper's backend) is a separate CUDA stack from raw PyTorch
# with narrower hardware support, and on the old GTX Titan X (compute
# capability 5.2, Maxwell-era) it loaded and ran without error but
# silently produced empty/garbage transcriptions instead of failing
# cleanly. That was a cc 5.2 problem specifically — CTranslate2 fully
# supports Ampere (cc 8.6) — so the flag is deleted rather than flipped;
# leaving it in place would only invite someone to wonder what it guarded.
#
# compute_type has the same history: float32 was the correct choice on
# Maxwell, which ran fp16 at 1/64 rate. Ampere has real fp16 tensor
# cores, so float16 is roughly 2x faster at half the VRAM. The fallback
# below still drops to CPU int8 if CUDA is missing or the load fails.


def _announce(msg: str):
    """2026-09-20: these lines used to be bare prints with emoji in them.
    The Controller launches with `python -X utf8`, so that was fine in
    production — but under a plain cp1252 console the print ITSELF raises
    UnicodeEncodeError, which the handler below then caught and reported as
    "GPU failed, falling back to CPU." A logging failure was being
    misdiagnosed as a hardware one, and the fallback's own emoji then
    killed the process outright. Encoding can never be the reason STT
    appears to fail."""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def load_model():
    # Load FIRST, announce after. If the announcement is inside the try
    # alongside the load, a print error is indistinguishable from a real
    # model-load error — which is exactly what went wrong before.
    try:
        # 2026-09-21: ALEX_STT_DEVICE=cpu keeps Whisper off the card. Not
        # the old FORCE_STT_CPU (a Maxwell bug workaround, deleted); this
        # is VRAM budgeting for a model comparison: qwen3.5:9b is 6.1 GiB
        # against 4.4 for qwen2.5:7b, and with distil-large-v3 resident
        # the two together sit at ~9.4 of 10 GiB. The harness drives her
        # over text, so STT on the CPU during a comparison costs nothing
        # it measures. Unset or "auto" keeps the GPU.
        if torch.cuda.is_available() and STT_DEVICE != "cpu":
            model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
            _announce(f"Using GPU for STT ({MODEL_SIZE}, float16)")
            return model
        model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        _announce(f"Using CPU for STT ({MODEL_SIZE}, int8)")
        return model
    except Exception as e:
        _announce(f"GPU STT load failed, falling back to CPU ({MODEL_SIZE}, int8): {e}")
        return WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")


model = load_model()


# -------------------------
# TRANSCRIBE (SAFE DECODE)
# -------------------------
def transcribe_audio(audio_bytes: bytes):
    """Returns (text, avg_logprob). avg_logprob is faster-whisper's own
    per-segment acoustic-confidence score (mean across segments when an
    utterance has more than one), None when nothing was transcribed.

    2026-07-18 (Craig: "tell me no" got misheard as "tell me now" — asked
    whether she can check her own confidence and ask instead of guessing).
    Real reference points, not guessed: a clear synthetic-speech test
    (Piper-generated audio fed back through this exact model/settings)
    measured avg_logprob ≈ -0.31; `log_prob_threshold=-1.0` below is
    Whisper's OWN internal decoding-retry cutoff. ws/ws_audio.py's
    LOW_CONFIDENCE_THRESHOLD sits between those two as a starting point
    for "noticeably less sure than a clear utterance" — like every other
    untuned threshold added tonight, this can't be verified against a
    genuinely ambiguous REAL recording without live use.

    2026-09-20: the -0.31 figure above is specific to `base`. Re-running
    the same Piper round-trip against distil-large-v3 measured roughly
    -0.06 to -0.22 (mean ~-0.14) on identically clear audio — the whole
    distribution shifted up, because the model is genuinely more certain.
    LOW_CONFIDENCE_THRESHOLD was rescaled to match; a threshold tuned for
    one model is not portable to another."""
    try:
        # 🔥 ignore tiny / broken chunks
        if not audio_bytes or len(audio_bytes) < MIN_AUDIO_BYTES:
            return "", None

        # ---------------- SAVE TEMP FILE ----------------
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(audio_bytes)
            path = f.name

        try:
            # ---------------- SAFE DECODE ----------------
            audio = decode_audio(path)
        except Exception as e:
            print("⚠️ decode failed, skipping chunk:", e)
            try:
                os.remove(path)
            except:
                pass
            return "", None

        # ---------------- TRANSCRIBE ----------------
        segments, _ = model.transcribe(
            audio,
            language="en",
            beam_size=5,
            temperature=0.0,
            vad_filter=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.8,  # raised from 0.6 — was discarding quieter speech as silence
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )

        # Materialized once — this drives both the text join and the
        # confidence average below, and the underlying generator can only
        # be consumed once.
        segments = list(segments)

        text = "".join(seg.text for seg in segments).strip()
        avg_logprob = (
            sum(seg.avg_logprob for seg in segments) / len(segments)
            if segments else None
        )

        # 🔥 NEW: reject garbage outputs (PUT IT RIGHT HERE)
        if not re.search(r'[a-zA-Z]{2,}', text):
            try:
                os.remove(path)
            except:
                pass
            return "", None

        # ---------------- CLEANUP ----------------
        try:
            os.remove(path)
        except:
            pass

        return text, avg_logprob

    except Exception as e:
        print("STT error:", e)
        return "", None
