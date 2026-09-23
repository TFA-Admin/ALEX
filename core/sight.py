# core/sight.py
"""
Her eyes: frames from the camera on the page of whoever she is talking
to, over the WebSocket she already has. Roadmap item 9 (2026-09-23).

Two jobs, as designed:

- Face verification (Principle 9, recognition as authority). At connect,
  if the person's face is enrolled and their page has its camera on, one
  frame verifies them without asking them to speak; voice stays the
  fallback and the other way in. OpenCV's YuNet finds the face and SFace
  embeds it (models/vision/*.onnx, 39 MB, CPU, tens of milliseconds);
  cosine against the enrolled samples, a rolling window of 15 like voice.
  Craig, 2026-09-23: any standard webcam, and testers' pages too — "this
  will be her eyes after all."

- Looking. The `look` tool asks the page for one frame and her own model
  describes it — qwen3.5 sees natively (checked 2026-09-23: a red circle
  and a blue square, 4.9 s), so there is no second model and nothing
  extra on the GPU. If there is a face she knows in the frame, she is
  told whose.

Frames are taken only when she looks or when verifying, never
continuously; nothing is stored but face embeddings. The page answers a
__LOOK__<purpose> with __FRAME__<purpose>:<base64 jpeg>, or with "none"
when its eyes are closed. Two ways of waiting for it: at connect, before
the main loop in ws/ws_handlers.py starts, this module reads the socket
itself (audio that arrives meanwhile goes to the session's buffer, other
text is queued for the loop); once the loop is running it keeps reading
while she works, so a look waits on a future the loop resolves.
"""
import asyncio
import base64
import json
import os
import re
import threading
import time

import numpy as np

try:
    import cv2
except Exception:      # pragma: no cover - she still talks without eyes
    cv2 = None

from config.logger_config import logger

ALEX_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(ALEX_DIR, "models", "vision")
DETECTOR_PATH = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
RECOGNIZER_PATH = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")

# SFace cosine. OpenCV's own line is 0.363; 0.40 was "a bit stricter" and
# his three enrolment samples score only 0.78-0.80 against each other in
# a backlit room, so a later frame can dip. His line it is (2026-09-23).
FACE_MATCH_THRESHOLD = 0.363
FRAME_TIMEOUT_S = 6.0           # a look: the page grabs and sends within a second
VERIFY_TIMEOUT_S = 4.0          # at connect: the camera may still be starting
DESCRIBE_TIMEOUT_S = 30.0
LOOK_TIMEOUT_S = 40.0           # the tool's own budget (frame + description)

# Glances (2026-09-23, Craig: "snapshots, not video — seems doable"). One
# frame a minute from a page with its eyes open while nothing is being
# said; compared with the last one as a 32x24 grey thumbnail; only a
# scene that changed goes to her model, at most once every two minutes
# per page. The description is an observation (db.observations, text
# only, 14 days), shown to her as "what you noticed" and offered to her
# curiosity. ~2.5 s of GPU per described glance; a still room costs none.
GLANCE_EVERY_S = 60.0
GLANCE_QUIET_S = 20.0           # nobody has spoken and she is not speaking
GLANCE_CHANGE = 1.5             # percent of the thumbnail whose pixels moved by more than 20/255 (a can is ~2%)
GLANCE_DESCRIBE_EVERY_S = 120.0
GLANCE_SIZE = (32, 24)

_detector = None
_recognizer = None
_lock = threading.Lock()


def available() -> bool:
    return cv2 is not None and os.path.exists(DETECTOR_PATH) and os.path.exists(RECOGNIZER_PATH)


def _models():
    global _detector, _recognizer
    with _lock:
        if _detector is None:
            # score threshold 0.7 (was 0.8): his face in front of a bright
            # window is partly in shadow; enrolment found it, a look must too.
            _detector = cv2.FaceDetectorYN.create(DETECTOR_PATH, "", (320, 320), 0.7, 0.3, 5000)
            _recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER_PATH, "")
    return _detector, _recognizer


