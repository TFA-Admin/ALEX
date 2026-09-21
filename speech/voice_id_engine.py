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

# 2026-09-20 (Craig): "I'm guessing she is picking up on voice activity
# happening in the background. Can we make her able to distinguish between
# me talking and background noise?"
#
# Deliberately NOT MATCH_THRESHOLD. That number answers "is this person who
# they claim to be", where the cost of a wrong yes is granting creator
# trust. This one answers "was this even the person I am talking to", where
# the costs are the other way round:
#
#   false drop   — his real speech is ignored and she looks broken. This is
#                  the complaint that already exists ("she did not
#                  respond"), and it is the expensive one.
#   false accept — a background voice gets answered. Annoying, and no worse
#                  than what happens today with no check at all.
#
# So this sits well BELOW the verification threshold and only rejects audio
# that clearly is not him. Anything between the two is treated as his,
# because a near-miss on a short or noisy utterance is far more likely to be
# him than a stranger who happens to score 0.6.
#
# Measured against the real enrolled embeddings in her database — craig's
# 15 samples and cheryl's 3, each scored against the other's profile with
# the sample itself held out:
#
#   craig's speech  vs craig's profile    0.69 - 0.82   (same person)
#   cheryl's speech vs craig's profile    0.57 - 0.64   (different person)
#
# **The separation is real but narrow: 0.64 against 0.69.** A first guess of
# 0.50 was safe for him and useless against her — it would have dropped 0 of
# 3 of cheryl's utterances. 0.60 drops the clearly-foreign end while keeping
# every one of his 15 with 0.09 of margin.
#
# Stated honestly, because the margin matters more than the number:
#
# - Audio that is nothing like him — a television, a voice across the room,
#   media playing — scores far below this and is caught cleanly. That is the
#   case Craig actually reported.
# - **Another person sitting with him is only partly caught.** Cheryl's real
#   samples span 0.57-0.64, so the top of her range survives this. Raising
#   the threshold to 0.66 would separate these two speakers completely, and
#   would sit 0.03 below his worst enrolled score, which is far too tight for
#   live audio — enrolled samples are clean and short live utterances score
#   lower. Not worth trading his speech being ignored for it.
# - n is small (3 samples for the second speaker) and these are ENROLLED
#   embeddings, not live noisy ones. Treat the numbers as indicative.
#
# The mechanism that improves this over time is already running:
# db.reinforce_voice_sample() adds every confirmed-genuine sample to the
# profile, so his side of the comparison gets more robust with use.
FOREIGN_SPEAKER_THRESHOLD = 0.60

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


TOP_K_FOR_MATCH = 2


def best_match(candidate: np.ndarray, enrolled: list, top_k: int = TOP_K_FOR_MATCH) -> float:
    """Average of the top `top_k` individual similarities, not just the
    single best one.

    2026-07-17 (Craig, after learning MATCH_THRESHOLD had been lowered
    from 0.75 to 0.68 specifically to stop rejecting his own real voice):
    a bare max meant every newly enrolled sample (continuous reinforcement
    keeps adding more over time) was purely another lottery ticket for a
    lucky high-scoring match — enrollment growing made verification
    structurally EASIER to pass over time, not more reliable. Averaging
    the top few scores means multiple samples actually have to resemble
    the candidate, not just one outlier, while still not being dragged
    down by a single old/noisy sample the way a full average across
    every enrolled sample ever recorded would be. Falls back to the
    single best score when there's only one enrolled sample (nothing to
    average yet).

    Note: this changes the SCORING METHOD, not MATCH_THRESHOLD itself
    (still 0.68) — a top-2 average is generally lower than a bare max for
    the same samples, so this makes verification meaningfully stricter in
    practice even with the same threshold number. Flagged, not silently
    absorbed: watch for this reintroducing false rejections on real
    speech (the exact problem that got the threshold lowered in the
    first place) — can't be verified without live mic testing."""
    if candidate is None or not enrolled:
        return 0.0

    scores = sorted((_cosine(candidate, e) for e in enrolled), reverse=True)
    top = scores[:top_k]
    return sum(top) / len(top)


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


def is_foreign_speaker(audio_bytes: bytes, enrolled: list) -> tuple:
    """Was this utterance someone other than the person she is talking to?

    Returns (foreign: bool, score: float). **Fails open in every direction**
    — no enrolled samples, audio too short to embed, a decode error, all
    return (False, 0.0), i.e. "treat it as his". A speaker check that can
    silence someone by breaking is worse than no speaker check, and this is
    on the path of every single spoken turn.

    Separate from is_match()/best_match() on purpose: those gate trust, this
    gates attention. See FOREIGN_SPEAKER_THRESHOLD for why the numbers
    differ.
    """
    if not enrolled:
        return False, 0.0

    try:
        candidate = embed_voice_bytes(audio_bytes)
    except Exception:
        return False, 0.0

    if candidate is None:
        return False, 0.0

    score = best_match(candidate, enrolled)
    return score < FOREIGN_SPEAKER_THRESHOLD, score
