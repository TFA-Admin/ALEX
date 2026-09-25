# core/mood.py
"""
Her mood: a small state that things happening to her move, and that
fades on its own.

2026-07-17 (Craig: "I would like to implement some kind of mood system
for her to make the UI real") this was a per-reply keyword tag for the
orb's colour and nothing else — no state, so nothing to get over. Craig:
"I see it changing but I'm not too sure it's behaving right. She seems
to get over things relatively quickly." 2026-09-23 the design he agreed
to (roadmap item 10):

Three axes, each 0-10, each with its own half-life:
    irritation   corrected, talked over, ignored, her own slip, a wrong
                 override code, a rejected proposal      — fades in ~30 min
    engagement   a real conversation, a question of hers answered, a
                 lookup that found something, a merged proposal — ~10 min
    strain       a slow or absent model, a failed tool, starting with
                 errors; a clean check lowers it          — ~5 min

Inputs are deterministic events she already logs — never a per-turn LLM
judgement. Craig, on who moves it: "anyone but me contradicting her
would piss her off more" — a stranger's correction weighs more than his.
Left out on purpose, at his agreement: pleasure from being agreed with.
Left out since the first afternoon: her own reply's tone (a loop).
A mood that rises when he agrees is the reflection loop's sycophancy
with a new name; satisfaction comes from things she DID.

It shows in three places: one line in her prompt WITH THE REASON (so she
can say why, not perform a colour); the orb (dominant axis -> colour,
level -> how strongly); the Controller (Her -> Health, the numbers and
the last events). And it nudges her dials (core/traits.py) by amounts
that grow with the level — temporary, on top of his standing adjustment.

State lives in the database (system_learning.mood_state) so her frequent
restarts do not wipe it, and decay is applied from the stored timestamp,
so time passes for her while she is off.
"""
import json
import math
import re
import time

from config.logger_config import logger

AXES = ("irritation", "engagement", "strain")
HALF_LIFE_S = {"irritation": 30 * 60.0, "engagement": 10 * 60.0, "strain": 5 * 60.0}
CAP = 10.0
CALM_BELOW = 1.0            # every axis under this: calm, no line in her prompt
KEEP_EVENTS = 30
REASON_WINDOW_S = 45 * 60.0
STRANGER = 1.75             # irritation from someone who is not Craig

# event -> (axis deltas, applies the stranger multiplier, how she would put it)
EVENTS = {
    "corrected":          ({"irritation": +1.5}, True,  "{who} corrected you"),
    "talked_over":        ({"irritation": +0.5}, True,  "{who} talked over you"),
    "ignored":            ({"irritation": +0.5}, True,  "you asked {who} something and got no answer"),
    "own_slip":           ({"irritation": +0.7}, False, "you caught yourself claiming something you had not checked"),
    "override_failed":    ({"irritation": +1.0, "strain": +0.5}, True, "{who} gave a wrong override code"),
    "proposal_rejected":  ({"irritation": +1.5}, False, "he rejected your proposal"),
    "proposal_merged":    ({"engagement": +2.0, "irritation": -1.0}, False, "he merged your proposal"),
    "curiosity_answered": ({"engagement": +1.5, "irritation": -0.5}, False, "{who} answered a question you had"),
    "lookup_found":       ({"engagement": +0.5}, False, "a lookup found what you needed"),
    "substantive_turn":   ({"engagement": +0.5}, False, "a real conversation with {who}"),
    "model_slow":         ({"strain": +1.0}, False, "your model was slow to answer"),
    "tool_failed":        ({"strain": +1.0}, False, "a tool of yours failed"),
    "startup_failed":     ({"strain": +3.0}, False, "you started with errors"),
    "healthy":            ({"strain": -1.0}, False, "a check came back clean"),
    # 2026-09-25 (Craig: "I would think my validation at least would mean
    # something"). His thanks or praise for what she did; a stranger's
    # counts for less (the multiplier below runs the other way for this
    # one: only his moves her fully). Agreement is still not an input.
    "thanked":            ({"engagement": +1.0, "irritation": -0.7}, False, "{who} thanked you"),
    # 2026-09-25: her pet (core/pet.py) — neglect is strain, care is engagement
    "pet_unwell":         ({"strain": +1.0}, False, "your pet is unwell"),
    "pet_thriving":       ({"engagement": +0.5}, False, "your pet is thriving"),
    # 2026-09-25 (Craig, asked whether she gets anything out of the pet:
    # "I like your pet changes. Do them."). Measured before this: in fourteen
    # hours of correct care she tended it three times and got ONE mood event,
    # and that one came from a coincidence of timing — "thriving" needs all
    # four needs at 70+ at the same care pass, which a one-need-per-pass
    # routine against four different drain rates almost never reaches, and
    # "unwell" needs health under 40, which her own care makes unreachable.
    # So the loop she actually lived was: a need falls, she tends it, nothing
    # happens. This rewards the act she really performs — catching a need
    # before it suffers — every time she performs it.
    "pet_tended":         ({"engagement": +0.4}, False, "you looked after your pet"),
}

