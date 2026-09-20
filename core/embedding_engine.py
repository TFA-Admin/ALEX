# embedding_engine.py
import os

# ALEX must never reach the network. Force the Hugging Face stack to only
# ever use the local model cache and fail loudly instead of phoning home.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from sentence_transformers import SentenceTransformer
import numpy as np
import torch

# 2026-09-20 (RTX 3080 swap): this had no device argument, so
# sentence-transformers defaulted it to CPU and every embedding — memory
# writes, vector recall on each turn — competed with Piper TTS and (until
# today) Whisper for the same 4-core i5-7600K. MiniLM-L6 is small enough
# that this was never the headline cost, but it is free to move now and
# the CPU has other work. Falls back to CPU when CUDA is unavailable
# rather than failing, matching speech/stt_engine.py.
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# Preload model
model = SentenceTransformer("all-MiniLM-L6-v2", device=_DEVICE)

def embed(text):
    return model.encode(text)

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))