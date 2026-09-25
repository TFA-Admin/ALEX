# core/sweep.py
"""
A full sweep of herself, and a check of one named part.

2026-09-25, Craig: "her diagnostic I think needs some work. right now it
seems command specific, but if I'm asking for a systems check or a
diagnostic I mean everything. Unless I say 'check this module' or
something specific I would expect either a clarification on what I want
or for her to just perform a full sweep. I'm also using this as a means
to tell what she can and can't see on herself."

So: "run a diagnostic" / "systems check" / "check everything" is the
full sweep — every system's own self-check, every module's, the model,
her senses, her memory, her mood, her pet, her author, retention, the
tools she has — and, stated plainly, what she cannot see. "Check the
camera" / "check the inquiry module" / "check your memory" is that one
part. The page gets the whole report as bullets; her voice gets the
summary and the problems, not the list.

Nothing here is her opinion: every line is read from the thing itself.
"""
import os
import time

from config.logger_config import logger

CANNOT_SEE = [
    "the Controller and its consoles (it must never depend on me)",
    "the operating system, other programs, and the network beyond Ollama",
    "the GPU's own state (only whether the model answers)",
    "what a person is doing off camera or away from the microphone",
]

PART_WORDS = {
    "camera": ("camera", "eyes", "sight", "vision", "see", "face"),
    "voice": ("voice", "mic", "microphone", "hearing", "ears", "speech", "stt"),
    "memory": ("memory", "memories", "remember", "database"),
    "mood": ("mood", "feeling", "irritation", "engagement", "strain"),
    "pet": ("pet",),
    "model": ("model", "brain", "ollama", "llm", "language model"),
    "author": ("author", "proposal", "proposals"),
    "tools": ("tools", "tool"),
    "clock": ("clock", "time"),
}


def named_part(lower: str, module_names: list, system_names: list):
    """The one part he named, or None for a sweep. Module and system names
    win; then the senses and stores above."""
    for m in module_names:
        if m.lower() in lower or m.lower().replace("_", " ") in lower:
            return ("module", m)
    for s in system_names:
        if f"{s} system" in lower or f"{s}-system" in lower:
            return ("system", s)
    for key, words in PART_WORDS.items():
        if any(w in lower for w in words):
            return ("part", key)
    return None


async def _systems():
    from core.alex_core import alex_core
    from modules.diagnostic_tool.module import _discover_system_names, _call_diagnose
    loaded = alex_core.systems.systems
    out = []
    for name in _discover_system_names():
        if name not in loaded:
            out.append((name, "not loaded", ""))
            continue
        outcome = await _call_diagnose(loaded[name])
        if outcome is None:
            out.append((name, "loaded", "no self-check"))
        else:
            ok, msg = outcome
            out.append((name, "ok" if ok else "issue", msg or ""))
    return out


async def _modules():
    from db.db import list_module_registry
    from modules.diagnostic_tool.module import _call_diagnose
    out = []
    try:
        from module_runtime.module_loader import load_module
    except Exception:
        load_module = None
    for entry in await list_module_registry():
        name, status = entry["name"], entry["status"]
        ver = entry.get("version")
        if status != "enabled":
            out.append((name, ver, status, ""))
            continue
        msg = ""
        if load_module is not None:
            try:
                mod = await load_module(name)
                outcome = await _call_diagnose(mod) if mod is not None else (False, "failed to load")
                if outcome is not None:
                    ok, m = outcome
                    status = "ok" if ok else "issue"
                    msg = m or ""
            except Exception as e:
                status, msg = "issue", str(e)[:120]
        out.append((name, ver, status, msg))
    return out


async def _senses(user_id: str):
    from core import sight
    from db.db import fetch_voice_samples, fetch_face_samples
    from ws.ws_handlers import _active_connections
    try:
        voice = len(await fetch_voice_samples(user_id))
    except Exception:
        voice = 0
    try:
        face = len(await fetch_face_samples(user_id))
    except Exception:
        face = 0
    conns = list(_active_connections.values())
    return {
        "voice_samples": voice,
        "face_samples": face,
        "face_models": sight.available(),
        "eyes_open": sight.eyes_open(user_id),
        "pages": len(conns),
        "pages_with_eyes": sum(1 for c in conns if c.get("eyes")),
    }


