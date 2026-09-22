"""
Onboarding suite — a stranger's first minute, measured.

2026-09-22. On the evening of 2026-09-21 the first two people other than
Craig to use her hit, in order: a page that sent no handshake and sat in
silence; a NameError on her first greeting that closed the socket with
nothing in her log; a microphone locked until a readiness signal that
only came after onboarding; her voice muted until a click; and the
Auto Listen button locking the page. Not one of those was visible to the
harness, because every other suite seeds the profile first (so onboarding
never runs) and types (so no gate applies).

This suite is the stranger. Deterministic (no judge): it connects with no
saved name the way the page now does, expects her readiness signal
BEFORE her question, expects her to ask in words and to speak (audio
bytes), answers by typing, and expects a profile for that name inside
the time limits. The profile it creates is removed afterwards.

It needs a running instance (ALEX_URL; the gate points it at the staging
copy). It cannot test the voice-enrollment path — there is no voice to
give — so it takes the typed route through enrollment, which is the
route that was broken on 2026-09-20/21.
"""
import json
import time
import asyncio
from dataclasses import dataclass

import websockets

from tests.harness import WS_URI, _SSL, purge, make_prefix


@dataclass
class OnboardingCase:
    id: str
    category: str
    expect: str
    text: str = ""          # for the report line
    note: str = ""


CASES = [
    OnboardingCase("stranger_by_text", "onboarding",
                   "ready-before-question, asks in words, speaks, profile within limits",
                   "a fresh visitor with no saved name, answering by typing"),
]

ASK_WITHIN_S = 20        # readiness + her question + her voice
PROFILE_WITHIN_S = 150   # the whole typed enrollment path; she waits out her own audio at each step
CASE_TIMEOUT_S = 200     # read by the harness's deterministic runner


async def evaluate(case: OnboardingCase):
    name = make_prefix() + "onb"
    t0 = time.time()
    got = {"ready_first": False, "asked": False, "spoke": False, "profile": False}
    detail = ""
    seen_text = False
    try:
        async with websockets.connect(WS_URI, ssl=_SSL, max_size=None) as ws:
            await ws.send(json.dumps({"user_name": None}))
            # phase 1: readiness, her question, her voice
            deadline = time.time() + ASK_WITHIN_S
            while time.time() < deadline and not (got["asked"] and got["spoke"]):
                msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                if isinstance(msg, (bytes, bytearray)):
                    got["spoke"] = True
                    continue
                if msg.startswith("__READY__"):
                    if not seen_text:
                        got["ready_first"] = True
                    continue
                if msg.startswith("__"):
                    continue
                seen_text = True
                got["asked"] = True
            if not got["asked"]:
                detail = f"no question from her within {ASK_WITHIN_S}s"
                return _summ(got), False, detail
            # phase 2: type the name, confirm, sit through enrollment by text
            script = [name, "yes"]
            step = 0
            deadline = time.time() + PROFILE_WITHIN_S
            await asyncio.sleep(0.5)
            await ws.send(script[step]); step += 1
            while time.time() < deadline:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                if isinstance(msg, (bytes, bytearray)):
                    continue
                if msg.startswith("__PROFILE__"):
                    try:
                        profile = json.loads(msg[len("__PROFILE__"):])
                    except ValueError:
                        profile = {}
                    got["profile"] = (profile.get("user_name") == name)
                    if not got["profile"]:
                        detail = f"profile arrived for {profile.get('user_name')!r}, not {name!r}"
                    break
                if msg.startswith("__"):
                    continue
                # she said something and is waiting: answer the next line of the script
                await asyncio.sleep(0.5)
                await ws.send(script[step] if step < len(script) else "text-only test client")
                step += 1
            if not got["profile"] and not detail:
                detail = f"no profile within {PROFILE_WITHIN_S}s"
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        try:
            purge(name[:8])
        except Exception:
            pass
    ok = all(got.values())
    if ok:
        detail = f"took {time.time() - t0:.0f}s"
    elif not detail:
        detail = "; ".join(k for k, v in got.items() if not v) + " missing"
    return _summ(got), ok, detail


def _summ(got: dict) -> str:
    return ", ".join(f"{k}={'y' if v else 'n'}" for k, v in got.items())
