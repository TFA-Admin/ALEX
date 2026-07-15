# embedding_engine.py
import os

# ALEX must never reach the network. Force the Hugging Face stack to only
# ever use the local model cache and fail loudly instead of phoning home.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from sentence_transformers import SentenceTransformer
import numpy as np

# Preload model
model = SentenceTransformer("all-MiniLM-L6-v2")

def embed(text):
    return model.encode(text)

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))