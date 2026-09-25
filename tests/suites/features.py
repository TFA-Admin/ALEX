"""
Features suite — deterministic, instant, no model and no database.

2026-09-25 (projects #26). Checks the module layer (features/): every
core module file loads and names itself after its file; what each owns
imports; the sandboxed modules under modules/ are discovered and adapt
into the same shape; tools do not collide with the core tools or each
other; "off" is really off (mood.note does nothing, prompt blocks and
tools vanish); outside her process everything reads as on; a broken
module is reported, never fatal; the sweep finds a module by name, both
kinds; an unprompted question waits while one is unanswered or he is
talking.

Run:  python -X utf8 -m tests.harness features
"""
import importlib
import time
from dataclasses import dataclass

from features import registry
from features.base import Feature as Base

EXPECTED = {"personality", "mood", "sight", "values", "pet", "curiosity", "retention", "author"}
EXPECTED_SANDBOXED = {"recall", "inquiry", "diagnostic_tool"}


@dataclass
class FeatureCase:
    id: str
    category: str
    expect: str
    note: str = ""


def _c(id, category, expect, note=""):
    return FeatureCase(id, category, expect, note)


CASES = [
    _c("discover_finds_all", "files", "the eight core modules"),
    _c("discover_finds_sandboxed", "files", "recall, inquiry, diagnostic_tool"),
    _c("each_names_itself", "files", "name == file, summary set, owns import"),
    _c("sandboxed_adapts", "files", "kind sandboxed, scope and version from its row, gives a command"),
    _c("tools_do_not_collide", "tools", "disjoint from core tools; unique"),
    _c("on_outside_her_process", "off", "is_on True when the registry is not started"),
    _c("off_is_off_for_mood", "off", "note() and state() read calm, touch nothing"),
    _c("prompt_blocks_skip_off_and_broken", "guard", "only the running block, in order"),
    _c("tools_route_to_owner", "tools", "run_tool by name; None for a stranger; timeout from the owner"),
    _c("emit_survives_a_raise", "guard", "a raising on() is logged, not fatal"),
    _c("status_reports_error_and_kind", "state", "error text, kind and scope in status()"),
    _c("wanted_default_on", "state", "missing -> on; enabled False -> off"),
    _c("curiosity_waits_when_he_spoke_early", "curiosity", "still awaiting"),
    _c("curiosity_keeps_waiting_on_short_turn", "curiosity", "still awaiting"),
    _c("curiosity_question_back_is_not_an_answer", "curiosity", "'what do you mean?' keeps waiting"),
    _c("unprompted_waits_for_answer_and_silence", "curiosity", "a waiting question or his speech blocks the next"),
    _c("curiosity_elaboration_requests", "curiosity", "requests to say more are recognised; answers are not"),
    _c("curiosity_repeat_is_not_an_answer", "curiosity", "his restated old statement is a repeat; the red wall answer is not"),
    _c("curiosity_started_early_is_not_an_answer", "curiosity", "words begun 10 s before she finished asking keep waiting"),
    _c("named_part_finds_module_both_kinds", "sweep", "('module','mood'); ('module','recall'); camera; None"),
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


def _fake_registry(*feats, off=(), kinds=None):
    """Point the registry at hand-made slots; returns a restore function."""
    saved = (dict(registry._slots), registry._started, dict(registry._sandbox_entries))
    registry._slots.clear()
    for f in feats:
        slot = registry._Slot(f.name, (kinds or {}).get(f.name, "core"))
        slot.feature = f
        slot.running = f.name not in off
        slot.wanted = f.name not in off
        registry._slots[f.name] = slot
    registry._started = True

    def restore():
        registry._slots.clear()
        registry._slots.update(saved[0])
        registry._started = saved[1]
        registry._sandbox_entries.clear()
        registry._sandbox_entries.update(saved[2])
    return restore


async def evaluate(case: FeatureCase):
    cid = case.id

    if cid == "discover_finds_all":
        names = set(registry.discover())
        missing = EXPECTED - names
        return ", ".join(sorted(names)), not missing, f"missing {sorted(missing)}" if missing else ""

    if cid == "discover_finds_sandboxed":
        names = set(registry.discover_sandboxed())
        missing = EXPECTED_SANDBOXED - names
        overlap = names & set(registry.discover())
        return ", ".join(sorted(names)), not missing and not overlap, f"missing {sorted(missing)} overlap {sorted(overlap)}"

    if cid == "each_names_itself":
        problems = []
        for name in registry.discover():
            try:
                mod = importlib.import_module(f"features.{name}")
                inst = getattr(mod, "Feature")()
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

    if cid == "sandboxed_adapts":
        from features.sandboxed import Sandboxed
        s = Sandboxed("recall", {"version": 3, "access_scope": "db_read", "status": "enabled"})
        restore = _fake_registry(s, kinds={"recall": "sandboxed"})
        try:
            registry._sandbox_entries["recall"] = {"version": 3, "access_scope": "db_read"}
            st = {x["name"]: x for x in registry.status()}["recall"]
        finally:
            restore()
        ok = (s.kind == "sandboxed" and s.scope == "db_read" and s.version == "v3" and s.owns == ()
              and st["kind"] == "sandboxed" and st["scope"] == "db_read" and st["version"] == "v3"
              and st["gives"] == ["command"])
        return f"{s.kind} {s.scope} {s.version} gives={st['gives']}", ok, ""

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
        except Exception:
            ok = False
        finally:
            restore()
        return "emit returned" if ok else "emit raised", ok, ""

    if cid == "status_reports_error_and_kind":
        restore = _fake_registry(_Good())
        try:
            registry._slots["good"].running = False
            registry._slots["good"].error = "start failed: no camera"
            st = {s["name"]: s for s in registry.status()}
            ok, msg = await registry.diagnose("good")
        finally:
            restore()
        good = st["good"]
        passed = ((not good["running"]) and good["error"] == "start failed: no camera" and not ok
                  and "no camera" in msg and good["kind"] == "core" and good["scope"] == "full")
        return f"{good['error']!r} kind={good['kind']} scope={good['scope']} / diagnose={ok}", passed, ""

    if cid == "wanted_default_on":
        a = registry._wants({}, "mood")
        b = registry._wants({"mood": {"enabled": False}}, "mood")
        c = registry._wants({"mood": {"enabled": True, "reload_at": 1}}, "mood")
        return f"{a} {b} {c}", (a, b, c) == (True, False, True), ""

    if cid == "curiosity_waits_when_he_spoke_early":
        feat = getattr(importlib.import_module("features.curiosity"), "Feature")()
        session = {"awaiting_curiosity_answer": "birds", "curiosity_asked_until": time.time() + 30}
        await feat.on("turn", user_id="craig", session=session, text="the sky was clear all morning today")
        return repr(session.get("awaiting_curiosity_answer")), session.get("awaiting_curiosity_answer") == "birds", ""

    if cid == "curiosity_keeps_waiting_on_short_turn":
        feat = getattr(importlib.import_module("features.curiosity"), "Feature")()
        session = {"awaiting_curiosity_answer": "birds", "curiosity_asked_until": 0}
        await feat.on("turn", user_id="craig", session=session, text="yeah ok")
        return repr(session.get("awaiting_curiosity_answer")), session.get("awaiting_curiosity_answer") == "birds", ""

    if cid == "curiosity_question_back_is_not_an_answer":
        feat = getattr(importlib.import_module("features.curiosity"), "Feature")()
        session = {"awaiting_curiosity_answer": "birds", "curiosity_asked_until": 0}
        await feat.on("turn", user_id="craig", session=session, text="what do you mean by that?")
        return repr(session.get("awaiting_curiosity_answer")), session.get("awaiting_curiosity_answer") == "birds", ""

    if cid == "curiosity_elaboration_requests":
        from features.curiosity import is_elaboration_request as f
        yes = ["what do you mean?", "Alex, can you elaborate on that?", "which question?", "say that again",
               "huh?", "I'm not sure what you're asking.", "could you please be more specific", "what?"]
        no = ["I painted my office red because I find the color relaxing.",
              "Because the room felt cold and red warms it up, that's all.",
              "I made you to be my personal assistant ALEX. You're meant to be offline and totally unique when compared to every other AI in existence."]
        wrong = [t for t in yes if not f(t)] + [t for t in no if f(t)]
        return f"{len(yes)} yes, {len(no)} no", not wrong, f"wrong: {wrong}" if wrong else ""

    if cid == "curiosity_repeat_is_not_an_answer":
        from features.curiosity import is_repeat
        earlier = ["I made you to be my personal assistant ALEX. You're meant to be offline and totally unique when compared to every other AI in existence.",
                   "what modules do you have now"]
        repeat = is_repeat("alex, i created you to be my personal assistant. i created you to be offline and totally unique from every other ai that existed. can you hear me?", earlier)
        fresh = is_repeat("I painted my office red because I fine the color relaxing.", earlier)
        return f"repeat={repeat} fresh={fresh}", repeat and not fresh, ""

    if cid == "curiosity_started_early_is_not_an_answer":
        feat = getattr(importlib.import_module("features.curiosity"), "Feature")()
        now = time.time()
        session = {"awaiting_curiosity_answer": "walls", "curiosity_asked_until": now - 2, "utterance_started_at": now - 12}
        await feat.on("turn", user_id="craig", session=session, text="the red makes the room feel calmer to me honestly")
        early = session.get("awaiting_curiosity_answer") == "walls"
        session2 = {"awaiting_curiosity_answer": "walls", "curiosity_asked_until": now - 2, "utterance_started_at": now - 1,
                    "curiosity_elaborate": None}
        # started after she finished: this one would be captured (needs the DB) — only check the gate does not hold it
        from features import curiosity as cmod
        started, until = session2["utterance_started_at"], session2["curiosity_asked_until"]
        late_ok = not (started < until - cmod.ANSWER_LEAD_S)
        return f"early_kept_waiting={early} late_passes_gate={late_ok}", early and late_ok, ""

    if cid == "unprompted_waits_for_answer_and_silence":
        from core import proactive
        from core.alex_core import alex_core
        from ws import ws_handlers
        sid = "features-suite-session"
        saved = dict(ws_handlers._active_connections)
        try:
            s = alex_core.get_session(sid)
            s["awaiting_curiosity_answer"] = "birds"
            waiting = proactive.a_question_is_waiting([sid])
            s.pop("awaiting_curiosity_answer", None)
            s["curiosity_asked_until"] = 0
            clear = proactive.a_question_is_waiting([sid])
            ws_handlers._active_connections[sid] = {"speech_started_at": time.time() - 3, "utterance_ended_at": time.time() - 30}
            talking = proactive.he_is_talking([sid])
            ws_handlers._active_connections[sid] = {"speech_started_at": time.time() - 90, "utterance_ended_at": time.time() - 60}
            quiet = proactive.he_is_talking([sid])
        finally:
            ws_handlers._active_connections.clear()
            ws_handlers._active_connections.update(saved)
            alex_core.end_session(sid)
        got = f"waiting={waiting} clear={clear} talking={talking} quiet={quiet}"
        return got, (waiting, clear, talking, quiet) == (True, False, True, False), ""

    if cid == "named_part_finds_module_both_kinds":
        from core import sweep
        feats = sorted(EXPECTED | EXPECTED_SANDBOXED)
        a = sweep.named_part("check the mood module", ["recall"], ["llm"], feats)
        b = sweep.named_part("check the recall module", ["recall"], ["llm"], feats)
        c = sweep.named_part("check the camera", ["recall"], ["llm"], feats)
        d = sweep.named_part("run a diagnostic", ["recall"], ["llm"], feats)
        got = f"{a} {b} {c} {d}"
        ok = a == ("module", "mood") and b == ("module", "recall") and c == ("part", "camera") and d is None
        return got, ok, ""

    if cid == "protected_machinery":
        from controller.versions import PROTECTED_PATHS
        ok = "features/base.py" in PROTECTED_PATHS and "features/registry.py" in PROTECTED_PATHS
        return ", ".join(p for p in PROTECTED_PATHS if p.startswith("features")), ok, ""

    return "<no such case>", False, ""
