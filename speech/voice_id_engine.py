# speech/voice_id_engine.py
"""
Speaker verification. Uses Resemblyzer, whose pretrained model ships inside
the installed package itself (site-packages/resemblyzer/pretrained.pt) —
loading it never touches the network, unlike the Hugging Face models used
elsewhere in the project.
"""
import os
import tempfile

import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav
from faster_whisper.audio import decode_audio

# Real-world evidence (compressed webm mic audio, short utterances) shows
# genuine same-speaker scores landing anywhere from ~0.65 to ~0.99 — 0.75
# produced repeated false negatives on real speech. Lowered with continuous
# reinforcement (db.reinforce_voice_sample) picking up the slack over time
# as more confirmed-genuine samples accumulate per profile.
MATCH_THRESHOLD = 0.68
MIN_AUDIO_BYTES = 6000

_encoder = None


def _get_encoder():
    global _encoder
    if _encoder is None:
        _encoder = VoiceEncoder()
    return _encoder


def embed_voice_bytes(audio_bytes: bytes):
    """Returns a 256-d speaker embedding for a voice clip, or None if unusable."""
    if not audio_bytes or len(audio_bytes) < MIN_AUDIO_BYTES:
        return None

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(audio_bytes)
        path = f.name

    try:
        wav = decode_audio(path)
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    try:
        processed = preprocess_wav(wav, source_sr=16000)
    except Exception:
        return None

    if processed is None or len(processed) == 0:
        return None

    return _get_encoder().embed_utterance(processed)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def best_match(candidate: np.ndarray, enrolled: list) -> float:
    """Highest similarity between candidate and any of a profile's enrolled samples."""
    if candidate is None or not enrolled:
        return 0.0

    return max(_cosine(candidate, e) for e in enrolled)


def is_match(candidate: np.ndarray, enrolled: list, threshold: float = MATCH_THRESHOLD) -> bool:
    return best_match(candidate, enrolled) >= threshold


def identify_speaker(candidate: np.ndarray, profiles: dict, threshold: float = MATCH_THRESHOLD):
    """
    profiles: {owner: [embeddings...]}
    Returns (owner, score) for the best match at/above threshold, or (None, best_score_seen).
    """
    if candidate is None or not profiles:
        return None, 0.0

    best_owner = None
    best_score = 0.0

    for owner, embeddings in profiles.items():
        score = best_match(candidate, embeddings)

        if score > best_score:
            best_score = score
            best_owner = owner

    if best_score >= threshold:
        return best_owner, best_score

    return None, best_score
