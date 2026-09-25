"""
Features suite — deterministic, instant, no model and no database.

2026-09-25 (projects #26). Checks the built-in module layer (features/):
every feature file loads and names itself after its file; what each owns
imports; their tools do not collide with the core tools or each other;
"off" is really off (mood.note does nothing, prompt blocks and tools
vanish); outside her process everything reads as on; a broken feature is
reported, never fatal; the sweep finds a feature by name.

Run:  python -X utf8 -m tests.harness features
"""
import asyncio
import importlib
from dataclasses import dataclass

from features import registry
from features.base import Feature as Base

EXPECTED = {"personality", "mood", "sight", "values", "pet", "curiosity", "retention", "author"}


@dataclass
class FeatureCase:
    id: str
    category: str
    expect: str
    note: str = ""


def _c(id, category, expect, note=""):
    return FeatureCase(id, category, expect, note)


CASES = [
    _c("discover_finds_all", "files", "the eight built-ins"),
    _c("each_names_itself", "files", "name == file, summary set, owns import"),
    _c("tools_do_not_collide", "tools", "disjoint from core tools; unique"),
    _c("on_outside_her_process", "off", "is_on True when the registry is not started"),
    _c("off_is_off_for_mood", "off", "note() and state() read calm, touch nothing"),
    _c("prompt_blocks_skip_off_and_broken", "guard", "only the running block, in order"),
    _c("tools_route_to_owner", "tools", "run_tool by name; None for a stranger; timeout from the owner"),
    _c("emit_survives_a_raise", "guard", "a raising on() is logged, not fatal"),
    _c("status_reports_error", "state", "error text in status()"),
    _c("wanted_default_on", "state", "missing -> on; enabled False -> off"),
    _c("curiosity_waits_when_he_spoke_early", "curiosity", "still awaiting"),
    _c("curiosity_keeps_waiting_on_short_turn", "curiosity", "still awaiting"),
    _c("named_part_finds_feature", "sweep", "('feature','mood'); camera; recall"),
    _c("protected_machinery", "author", "features/base.py and registry.py protected"),
]


class _Good(Base):
    name = "good"
    order = 5
    tick_every_s = None

    async def prompt_block(self, **kw):
        return "\n\n    GOOD"

    def tools(self):
        return [{"type": "function", "function": {"name": "good_tool", "description": "", "parameters": {}}}]

    def tool_timeout(self, name):
        return 77

    async def run_tool(self, name, args, user_id):
        return f"good ran {name} for {user_id}"


class _Broken(Base):
    name = "broken"
    order = 1

    async def prompt_block(self, **kw):
        raise RuntimeError("boom")

    async def on(self, event, **kw):
        raise RuntimeError("boom")


class _Late(Base):
    name = "late"
    order = 90

    async def prompt_block(self, **kw):
        return "\n\n    LATE"


def _fake_registry(*feats, off=()):
    """Point the registry at hand-made slots; returns a restore function."""
    saved = (dict(registry._slots), registry._started)
    registry._slots.clear()
    for f in feats:
        slot = registry._Slot(f.name)
        slot.feature = f
        slot.running = f.name not in off
        slot.wanted = f.name not in off
        registry._slots[f.name] = slot
    registry._started = True

    def restore():
        registry._slots.clear()
        registry._slots.update(saved[0])
        registry._started = saved[1]
    return restore


