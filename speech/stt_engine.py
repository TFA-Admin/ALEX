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

MODEL_SIZE = os.getenv("ALEX_STT_MODEL", "base")
MIN_AUDIO_BYTES = 6000


# -------------------------
# LOAD MODEL
# -------------------------
#
# CTranslate2 (faster-whisper's backend) is a separate CUDA stack from raw
# PyTorch, with narrower hardware support. On this machine's GTX Titan X
# (compute capability 5.2, Maxwell-era) it loads and runs without error but
# silently produces empty/garbage transcriptions instead of a clean failure —
# confirmed by real captured audio consistently transcribing to nothing on
# GPU. Force CPU here regardless of torch.cuda.is_available(); the other
# GPU-eligible models (sentence-transformers, resemblyzer) use plain PyTorch
# and don't have this problem.
FORCE_STT_CPU = True


def load_model():
    try:
        if torch.cuda.is_available() and not FORCE_STT_CPU:
            print("🧠 Using GPU for STT")
            return WhisperModel(MODEL_SIZE, device="cuda", compute_type="float32")
        else:
            print("🧠 Using CPU for STT")
            return WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    except Exception as e:
        print("⚠️ GPU failed, falling back to CPU:", e)
        return WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")


model = load_model()


# -------------------------
# TRANSCRIBE (SAFE DECODE)
# -------------------------
def transcribe_audio(audio_bytes: bytes):
    try:
        # 🔥 ignore tiny / broken chunks
        if not audio_bytes or len(audio_bytes) < MIN_AUDIO_BYTES:
            return ""

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
            return ""

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

        text = "".join([seg.text for seg in segments]).strip()

        # 🔥 NEW: reject garbage outputs (PUT IT RIGHT HERE)
        if not re.search(r'[a-zA-Z]{2,}', text):
            try:
                os.remove(path)
            except:
                pass
            return ""

        # ---------------- CLEANUP ----------------
        try:
            os.remove(path)
        except:
            pass

        return text
    
    except Exception as e:
        print("STT error:", e)
        return ""