# ------------------------------------------------------------------ frames
def decode_frame(b64: str):
    try:
        raw = base64.b64decode(b64, validate=False)
        arr = np.frombuffer(raw, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def face_embedding(img):
    """(embedding, faces_found) for the largest face in the frame."""
    det, rec = _models()
    h, w = img.shape[:2]
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    if faces is None or len(faces) == 0:
        return None, 0
    faces = sorted(faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
    aligned = rec.alignCrop(img, faces[0])
    return rec.feature(aligned), len(faces)


def match_score(feat, enrolled) -> float:
    _, rec = _models()
    cosine = getattr(cv2, "FaceRecognizerSF_FR_COSINE", 0)
    best = 0.0
    for e in enrolled or []:
        try:
            best = max(best, float(rec.match(feat, e, cosine)))
        except Exception:
            continue
    return best


async def await_frame(websocket, conn: dict, purpose: str, timeout: float):
    """Reads the socket until the page answers __LOOK__<purpose>."""
    deadline = time.time() + timeout
    prefix = f"__FRAME__{purpose}:"
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if message.get("type") == "websocket.disconnect":
            conn.setdefault("pending", []).append(message)
            raise ConnectionError("the page went away")
        b = message.get("bytes")
        if b is not None:
            audio = conn.get("audio")
            if audio is not None:
                audio.add_audio(b)
            continue
        text = message.get("text")
        if text is None:
            continue
        if text.startswith(prefix):
            payload = text[len(prefix):]
            return None if payload in ("", "none") else payload
        conn.setdefault("pending", []).append(message)


async def request_frame(conn: dict, purpose: str = "look", timeout: float = FRAME_TIMEOUT_S,
                        direct: bool = False):
    """Asks the page for a frame and waits for it.

    direct=True reads the socket here — only right at connect, before the
    main loop starts, when nothing else is reading (verify_at_connect).
    Otherwise the main loop keeps reading while she works (found live:
    "cannot call recv while another coroutine is already waiting"), so
    the answer is handed over by the loop through deliver_frame()."""
    ws = conn["websocket"]
    if direct:
        await ws.send_text(f"__LOOK__{purpose}")
        return await await_frame(ws, conn, purpose, timeout)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    conn.setdefault("frame_waiters", {})[purpose] = fut
    try:
        await ws.send_text(f"__LOOK__{purpose}")
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        if conn.get("frame_waiters", {}).get(purpose) is fut:
            del conn["frame_waiters"][purpose]


def deliver_frame(conn: dict, msg: str) -> bool:
    """The main loop hands a __FRAME__<purpose>:<payload> here. True when
    someone was waiting for it."""
    body = msg[len("__FRAME__"):]
    purpose, _, payload = body.partition(":")
    fut = (conn.get("frame_waiters") or {}).get(purpose)
    if fut is None or fut.done():
        return False
    fut.set_result(None if payload in ("", "none") else payload)
    return True


# ------------------------------------------------------------- eyes state
# 2026-09-23 (Craig held a can up: "Alex do you know what this is?" — the
# classifier scored sight 0 and she answered "Identify the object; I have
# no omniscient database"). The page now says when its eyes open or
# close (__EYES__on/off), so a "what is this" with the eyes open is a
# look, deterministically, whatever the classifier thought.
_DEMONSTRATIVE_RE = re.compile(
    r"\b(?:what(?:'s| is) (?:this|that|it)\b|do you know what (?:this|that|it) is|what am i (?:holding|wearing|showing)"
    r"|(?:look|looking) at (?:this|that|it|me)\b|can you see (?:this|that|it|me)\b|(?:see|recogni[sz]e) (?:this|that)\b"
    r"|what do you (?:see|think of this|make of this)|guess what (?:this|it) is|(?:identify|describe) (?:this|that|it))",
    re.I,
)


def eyes_open(user_id: str) -> bool:
    from ws.ws_handlers import _active_connections
    return any(c.get("eyes") for c in list(_active_connections.values()) if c.get("user_id") == user_id)


def wants_a_look(user_id: str, text: str) -> bool:
    """He is pointing at something and his eyes are open."""
    return bool(text) and eyes_open(user_id) and bool(_DEMONSTRATIVE_RE.search(text))


# ------------------------------------------------------------- recognising
async def recognise(b64: str, info: dict = None) -> str:
    """Whose face is in the frame, in her words, or "" if she cannot say.
    `info`, if given, is filled with the facts for the page and the log:
    seen (user / "unknown" / "none"), score, faces."""
    info = info if info is not None else {}
    if not available():
        return ""
    img = decode_frame(b64)
    if img is None:
        return ""
    feat, n = await asyncio.to_thread(face_embedding, img)
    info["faces"] = n
    if feat is None:
        info["seen"] = "none"
        return "There is no face in the frame."
    from db.db import fetch_all_face_profiles
    profiles = await fetch_all_face_profiles()
    best_user, best = None, 0.0
    for user, embs in profiles.items():
        s = match_score(feat, embs)
        if s > best:
            best_user, best = user, s
    info["score"] = round(best, 2)
    info["best"] = best_user
    more = f" There {'is one more face' if n == 2 else f'are {n - 1} more faces'} in the frame." if n > 1 else ""
    if best_user and best >= FACE_MATCH_THRESHOLD:
        info["seen"] = best_user
        return f"The face in the frame is {best_user}'s (match {best:.2f})." + more
    info["seen"] = "unknown"
    # 2026-09-23: "you do not recognise" became "that is not Craig" in her
    # mouth before he had enrolled at all. Say what is actually known.
    if not profiles:
        return ("There is a face in the frame; nobody has enrolled a face yet, so you cannot "
                "tell by sight who it is — go by their voice and name. (The 'Enrol my face' button "
                "on his page enrols him, while he is verified by voice.)" + more)
    return (f"There is a face in the frame that matches nobody who has enrolled (best {best:.2f} "
            f"against {best_user or 'anyone'}; a match needs {FACE_MATCH_THRESHOLD:.2f}); you cannot "
            "tell by sight who it is." + more)


# ------------------------------------------------------------------ looking
async def describe(b64: str, question: str, who: str) -> str:
    from llm.ollama_client import ollama_manager
    prompt = (f"This is one frame from the webcam on {who}'s side, taken just now. "
              "Describe what you actually see in two or three plain sentences: people and their "
              "expression, what they are holding or doing, the room and the light. State only what is "
              "visible; do not guess names or intentions."
              + (f" They asked: {question.strip()}" if question and question.strip() else ""))
    text = await ollama_manager.describe_image(prompt, b64, timeout=DESCRIBE_TIMEOUT_S)
    return (text or "").strip()


async def look(user_id: str, question: str = "") -> str:
    """The `look` tool: one frame from the page of the person she is
    talking to, described by her own model, with whose face it is."""
    from ws.ws_handlers import _active_connections
    conns = [c for c in list(_active_connections.values()) if c.get("user_id") == user_id]
    if not conns:
        return f"No page of {user_id}'s is connected, so there is nothing to look through."
    b64 = None
    for conn in conns:
        try:
            b64 = await request_frame(conn, "look")
        except ConnectionError:
            continue
        if b64:
            break
    if not b64:
        return (f"{user_id}'s page has its eyes closed (the Eyes button on the page opens the camera), "
                "so you cannot see anything right now.")
    t0 = time.time()
    info = {}
    who = await recognise(b64, info)
    seen = await describe(b64, question, user_id)
    # 2026-09-23 (Craig: "it is seeing things but still won't recognize the
    # visual as me"): whose face it is comes FIRST — after a paragraph of
    # description it was the last thing she read, and it lost to her
    # earlier "you remain faceless to my sensors". The page's face row
    # and the log get the facts too, so a miss can be seen for what it is.
    out = " ".join(x for x in (who, seen) if x)
    logger.info(f"[SIGHT] face for {user_id}: {info.get('seen', '?')}"
                + (f" (score {info['score']:.2f} vs {FACE_MATCH_THRESHOLD:.2f}, best {info.get('best')})" if 'score' in info else "")
                + f", faces {info.get('faces', '?')}")
    logger.info(f"[SIGHT] looked for {user_id} in {time.time() - t0:.1f}s -> {seen[:100]!r}")
    if info:
        for conn in conns:
            await _tell(conn["websocket"], info)
    return out or "The frame arrived but you could not make anything of it."


# ------------------------------------------------------------ verification
async def verify_at_connect(websocket, conn: dict, session: dict, session_id: str, user_id: str) -> bool:
    """One frame at connect. True when the face matches the enrolled
    samples — the session is then verified the same way a voice match
    verifies it. False means voice is asked for as before."""
    if not available():
        return False
    from db.db import fetch_face_samples, session_verified, reinforce_face_sample
    enrolled = await fetch_face_samples(user_id)
    if not enrolled:
        return False
    try:
        b64 = await request_frame(conn, "verify", timeout=VERIFY_TIMEOUT_S, direct=True)
    except ConnectionError:
        return False
    if not b64:
        logger.info(f"[SIGHT] {user_id}'s page has its eyes closed — voice will do")
        return False
    img = decode_frame(b64)
    feat, n = (await asyncio.to_thread(face_embedding, img)) if img is not None else (None, 0)
    if feat is None:
        await _tell(websocket, {"seen": "none"})
        logger.info(f"[SIGHT] no face in {user_id}'s frame — voice will do")
        return False
    score = match_score(feat, enrolled)
    ok = score >= FACE_MATCH_THRESHOLD
    await _tell(websocket, {"seen": user_id if ok else "unknown", "score": round(score, 2), "verified": ok})
    logger.info(f"[SIGHT] face {'verified' if ok else 'did not match'} {user_id}: {score:.2f} ({n} face(s))")
    if ok:
        session["creator_verified"] = True
        session["verified_how"] = f"face, match {score:.2f}"
        try:
            await session_verified(session_id, True)
        except Exception:
            pass
        try:
            await reinforce_face_sample(user_id, feat)
        except Exception as e:
            logger.warning(f"⚠️ could not keep the face sample: {e}")
    return ok


async def enrol_frame(websocket, user_id: str, b64: str) -> bool:
    """One enrolment frame from the page (the button sends three)."""
    if not available():
        await _tell(websocket, {"enrolled": False, "reason": "no face models installed"})
        return False
    from db.db import fetch_face_samples, reinforce_face_sample
    img = decode_frame(b64) if b64 and b64 != "none" else None
    feat, n = (await asyncio.to_thread(face_embedding, img)) if img is not None else (None, 0)
    if feat is None:
        await _tell(websocket, {"enrolled": False, "reason": "no face in the frame"})
        return False
    await reinforce_face_sample(user_id, feat)
    count = len(await fetch_face_samples(user_id))
    await _tell(websocket, {"enrolled": True, "samples": count})
    logger.info(f"[SIGHT] enrolled a face sample for {user_id} ({count} kept)")
    return True


async def _tell(websocket, info: dict):
    try:
        await websocket.send_text("__FACE__" + json.dumps(info))
    except Exception:
        pass


# ------------------------------------------------------------------ glances
def frame_signature(img):
    """A 32x24 grey thumbnail as float32 — enough to tell a changed scene
    from a still one, cheap enough to do every minute for every page."""
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(grey, GLANCE_SIZE, interpolation=cv2.INTER_AREA)
    return small.astype("float32")


def frame_change(a, b) -> float:
    """Percent of thumbnail pixels that moved by more than 20/255. A mean
    difference diluted a small new object (a can: 3 of 10); a percentage
    of changed area catches it (2%) and still ignores lighting drift
    (0%), while a person moving or the lights going off is most of it."""
    if a is None or b is None:
        return 100.0
    return float(100.0 * np.mean(np.abs(a - b) > 20.0))


def _busy() -> bool:
    try:
        from ws.ws_handlers import generation_lock
        return generation_lock.locked()
    except Exception:
        return False


async def describe_glance(b64: str, who: str) -> str:
    from llm.ollama_client import ollama_manager
    prompt = (f"This is a glance through the webcam on {who}'s side while nothing is being said. "
              "In one or two plain sentences: who is there and what they are doing, and any object "
              "or change worth noticing. State only what is visible; no names, no guesses.")
    text = await ollama_manager.describe_image(prompt, b64, timeout=DESCRIBE_TIMEOUT_S, num_predict=120)
    return (text or "").strip()


async def glance(session_id: str, conn: dict) -> str:
    """One glance for one page. Returns the observation text when a
    changed scene was described, else ""."""
    if cv2 is None:
        return ""
    user_id = conn.get("user_id") or ""
    try:
        b64 = await request_frame(conn, "glance", timeout=5.0)
    except ConnectionError:
        return ""
    if not b64:
        return ""
    img = decode_frame(b64)
    if img is None:
        return ""
    sig = await asyncio.to_thread(frame_signature, img)
    change = frame_change(conn.get("glance_sig"), sig)
    conn["glance_sig"] = sig
    first = "glance_described_at" not in conn
    if not first and change < GLANCE_CHANGE:
        return ""
    if not first and time.time() - conn.get("glance_described_at", 0) < GLANCE_DESCRIBE_EVERY_S:
        return ""
    conn["glance_described_at"] = time.time()
    info = {}
    who = await recognise(b64, info)
    text = await describe_glance(b64, user_id)
    if not text:
        return ""
    face = info.get("seen") or ""
    from db.db import add_observation
    try:
        await add_observation(user_id, text, changed=change, face=face, session_id=session_id)
    except Exception as e:
        logger.warning(f"[SIGHT] could not keep the observation: {e}")
    logger.info(f"[SIGHT] glance at {user_id}'s ({'first' if first else f'changed {change:.0f}%'}"
                f"{', face ' + face if face else ''}): {text[:120]!r}")
    if info:
        await _tell(conn["websocket"], info)
    return text


async def glance_all() -> int:
    """Every page with its eyes open, if the room is quiet and she is not
    busy. Called once a minute from main.py."""
    from ws.ws_handlers import _active_connections
    from core import idle_author
    from core.alex_core import alex_core
    if _busy() or idle_author.idle_for() < GLANCE_QUIET_S:
        return 0
    n = 0
    for session_id, conn in list(_active_connections.items()):
        if not conn.get("eyes"):
            continue
        try:
            session = alex_core.get_session(session_id)
            if time.time() < float(session.get("last_addressed_at", 0) or 0) + GLANCE_QUIET_S:
                continue
        except Exception:
            pass
        try:
            if await glance(session_id, conn):
                n += 1
        except Exception as e:
            logger.warning(f"[SIGHT] glance at {conn.get('user_id')}'s failed: {e}")
    return n