async def _memory(user_id: str):
    import sqlite3
    from db.db import DB_PATH
    out = {}
    try:
        c = sqlite3.connect(DB_PATH)
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        out["rows"] = q("SELECT COUNT(*) FROM memory WHERE COALESCE(retracted,0)=0")
        out["today"] = q("SELECT COUNT(*) FROM memory WHERE created_at >= datetime('now','-1 day')")
        out["retracted"] = q("SELECT COUNT(*) FROM memory WHERE COALESCE(retracted,0)=1")
        out["decisions_today"] = q("SELECT COUNT(*) FROM decisions WHERE created_at >= datetime('now','-1 day')")
        out["curiosity_open"] = q("SELECT COUNT(*) FROM curiosity_queue WHERE (answer IS NULL OR answer='') AND created_at >= datetime('now','-7 days')")
        out["observations_24h"] = q("SELECT COUNT(*) FROM observations WHERE created_at >= datetime('now','-1 day')")
        out["learned_shapes"] = q("SELECT COUNT(*) FROM claim_patterns WHERE active=1")
        out["conclusions"] = q("SELECT COUNT(*) FROM conclusions")
        out["db_mb"] = round(os.path.getsize(DB_PATH) / 1e6, 1)
        c.close()
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


async def full_sweep(user_id: str) -> dict:
    t0 = time.time()
    report = {"at": time.time()}
    try:
        from core import version
        report["version"] = version.label()
    except Exception:
        report["version"] = "?"
    try:
        from llm.ollama_client import ollama_manager, DEFAULT_MODEL
        from core import readiness
        report["model"] = {"name": DEFAULT_MODEL, "ready": bool(ollama_manager.ready), "state": readiness.get_state()}
    except Exception as e:
        report["model"] = {"name": "?", "ready": False, "state": str(e)[:80]}
    try:
        report["systems"] = await _systems()
    except Exception as e:
        report["systems"] = [("systems", "issue", str(e)[:120])]
    try:
        report["modules"] = await _modules()
    except Exception as e:
        report["modules"] = [("modules", None, "issue", str(e)[:120])]
    try:
        from core.tools import TOOL_NAMES
        report["tools"] = sorted(TOOL_NAMES)
    except Exception:
        report["tools"] = []
    try:
        report["senses"] = await _senses(user_id)
    except Exception as e:
        report["senses"] = {"error": str(e)[:120]}
    report["memory"] = await _memory(user_id)
    try:
        from core import mood
        report["mood"] = mood.decayed(await mood.state())
    except Exception:
        report["mood"] = {}
    try:
        from core import pet
        report["pet"] = pet.describe(await pet.state(), pet.pet_name())
    except Exception:
        report["pet"] = ""
    try:
        from core import idle_author
        from core.self_author import WHITELIST
        from db.db import fetch_proposals
        rows = await fetch_proposals(limit=100)
        report["author"] = {"enabled": idle_author.enabled(),
                            "open": sum(1 for r in rows if r.get("status") in idle_author.OPEN_STATUSES),
                            "targets": len(WHITELIST)}
    except Exception:
        report["author"] = {}
    try:
        from db.db import get_retention_summary
        report["retention"] = await get_retention_summary()
    except Exception:
        report["retention"] = None
    problems = [f"system {n}: {m or s}" for n, s, m in report["systems"] if s in ("issue", "not loaded")]
    problems += [f"module {n}: {m or s}" for n, v, s, m in report["modules"] if s == "issue"]
    if not report["model"].get("ready"):
        problems.append(f"model {report['model'].get('name')}: not ready ({report['model'].get('state')})")
    if report["memory"].get("error"):
        problems.append("memory: " + report["memory"]["error"])
    report["problems"] = problems
    report["took"] = round(time.time() - t0, 2)
    logger.info(f"[SWEEP] full sweep for {user_id} in {report['took']}s: {len(problems)} problem(s)")
    return report