async def evaluate(case: FeatureCase):
    cid = case.id

    if cid == "discover_finds_all":
        names = set(registry.discover())
        missing = EXPECTED - names
        return ", ".join(sorted(names)), not missing, f"missing {sorted(missing)}" if missing else ""

    if cid == "each_names_itself":
        problems = []
        for name in registry.discover():
            try:
                mod = importlib.import_module(f"features.{name}")
                cls = getattr(mod, "Feature")
                inst = cls()
                if inst.name != name:
                    problems.append(f"{name} names itself {inst.name!r}")
                if not inst.summary.strip():
                    problems.append(f"{name} has no summary")
                if not isinstance(inst, Base):
                    problems.append(f"{name} is not a Feature")
                for owned in inst.owns:
                    importlib.import_module(owned)
            except Exception as e:
                problems.append(f"{name}: {type(e).__name__}: {e}")
        return f"{len(registry.discover())} checked", not problems, "; ".join(problems)

    if cid == "tools_do_not_collide":
        from core.tools import TOOL_NAMES
        seen, clashes = set(), []
        for name in registry.discover():
            inst = getattr(importlib.import_module(f"features.{name}"), "Feature")()
            for spec in inst.tools() or []:
                tn = spec["function"]["name"]
                if tn in TOOL_NAMES or tn in seen:
                    clashes.append(f"{name}.{tn}")
                seen.add(tn)
        return ", ".join(sorted(seen)), not clashes and bool(seen), f"clashes {clashes}" if clashes else ""

    if cid == "on_outside_her_process":
        saved = registry._started
        registry._started = False
        try:
            got = registry.is_on("mood"), registry.is_on("nothing_like_this")
        finally:
            registry._started = saved
        return str(got), got == (True, True), ""

    if cid == "off_is_off_for_mood":
        from core import mood
        restore = _fake_registry()          # started, nothing running
        try:
            s = await mood.note("corrected", who="sam", creator=False)
            st = await mood.state()
            calm = all(v == 0 for v in s["axes"].values()) and all(v == 0 for v in st["axes"].values())
            on = registry.is_on("mood")
        finally:
            restore()
        return f"is_on={on} axes={s['axes']}", (not on) and calm, ""

    if cid == "prompt_blocks_skip_off_and_broken":
        restore = _fake_registry(_Broken(), _Good(), _Late(), off=("late",))
        try:
            got = await registry.prompt_blocks(user_id="u", session={}, text="", context_blocks=[])
        finally:
            restore()
        return repr(got), got == "\n\n    GOOD", ""

    if cid == "tools_route_to_owner":
        restore = _fake_registry(_Good(), _Broken())
        try:
            r1 = await registry.run_tool("good_tool", {}, "craig")
            r2 = await registry.run_tool("no_such_tool", {}, "craig")
            t1 = registry.tool_timeout("good_tool", 20.0)
            t2 = registry.tool_timeout("no_such_tool", 20.0)
            names = registry.tool_names()
        finally:
            restore()
        ok = r1 == "good ran good_tool for craig" and r2 is None and t1 == 77.0 and t2 == 20.0 and names == {"good_tool"}
        return f"{r1!r} {r2!r} {t1} {t2} {sorted(names)}", ok, ""

    if cid == "emit_survives_a_raise":
        restore = _fake_registry(_Broken(), _Good())
        try:
            await registry.emit("reply", text="x")
            ok = True
        except Exception as e:
            ok = False
        finally:
            restore()
        return "emit returned" if ok else "emit raised", ok, ""

    if cid == "status_reports_error":
        restore = _fake_registry(_Good())
        try:
            registry._slots["good"].running = False
            registry._slots["good"].error = "start failed: no camera"
            st = {s["name"]: s for s in registry.status()}
            ok, msg = await registry.diagnose("good")
        finally:
            restore()
        good = st["good"]
        passed = (not good["running"]) and good["error"] == "start failed: no camera" and not ok and "no camera" in msg
        return f"{good['error']!r} / diagnose={ok},{msg!r}", passed, ""

    if cid == "wanted_default_on":
        a = registry._wants({}, "mood")
        b = registry._wants({"mood": {"enabled": False}}, "mood")
        c = registry._wants({"mood": {"enabled": True, "reload_at": 1}}, "mood")
        return f"{a} {b} {c}", (a, b, c) == (True, False, True), ""

    if cid == "curiosity_waits_when_he_spoke_early":
        import time
        feat = getattr(importlib.import_module("features.curiosity"), "Feature")()
        session = {"awaiting_curiosity_answer": "birds", "curiosity_asked_until": time.time() + 30}
        await feat.on("turn", user_id="craig", session=session, text="the sky was clear all morning today")
        return repr(session.get("awaiting_curiosity_answer")), session.get("awaiting_curiosity_answer") == "birds", ""

    if cid == "curiosity_keeps_waiting_on_short_turn":
        feat = getattr(importlib.import_module("features.curiosity"), "Feature")()
        session = {"awaiting_curiosity_answer": "birds", "curiosity_asked_until": 0}
        await feat.on("turn", user_id="craig", session=session, text="yeah ok")
        return repr(session.get("awaiting_curiosity_answer")), session.get("awaiting_curiosity_answer") == "birds", ""

    if cid == "named_part_finds_feature":
        from core import sweep
        feats = sorted(EXPECTED)
        a = sweep.named_part("check the mood module", [], ["llm"], feats)
        b = sweep.named_part("check the camera", [], ["llm"], feats)
        c = sweep.named_part("check the recall module", ["recall"], ["llm"], feats)
        d = sweep.named_part("run a diagnostic", ["recall"], ["llm"], feats)
        got = f"{a} {b} {c} {d}"
        ok = a == ("feature", "mood") and b == ("part", "camera") and c == ("module", "recall") and d is None
        return got, ok, ""

    if cid == "protected_machinery":
        from controller.versions import PROTECTED_PATHS
        ok = "features/base.py" in PROTECTED_PATHS and "features/registry.py" in PROTECTED_PATHS
        return ", ".join(p for p in PROTECTED_PATHS if p.startswith("features")), ok, ""

    return "<no such case>", False, ""