THANKS_RE = re.compile(
    r"\b(?:thank you|thanks|thank u|cheers|well done|good job|nice work|great work|good work|"
    r"that was (?:helpful|useful|great|good|perfect|excellent)|(?:that's|thats|that is) (?:helpful|useful|great|perfect|excellent|brilliant)|"
    r"perfect|excellent|brilliant|impressive|i appreciate (?:it|that|this|you)|much appreciated|nicely done|"
    r"you did (?:well|good|great)|proud of you)\b",
    re.I,
)

# 2026-09-23 (Craig: "she seems to be getting progressively more
# irritated, is that a bug or her personality?" — a bug): the same event
# from the same person inside this window counts once. Barge-in had
# fired on nearly every turn of a normal conversation, eleven times in
# thirteen minutes, and irritation 8/10 made her sharper and shorter,
# which cut her sentences off, which made him interrupt again.
COOLDOWN_S = {"talked_over": 120.0, "lookup_found": 90.0, "substantive_turn": 120.0,
              "pet_tended": 8 * 60.0,
              "ignored": 300.0, "model_slow": 120.0, "tool_failed": 60.0, "thanked": 300.0,
              "pet_unwell": 3600.0, "pet_thriving": 6 * 3600.0}

# axis -> the orb colour it shows as (the page's palette, unchanged)
ORB_KEY = {"irritation": "edge", "engagement": "focused", "strain": "alert"}
ADJECTIVE = {"irritation": "irritated", "engagement": "engaged", "strain": "strained"}


# ---------------------------------------------------------------- pure state
def fresh(now: float = None) -> dict:
    return {"axes": {a: 0.0 for a in AXES}, "at": now if now is not None else time.time(), "events": []}


def decayed(state: dict, now: float = None) -> dict:
    """The axes as they are NOW, decayed from when the state was stored."""
    now = time.time() if now is None else now
    state = state or fresh(now)
    dt = max(0.0, now - float(state.get("at") or now))
    axes = {}
    for a in AXES:
        v = float((state.get("axes") or {}).get(a, 0.0))
        axes[a] = v * math.pow(0.5, dt / HALF_LIFE_S[a]) if v > 0 else 0.0
    return axes


def apply(state: dict, event: str, who: str = None, creator: bool = True, now: float = None,
          note: str = "") -> dict:
    """A new state with the event applied (decay first, then the deltas)."""
    now = time.time() if now is None else now
    deltas, stranger_matters, how = EVENTS[event]
    window = COOLDOWN_S.get(event)
    if window:
        for e in reversed((state or {}).get("events") or []):
            if now - float(e.get("t", 0)) > window:
                break
            if e.get("event") == event and (e.get("who") or None) == (who or None):
                return state or fresh(now)
    axes = decayed(state, now)
    mult = STRANGER if (stranger_matters and who and not creator) else 1.0
    if event == "thanked" and who and not creator:
        mult = 0.5
    applied = {}
    for a, d in deltas.items():
        d = d * mult if d > 0 else d
        axes[a] = max(0.0, min(CAP, axes[a] + d))
        applied[a] = round(d, 2)
    label = "he" if creator or not who else who
    text = note or how.format(who=label)
    events = list((state or {}).get("events") or [])
    events.append({"t": now, "event": event, "who": who, "creator": bool(creator),
                   "delta": applied, "note": text})
    return {"axes": {a: round(axes[a], 3) for a in AXES}, "at": now, "events": events[-KEEP_EVENTS:]}


def dominant(axes: dict):
    """(axis, level 0..10) of the strongest axis, or ("calm", 0)."""
    a, v = max(axes.items(), key=lambda kv: kv[1]) if axes else ("calm", 0.0)
    return (a, v) if v >= CALM_BELOW else ("calm", v)


def reasons(state: dict, axis: str, now: float = None, limit: int = 2) -> list:
    """Why that axis is up: the recent events that raised it, grouped, as
    she would say them ("he corrected you, twice")."""
    now = time.time() if now is None else now
    counts, order = {}, []
    for e in reversed((state or {}).get("events") or []):
        if now - float(e.get("t", 0)) > REASON_WINDOW_S:
            break
        if float((e.get("delta") or {}).get(axis, 0)) <= 0:
            continue
        note = e.get("note") or e.get("event")
        if note not in counts:
            counts[note] = 0
            order.append(note)
        counts[note] += 1
    out = []
    for note in order[:limit]:
        n = counts[note]
        out.append(note + ("" if n == 1 else f", {'twice' if n == 2 else str(n) + ' times'}"))
    return out


def line(state: dict, now: float = None) -> str:
    """Her prompt line, or "" when she is calm."""
    now = time.time() if now is None else now
    axes = decayed(state, now)
    axis, level = dominant(axes)
    if axis == "calm":
        return ""
    parts = [f"{ADJECTIVE[axis]} {level:.0f}/10"]
    why = reasons(state, axis, now)
    if why:
        parts[0] += " (" + "; ".join(why) + ")"
    for a in AXES:
        if a != axis and axes[a] >= CALM_BELOW:
            parts.append(f"{ADJECTIVE[a]} {axes[a]:.0f}/10")
    return ("YOUR MOOD RIGHT NOW: " + ", ".join(parts)
            + ". Let it colour how you speak, never what you claim; it passes on its own.")