def render(r: dict) -> tuple:
    """(shown, spoken). The page gets everything; the voice gets the sum."""
    systems, modules = r.get("systems", []), r.get("modules", [])
    sys_ok = sum(1 for _n, s, _m in systems if s in ("ok", "loaded"))
    mods_on = sum(1 for _n, _v, s, _m in modules if s in ("ok", "enabled"))
    sen, mem, mood, model = r.get("senses", {}), r.get("memory", {}), r.get("mood", {}), r.get("model", {})
    lines = [f"Full sweep — A.L.E.X. {r.get('version')}", ""]
    lines.append(f"- Model: {model.get('name')} — {'ready' if model.get('ready') else 'NOT ready'} ({model.get('state')})")
    lines.append(f"- Systems: {len(systems)} found, {sys_ok} fine" + ("" if len(systems) == sys_ok else f", {len(systems) - sys_ok} with issues"))
    for n, s, m in systems:
        if s in ("issue", "not loaded"):
            lines.append(f"    - {n}: {s}{(' — ' + m) if m else ''}")
    lines.append(f"- Modules: {len(modules)} in the registry, {mods_on} enabled and fine")
    for n, v, s, m in modules:
        lines.append(f"    - {n} v{v}: {s}{(' — ' + m) if m else ''}")
    lines.append(f"- Tools I have: {', '.join(r.get('tools', [])) or 'none'}")
    if sen:
        lines.append(f"- Senses: voice samples {sen.get('voice_samples', 0)}, face samples {sen.get('face_samples', 0)}"
                     f"{'' if sen.get('face_models') else ' (face models missing)'}; eyes {'open' if sen.get('eyes_open') else 'closed'} on your page; "
                     f"{sen.get('pages', 0)} page(s) connected, {sen.get('pages_with_eyes', 0)} with eyes open")
    if mem and not mem.get("error"):
        lines.append(f"- Memory: {mem.get('rows')} turns kept ({mem.get('today')} in the last day, {mem.get('retracted')} retracted); "
                     f"{mem.get('decisions_today')} decisions today; {mem.get('curiosity_open')} open questions; "
                     f"{mem.get('observations_24h')} observations in 24 h; {mem.get('conclusions')} conclusions; "
                     f"{mem.get('learned_shapes')} learned claim shapes; database {mem.get('db_mb')} MB")
    if mood:
        lines.append("- Mood: " + ", ".join(f"{k} {v:.1f}/10" for k, v in mood.items()))
    if r.get("pet"):
        lines.append(f"- Pet: {r['pet']}")
    if r.get("author"):
        a = r["author"]
        lines.append(f"- Author: {'on' if a.get('enabled') else 'off'}, {a.get('open', 0)} proposal(s) open, {a.get('targets', 0)} targets I may change")
    if r.get("retention"):
        lines.append(f"- Retention: last ran {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(r['retention'].get('at', 0))))}")
    lines.append("- What I cannot see: " + "; ".join(CANNOT_SEE))
    probs = r.get("problems", [])
    lines.append("")
    lines.append(("Problems: " + "; ".join(probs)) if probs else "No problems found.")
    shown = "\n".join(lines)
    spoken = (f"Full sweep done in {r.get('took', 0):.0f} seconds. {len(systems)} systems, {sys_ok} fine; {len(modules)} modules, "
              f"{mods_on} enabled; model {'ready' if model.get('ready') else 'not ready'}; eyes {'open' if sen.get('eyes_open') else 'closed'}. "
              + (("Problems: " + "; ".join(probs[:3]) + ".") if probs else "No problems found.")
              + " The full report is on your screen.")
    return shown, spoken


async def check_part(user_id: str, part) -> tuple:
    kind, key = part
    if kind == "module":
        mods = [m for m in await _modules() if m[0] == key]
        if not mods:
            return f"No module called {key}.", f"I have no module called {key}."
        n, v, s, m = mods[0]
        text = f"Module {n} v{v}: {s}{(' — ' + m) if m else ''}"
        return text, text
    if kind == "system":
        systems = [s for s in await _systems() if s[0] == key]
        if not systems:
            return f"No system called {key}.", f"I have no system called {key}."
        n, s, m = systems[0]
        text = f"System {n}: {s}{(' — ' + m) if m else ''}"
        return text, text
    if key == "camera":
        sen = await _senses(user_id)
        text = (f"Camera: eyes {'open' if sen.get('eyes_open') else 'closed'} on your page; {sen.get('pages_with_eyes', 0)} of "
                f"{sen.get('pages', 0)} pages have eyes open; face samples for you: {sen.get('face_samples', 0)}; "
                f"face models {'present' if sen.get('face_models') else 'MISSING'}.")
        return text, text
    if key == "voice":
        sen = await _senses(user_id)
        text = f"Voice: {sen.get('voice_samples', 0)} enrolled samples for you; speech-to-text is on the GPU."
        return text, text
    if key == "memory":
        mem = await _memory(user_id)
        shown, _ = render({"memory": mem, "systems": [], "modules": [], "tools": [], "senses": {}, "problems": []})
        line = [ln for ln in shown.splitlines() if ln.startswith("- Memory")]
        text = line[0][2:] if line else str(mem)
        return text, text
    if key == "mood":
        from core import mood
        axes = mood.decayed(await mood.state())
        text = "Mood: " + ", ".join(f"{k} {v:.1f}/10" for k, v in axes.items())
        return text, text
    if key == "pet":
        from core import pet
        text = "Pet — " + pet.describe(await pet.state(), pet.pet_name())
        return text, text
    if key == "model":
        r = await full_sweep(user_id)
        m = r["model"]
        text = f"Model {m.get('name')}: {'ready' if m.get('ready') else 'NOT ready'} ({m.get('state')}); version {r.get('version')}"
        return text, text
    if key == "author":
        r = await full_sweep(user_id)
        a = r.get("author", {})
        text = f"Author: {'on' if a.get('enabled') else 'off'}, {a.get('open', 0)} proposal(s) open, {a.get('targets', 0)} targets I may change."
        return text, text
    if key == "tools":
        from core.tools import TOOL_NAMES
        text = "Tools I have: " + ", ".join(sorted(TOOL_NAMES))
        return text, text
    if key == "clock":
        text = "It is " + time.strftime("%A, %Y-%m-%d %H:%M local time")
        return text, text
    shown, spoken = render(await full_sweep(user_id))
    return shown, spoken
