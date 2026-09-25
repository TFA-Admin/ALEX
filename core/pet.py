# core/pet.py
"""
Something of hers to take care of.

2026-09-25, Craig: "a virtual pet for her to take care of. The premise
is this. The pet itself is almost irrelevant but would have requirements
and give her something to interact with to teach her things."

The pet has four needs that fall on the clock — food, rest, clean,
company — and a health that falls when any need is starved and recovers
when all are met. Time passes for it while she is off (the state is
stored with its timestamp and drifted on the next read). She tends it
herself, in quiet time, from the loop in main.py: one need per pass,
the lowest first, as a decision she records; and in conversation
through the tend_pet tool when it is urgent or someone asks. Neglect
shows in her mood as strain; a pet with every need met shows as
engagement. Nothing about it is scripted speech: the state is one line
in her prompt and what she says about it is hers.

What it teaches, by construction: needs arise whether or not anyone is
talking; noticing has to happen on a schedule; an action has a cost
(play tires it); what she does not do has consequences she will see in
the numbers and feel in her mood. The pet's name is `pet_name` in
config/controller_settings.json; the Controller shows its state and log
at Her -> Health, and the page's rail shows how it is.
"""
import json
import time

from config.logger_config import logger

NEEDS = ("food", "rest", "clean", "company")
DRIFT_PER_HOUR = {"food": -6.0, "rest": -4.0, "clean": -3.0, "company": -5.0}
ACTIONS = {
    # action -> (need it serves, gain, side effects)
    "feed":  ("food", 40.0, {}),
    "rest":  ("rest", 40.0, {}),
    "clean": ("clean", 50.0, {}),
    "play":  ("company", 35.0, {"rest": -8.0}),
}
LOW = 40.0                  # a need below this is tended in quiet time
URGENT = 15.0               # below this the pet is suffering
HEALTH_LOSS_PER_HOUR = 4.0  # while any need is under URGENT
HEALTH_GAIN_PER_HOUR = 2.0  # while every need is 50 or more
TEND_IDLE_S = 5 * 60        # nobody has spoken for this long before she tends it herself
MAX_GAP_HOURS = 2.0         # the most a gap in her running counts against the pet
KEEP_LOG = 20


def fresh(now: float = None) -> dict:
    now = time.time() if now is None else now
    return {"needs": {n: 80.0 for n in NEEDS}, "health": 100.0, "born_at": now, "at": now, "log": []}


def drifted(state: dict, now: float = None) -> dict:
    """The pet as it is NOW: needs fallen since the state was stored,
    health moved by how the needs stood."""
    now = time.time() if now is None else now
    state = state or fresh(now)
    hours = max(0.0, now - float(state.get("at") or now)) / 3600.0
    # 2026-09-25 (Craig: "The pet won't die though if I simply turn ALEX
    # off for a week though correct?"): it must not. Its clock runs while
    # SHE runs; a gap longer than MAX_GAP_HOURS (she was off) counts as
    # that much and no more, so his switching her off is never her
    # neglect. And health never goes below 5: poorly, never dead.
    hours = min(hours, MAX_GAP_HOURS)
    needs = {n: max(0.0, min(100.0, float((state.get("needs") or {}).get(n, 80.0)) + DRIFT_PER_HOUR[n] * hours))
             for n in NEEDS}
    health = float(state.get("health", 100.0))
    if any(v < URGENT for v in needs.values()):
        health -= HEALTH_LOSS_PER_HOUR * hours
    elif all(v >= 50.0 for v in needs.values()):
        health += HEALTH_GAIN_PER_HOUR * hours
    health = max(5.0, min(100.0, health))
    return {**state, "needs": {n: round(v, 1) for n, v in needs.items()}, "health": round(health, 1), "at": now}


def apply_action(state: dict, action: str, by: str = "her", now: float = None) -> dict:
    now = time.time() if now is None else now
    if action not in ACTIONS:
        raise ValueError(f"no such action: {action}")
    s = drifted(state, now)
    need, gain, side = ACTIONS[action]
    before = s["needs"][need]
    s["needs"][need] = round(min(100.0, before + gain), 1)
    for k, d in side.items():
        s["needs"][k] = round(max(0.0, min(100.0, s["needs"][k] + d)), 1)
    log = list(s.get("log") or [])
    log.append({"t": now, "action": action, "by": by, "need": need, "before": round(before, 1), "after": s["needs"][need]})
    s["log"] = log[-KEEP_LOG:]
    return s