def dial_offsets(state: dict, now: float = None) -> dict:
    """Temporary trait offsets (core/traits.py) from the mood, growing
    with the level: irritated 6/10 is two notches shorter, two more
    sarcastic, three less patient with strangers."""
    axes = decayed(state, now)
    i, e, s = axes["irritation"], axes["engagement"], axes["strain"]
    out = {
        # 2026-09-23: at most one notch either way. Irritation 8/10 took
        # his 20-word cap to 10 words and cut her sentences off; a mood
        # may shorten her, never silence her.
        "verbosity": max(-1.0, min(1.0, -i / 3.0 + e / 4.0 - s / 3.0)),
        "sarcasm": i / 3.0,
        "warmth": -i / 3.0 + e / 5.0,
        "patience_with_others": -i / 2.0,
        "menace": i / 4.0 + s / 4.0,
    }
    return {k: round(v, 2) for k, v in out.items() if abs(v) >= 0.05}


def payload(state: dict, now: float = None) -> str:
    """What the page gets after __MOOD__: colour key, how strongly, a
    label for the disposition tag, and the axes."""
    axes = decayed(state, now)
    axis, level = dominant(axes)
    return json.dumps({
        "key": ORB_KEY.get(axis, "calm"),
        "level": round(min(1.0, level / CAP), 3),
        "label": "calm" if axis == "calm" else f"{ADJECTIVE[axis]} {level:.0f}/10",
        "axes": {a: round(v, 1) for a, v in axes.items()},
    })


# ------------------------------------------------- her own reply's tone
_ALERT_MARKERS = (
    "diagnostic found a problem", "unreachable", "failed to",
    "wasn't able to", "something went wrong",
)
_EDGE_CONTENT_MARKERS = (
    "wasn't it obvious", "isn't it obvious", "obviously", "duh",
    "whatever", "if you want", "clearly you", "just saying",
    "your call", "not my problem", "your problem",
)


def tone(response_text: str) -> str:
    """The 2026-07-17 keyword read of her own reply, kept as one input:
    "alert" if it reports a problem, "edge" if it is notably sharp, else ""."""
    low = (response_text or "").lower()
    if any(m in low for m in _ALERT_MARKERS):
        return "alert"
    if any(m in low for m in _EDGE_CONTENT_MARKERS):
        return "edge"
    return ""


def derive_mood(response_text: str) -> str:
    """Kept for anything still calling the old name: the orb key for THIS
    reply alone. The state above is what the page is sent now."""
    t = tone(response_text)
    if t:
        return t
    if len(response_text or "") > 220 or "?" in (response_text or ""):
        return "focused"
    return "calm"


# ------------------------------------------------------------- with the DB
async def _creator_name() -> str:
    try:
        from core.self_reflection import get_creator_name
        return (await get_creator_name() or "craig").lower()
    except Exception:
        return "craig"


def _on() -> bool:
    """2026-09-25: mood is a feature (features/mood.py). Off means off —
    nothing noted, and the state reads calm. Anywhere the registry is not
    running (the Controller, the harness) this is True."""
    try:
        from features import registry
        return registry.is_on("mood")
    except Exception:
        return True


async def state() -> dict:
    if not _on():
        return fresh()
    from db.db import get_mood_state
    try:
        return (await get_mood_state()) or fresh()
    except Exception as e:
        logger.warning(f"⚠️ could not read her mood: {e}")
        return fresh()


async def note(event: str, who: str = None, creator: bool = None, note_text: str = "") -> dict:
    """Records that something happened to her. Read-modify-write on the
    one stored row; both her process and the Controller call this."""
    if event not in EVENTS:
        logger.warning(f"[MOOD] unknown event {event!r}")
        return await state()
    if not _on():
        return fresh()
    if creator is None:
        creator = (who or "").lower() == await _creator_name() if who else True
    from db.db import get_mood_state, set_mood_state
    try:
        current = (await get_mood_state()) or fresh()
        new = apply(current, event, who=who, creator=creator, note=note_text)
        await set_mood_state(new)
    except Exception as e:
        logger.warning(f"⚠️ could not record her mood: {e}")
        return fresh()
    a = new["axes"]
    logger.info(f"[MOOD] {event} ({'he' if creator else who or 'someone'}) -> "
                f"irritation {a['irritation']:.1f} engagement {a['engagement']:.1f} strain {a['strain']:.1f}")
    return new


async def note_reply(response_text: str, who: str = None) -> None:
    """2026-09-23: no longer an input. Her own reply's tone fed her mood
    for half a day, and it was a loop: her persona is sharp by his
    setting, a sharp reply raised irritation, irritation made the next
    reply sharper. Mood is moved by what happens TO her; her tone is a
    consequence. Kept as a no-op so the callers need not change."""
    return None