def lowest_need(state: dict):
    needs = state.get("needs") or {}
    if not needs:
        return None, 100.0
    n = min(needs, key=needs.get)
    return n, needs[n]


def action_for(need: str) -> str:
    for a, (n, _g, _s) in ACTIONS.items():
        if n == need:
            return a
    return "feed"


def describe(state: dict, name: str = "the pet") -> str:
    """One line for her prompt and the Controller."""
    needs = state.get("needs") or {}
    parts = []
    for n in NEEDS:
        v = needs.get(n, 0.0)
        tag = " (suffering)" if v < URGENT else " (low)" if v < LOW else ""
        parts.append(f"{n} {v:.0f}{tag}")
    h = float(state.get("health", 100.0))
    htag = " — unwell" if h < 40 else " — poorly" if h < 70 else ""
    return f"{name}: " + ", ".join(parts) + f"; health {h:.0f}/100{htag}"


def status_for_page(state: dict) -> dict:
    n, v = lowest_need(state)
    h = float(state.get("health", 100.0))
    if h < 40:
        word = "unwell"
    elif v < URGENT:
        word = f"needs {n}"
    elif v < LOW:
        word = f"{n} low"
    else:
        word = "fine"
    return {"word": word, "health": round(h), "needs": {k: round(x) for k, x in (state.get("needs") or {}).items()}}


# ---------------------------------------------------------------- with the DB
def pet_name() -> str:
    try:
        from controller.common import load_controller_settings
        return (load_controller_settings().get("pet_name") or "").strip() or "the pet"
    except Exception:
        return "the pet"


async def state() -> dict:
    from db.db import get_pet_state
    try:
        return drifted((await get_pet_state()) or fresh())
    except Exception as e:
        logger.warning(f"[PET] could not read the pet: {e}")
        return fresh()


async def save(s: dict):
    from db.db import set_pet_state
    await set_pet_state(s)


async def tend(action: str, by: str = "her", why: str = "") -> str:
    """One action, recorded. Returns the line she gets back."""
    from db.db import get_pet_state, record_decision
    current = (await get_pet_state()) or fresh()
    new = apply_action(current, action, by=by)
    # 2026-09-25: a lifetime count, since `log` keeps only the last 20 and
    # tenure() should be able to say how much care he has actually had.
    new["acts_total"] = int(current.get("acts_total") or len(current.get("log") or [])) + 1
    await save(new)
    need, _g, _s = ACTIONS[action]
    last = new["log"][-1]
    text = f"{action}: {need} {last['before']:.0f} -> {last['after']:.0f}; now {describe(new, pet_name())}"
    logger.info(f"[PET] {by} {text}")
    try:
        await record_decision(
            "pet", f"{'She' if by == 'her' else by} tended the pet: {action}",
            reasoning=why or (f"{need} was at {last['before']:.0f}" if by == "her" else "asked to"),
            evidence=describe(current, pet_name()), outcome=describe(new, pet_name()), actor="alex" if by == "her" else by)
    except Exception:
        pass
    return text


def tenure(state: dict, now: float = None) -> dict:
    """What her record with the pet is, as numbers. 2026-09-25: nothing read
    `born_at` before this, so "she has kept him at full health since Tuesday"
    was a fact the system held and never used."""
    now = time.time() if now is None else now
    born = float(state.get("born_at") or now)
    log = state.get("log") or []
    return {"days": max(0.0, (now - born) / 86400.0),
            "acts": int(state.get("acts_total") or len(log)),
            "health": float(state.get("health", 100.0))}


async def care_pass() -> str:
    """Quiet-time care from main.py: one need under LOW, the lowest first.
    Also where neglect and thriving reach her mood."""
    from core import idle_author, mood as her_mood
    s = await state()
    await save(s)                      # the drift is kept even when nothing is done
    n, v = lowest_need(s)
    h = float(s.get("health", 100.0))
    try:
        if h < 40:
            await her_mood.note("pet_unwell")
        elif all(x >= 70 for x in s["needs"].values()) and h >= 90:
            await her_mood.note("pet_thriving")
    except Exception:
        pass
    if n is None or v >= LOW:
        return ""
    if idle_author.idle_for() < TEND_IDLE_S:
        return ""
    text = await tend(action_for(n), by="her", why=f"{n} had fallen to {v:.0f} in quiet time")
    # 2026-09-25: the act itself counts. She caught a need at LOW, well before
    # URGENT — that is the thing she does, and it went unrewarded until now.
    try:
        if v > URGENT:
            await her_mood.note("pet_tended")
    except Exception:
        pass
    return text
