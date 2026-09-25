# features/registry.py
"""
The registry: one list of her modules. It finds them, starts the wanted
ones, ticks them, reloads them when their files change, switches them on
and off on request, and reports what is running.

Two kinds in one basket (Craig, 2026-09-25: "shouldn't they all be in the
same basket?"):

  core        features/<name>.py — code with full access, hand-written;
              on/off lives in system_learning.features_wanted
  sandboxed   modules/<name>/module.py — authored through the build flow,
              runs inside the access scope he granted, re-validated on
              every load (module_runtime/); on/off lives in
              module_registry.status, as it always has; seen here through
              features/sandboxed.py

The kind is a property of a module (its scope), not a category she or
the Controller has to think in.

Runs in HER process only. The Controller never imports her runtime; it
writes what it WANTS into the database and reads what she REPORTS
(system_learning.features_state). She polls the wanted state every few
seconds, so a switch at the Controller takes effect while she runs, and
the Controller works exactly the same when she is off — the kill path
never depends on her.

Two kinds of "off":

  wanted off   he switched it off; it stays off across her restarts
  failed       wanted on, but start() or a reload raised; the error is
               in the state, the sweep lists it as a problem, and it is
               retried on the next reload or wanted change

Hot reload: a module's file (or, for a core one, any file it owns)
changing on disk stops it, re-imports, and starts it again — the same
property systems/ have. A reload that raises leaves it stopped with the
error recorded. `reload(name)` does the same on request: "take it
offline, change it, bring it back".

Outside her process (the Controller, tests, the harness) nothing here is
started, and is_on() answers True for everything.
"""
import asyncio
import importlib
import os
import sys
import time

from config.logger_config import logger

FEATURES_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_DIR = os.path.join(os.path.dirname(FEATURES_DIR), "modules")
_SKIP = {"__init__", "base", "registry", "sandboxed"}

WANTED_KEY = "features_wanted"
STATE_KEY = "features_state"

POLL_S = 5.0            # wanted-state and file checks
REPORT_S = 30.0         # how often the state is written for the Controller
TICK_TIMEOUT_S = 120.0  # a tick past this is stopped and counted as an error


class _Slot:
    def __init__(self, name: str, kind: str = "core"):
        self.name = name
        self.kind = kind
        self.feature = None
        self.module = None
        self.running = False
        self.wanted = True
        self.error = ""
        self.mtimes = {}
        self.loaded_at = 0.0
        self.last_tick = 0.0
        self.next_tick = 0.0
        self.tick_error = ""
        self.ticking = False
        self.reload_seen = 0.0


_slots: dict = {}
_sandbox_entries: dict = {}     # name -> module_registry row, refreshed each poll
_started = False
_task = None
_last_report = 0.0
_core_version = ""


# ---------------------------------------------------------------- discovery
def discover() -> list:
    """Core modules: every features/<name>.py, in name order. Pure
    filesystem — safe for the Controller, which lists them even while she
    is off."""
    try:
        names = [f[:-3] for f in os.listdir(FEATURES_DIR)
                 if f.endswith(".py") and f[:-3] not in _SKIP and not f.startswith("_")]
    except OSError:
        return []
    return sorted(names)


def discover_sandboxed() -> list:
    """Sandboxed modules: every modules/<name>/module.py."""
    try:
        return sorted(d for d in os.listdir(MODULES_DIR)
                      if os.path.isfile(os.path.join(MODULES_DIR, d, "module.py")))
    except OSError:
        return []


def started() -> bool:
    return _started


def names() -> list:
    return list(_slots) if _started else sorted(set(discover()) | set(discover_sandboxed()))


def kind_of(name: str) -> str:
    s = _slots.get(name)
    if s is not None:
        return s.kind
    return "core" if name in discover() else ("sandboxed" if name in discover_sandboxed() else "")


def is_on(name: str) -> bool:
    """False only in her process when the module is not running. Anywhere
    the registry is not started, everything is on."""
    if not _started:
        return True
    s = _slots.get(name)
    return bool(s and s.running)


def get(name: str):
    s = _slots.get(name)
    return s.feature if (s and s.running) else None


def running() -> list:
    out = [s.feature for s in _slots.values() if s.running and s.feature is not None]
    out.sort(key=lambda f: (getattr(f, "order", 50), f.name))
    return out


# ---------------------------------------------------------------- files
def _own_file(slot: _Slot) -> str:
    if slot.kind == "sandboxed":
        return os.path.join(MODULES_DIR, slot.name, "module.py")
    return os.path.join(FEATURES_DIR, slot.name + ".py")


def _files_of(slot: _Slot) -> list:
    paths = [_own_file(slot)]
    feat = slot.feature
    for dotted in (getattr(feat, "owns", ()) or ()) if feat is not None else ():
        mod = sys.modules.get(dotted)
        f = getattr(mod, "__file__", None) if mod else None
        if f:
            paths.append(f)
    return paths


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _mtimes(slot: _Slot) -> dict:
    return {p: _safe_mtime(p) for p in _files_of(slot)}


# ---------------------------------------------------------------- load
async def _load(name: str) -> _Slot:
    """Import (or re-import) a module and, for a core one, what it owns.
    Raises on failure; callers record the error on the slot."""
    kind = _slots[name].kind if name in _slots else ("sandboxed" if name in _sandbox_entries or
                                                     (name not in discover() and name in discover_sandboxed())
                                                     else "core")
    slot = _slots.get(name) or _Slot(name, kind)
    _slots[name] = slot
    if kind == "sandboxed":
        from features.sandboxed import Sandboxed
        feature = Sandboxed(name, _sandbox_entries.get(name))
        slot.module = None
    else:
        dotted = f"features.{name}"
        if dotted in sys.modules:
            module = importlib.reload(sys.modules[dotted])
        else:
            module = importlib.import_module(dotted)
        cls = getattr(module, "Feature", None)
        if cls is None:
            raise RuntimeError(f"features/{name}.py has no class Feature")
        for owned in getattr(cls, "owns", ()) or ():
            if owned in sys.modules:
                importlib.reload(sys.modules[owned])
            else:
                importlib.import_module(owned)
        feature = cls()
        if getattr(feature, "name", "") != name:
            raise RuntimeError(f"features/{name}.py names itself {feature.name!r}")
        slot.module = module
    slot.feature = feature
    slot.loaded_at = time.time()
    slot.mtimes = _mtimes(slot)
    return slot


async def _start(slot: _Slot) -> bool:
    if slot.running or slot.feature is None:
        return slot.running
    try:
        await slot.feature.start()
        slot.running = True
        slot.error = ""
        slot.tick_error = ""
        every = getattr(slot.feature, "tick_every_s", None)
        slot.next_tick = (time.time() + float(getattr(slot.feature, "first_tick_after_s", 60.0))) if every else 0.0
        logger.info(f"[FEATURE] {slot.name} started")
        return True
    except Exception as e:
        slot.running = False
        slot.error = f"start failed: {e}"
        logger.warning(f"[FEATURE] {slot.name} {slot.error}")
        return False


async def _stop(slot: _Slot):
    if not slot.running:
        return
    try:
        await slot.feature.stop()
    except Exception as e:
        logger.warning(f"[FEATURE] {slot.name} stop raised: {e}")
    slot.running = False
    logger.info(f"[FEATURE] {slot.name} stopped")


# ---------------------------------------------------------------- wanted (DB)
async def _read_wanted() -> dict:
    """One dict, name -> {"enabled", "reload_at", ...}: core from
    features_wanted, sandboxed from module_registry.status (plus any
    reload request the Controller left in features_wanted)."""
    global _sandbox_entries
    wanted = {}
    try:
        from db.db import get_features_wanted
        wanted = dict(await get_features_wanted())
    except Exception as e:
        logger.warning(f"[FEATURE] could not read what is wanted: {e}")
    try:
        from db.db import list_module_registry
        entries = {m["name"]: m for m in await list_module_registry()}
        _sandbox_entries = entries
        for name, m in entries.items():
            entry = dict(wanted.get(name) or {})
            entry["enabled"] = (m.get("status") == "enabled")
            wanted[name] = entry
    except Exception as e:
        logger.warning(f"[FEATURE] could not read the module registry: {e}")
    return wanted


def _wants(wanted: dict, name: str) -> bool:
    entry = wanted.get(name) or {}
    return bool(entry.get("enabled", True))


async def _apply_wanted(wanted: dict):
    changed = False
    for name, slot in list(_slots.items()):
        want = _wants(wanted, name)
        reload_at = float((wanted.get(name) or {}).get("reload_at") or 0.0)
        if reload_at > slot.reload_seen:
            slot.reload_seen = reload_at
            slot.wanted = want
            await reload(name)
            continue
        if want == slot.wanted:
            continue
        slot.wanted = want
        changed = True
        if want:
            await _start(slot)
        else:
            await _stop(slot)
            slot.error = ""
    if changed:
        await _report(force=True)       # so the Controller sees it now, not in REPORT_S


async def _discover_new(wanted: dict):
    """A module installed while she runs (the build flow writes
    modules/<name>/module.py and a registry row) joins the list."""
    for name in discover_sandboxed():
        if name in _slots:
            continue
        if name in discover():
            logger.warning(f"[FEATURE] modules/{name} has the same name as features/{name}.py — ignored")
            continue
        try:
            slot = await _load(name)
        except Exception as e:
            slot = _Slot(name, "sandboxed")
            _slots[name] = slot
            slot.error = f"load failed: {e}"
            slot.mtimes = {_own_file(slot): _safe_mtime(_own_file(slot))}
            continue
        slot.wanted = _wants(wanted, name)
        if slot.wanted:
            await _start(slot)
        await _report(force=True)


# ---------------------------------------------------------------- public switches
async def _record_wanted(name: str, enabled: bool, by: str, why: str):
    slot = _slots.get(name)
    try:
        if slot is not None and slot.kind == "sandboxed":
            from db.db import set_module_status
            await set_module_status(name, "enabled" if enabled else "disabled")
        else:
            from db.db import set_feature_wanted
            await set_feature_wanted(name, enabled=enabled, by=by, why=why)
    except Exception as e:
        logger.warning(f"[FEATURE] could not record {'enable' if enabled else 'disable'} of {name}: {e}")


async def enable(name: str, by: str = "", why: str = "") -> tuple:
    if name not in _slots:
        return False, f"no module called {name}"
    await _record_wanted(name, True, by, why)
    slot = _slots[name]
    slot.wanted = True
    ok = await _start(slot)
    await _report(force=True)
    return ok, (f"{name} is on" if ok else slot.error)


async def disable(name: str, by: str = "", why: str = "") -> tuple:
    if name not in _slots:
        return False, f"no module called {name}"
    await _record_wanted(name, False, by, why)
    slot = _slots[name]
    slot.wanted = False
    await _stop(slot)
    slot.error = ""
    await _report(force=True)
    return True, f"{name} is off"


async def reload(name: str) -> tuple:
    """Stop, re-import the module (and what a core one owns), start again
    if wanted. (ok, message)."""
    if name not in _slots and name not in discover() and name not in discover_sandboxed():
        return False, f"no module called {name}"
    slot = _slots.get(name)
    if slot is not None:
        await _stop(slot)
    try:
        slot = await _load(name)
    except Exception as e:
        slot = _slots.get(name) or _Slot(name)
        _slots[name] = slot
        slot.running = False
        slot.error = f"reload failed: {e}"
        logger.warning(f"[FEATURE] {name} {slot.error}")
        await _report(force=True)
        return False, slot.error
    ok = await _start(slot) if slot.wanted else True
    logger.info(f"[FEATURE] {name} reloaded{'' if slot.wanted else ' (kept off)'}")
    await _report(force=True)
    return ok, (f"{name} reloaded" + ("" if slot.wanted else ", still off")) if ok else slot.error


# ---------------------------------------------------------------- what she gets
async def prompt_blocks(**kw) -> str:
    out = []
    for feature in running():
        try:
            block = await feature.prompt_block(**kw)
        except Exception as e:
            logger.warning(f"[FEATURE] {feature.name}.prompt_block failed: {e}")
            continue
        if block:
            out.append(block)
    return "".join(out)


def tools() -> list:
    specs = []
    for feature in running():
        try:
            specs.extend(feature.tools() or [])
        except Exception as e:
            logger.warning(f"[FEATURE] {feature.name}.tools failed: {e}")
    return specs


def tool_names() -> set:
    return {t["function"]["name"] for t in tools()}


def _owner_of(tool_name: str):
    for feature in running():
        try:
            if any(t["function"]["name"] == tool_name for t in feature.tools() or []):
                return feature
        except Exception:
            continue
    return None


def tool_timeout(tool_name: str, default: float) -> float:
    owner = _owner_of(tool_name)
    if owner is None:
        return default
    try:
        t = owner.tool_timeout(tool_name)
    except Exception:
        t = None
    return float(t) if t else default


async def run_tool(tool_name: str, args: dict, user_id: str):
    """Her answer from the owning module, or None when no running module
    has a tool by that name."""
    owner = _owner_of(tool_name)
    if owner is None:
        return None
    return await owner.run_tool(tool_name, args or {}, user_id)


async def emit(event: str, **kw):
    for feature in running():
        try:
            await feature.on(event, **kw)
        except Exception as e:
            logger.warning(f"[FEATURE] {feature.name}.on({event}) failed: {e}")


# ---------------------------------------------------------------- looking at them
async def diagnose(name: str) -> tuple:
    slot = _slots.get(name)
    if slot is None:
        return False, f"no module called {name}"
    if not slot.running:
        return False, slot.error or ("switched off" if not slot.wanted else "not running")
    try:
        result = await slot.feature.diagnose()
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1] or "")
        return bool(result), ""
    except Exception as e:
        return False, f"diagnose() raised: {e}"


def _gives(slot: _Slot) -> list:
    """What a module contributes, read from what it overrides."""
    from features.base import Feature as Base
    f = slot.feature
    if f is None:
        return []
    if slot.kind == "sandboxed":
        return ["command"]
    out = []
    cls = type(f)
    if cls.prompt_block is not Base.prompt_block:
        out.append("prompt")
    try:
        tn = [t["function"]["name"] for t in f.tools() or []]
    except Exception:
        tn = []
    if tn:
        out.append("tools: " + ", ".join(tn))
    if getattr(f, "tick_every_s", None):
        out.append("tick")
    if cls.on is not Base.on:
        out.append("events")
    if cls.start is not Base.start:
        out.append("task")
    return out


def status() -> list:
    """One dict per module, for the sweep and for features_state."""
    out = []
    for name in sorted(_slots):
        s = _slots[name]
        f = s.feature
        if s.kind == "sandboxed":
            entry = _sandbox_entries.get(name) or {}
            scope = (entry.get("access_scope") or "").strip() or "none"
            version = f"v{entry.get('version')}" if entry.get("version") is not None else ""
        else:
            scope = "full"
            version = _core_version
        out.append({
            "name": name,
            "kind": s.kind,
            "scope": scope,
            "version": version,
            "summary": getattr(f, "summary", "") if f else "",
            "owns": list(getattr(f, "owns", ()) or ()) if f else [],
            "order": getattr(f, "order", 50) if f else 50,
            "gives": _gives(s) if s.running else [],
            "tools": [t["function"]["name"] for t in (f.tools() if (f and s.running) else [])],
            "wanted": s.wanted,
            "running": s.running,
            "error": s.error,
            "tick_every_s": getattr(f, "tick_every_s", None) if f else None,
            "last_tick": s.last_tick,
            "tick_error": s.tick_error,
            "loaded_at": s.loaded_at,
        })
    return out


async def _report(force: bool = False):
    global _last_report
    now = time.time()
    if not force and now - _last_report < REPORT_S:
        return
    _last_report = now
    try:
        from db.db import set_features_state
        await set_features_state({"at": now, "pid": os.getpid(), "features": status()})
    except Exception as e:
        logger.warning(f"[FEATURE] could not write the state: {e}")


# ---------------------------------------------------------------- the loop
async def _tick(slot: _Slot):
    if slot.ticking or not slot.running:
        return
    slot.ticking = True
    try:
        await asyncio.wait_for(slot.feature.tick(), timeout=TICK_TIMEOUT_S)
        slot.tick_error = ""
    except asyncio.TimeoutError:
        slot.tick_error = f"tick took longer than {TICK_TIMEOUT_S:.0f}s and was stopped"
        logger.warning(f"[FEATURE] {slot.name}: {slot.tick_error}")
    except Exception as e:
        slot.tick_error = f"tick failed: {e}"
        logger.warning(f"[FEATURE] {slot.name}: {slot.tick_error}")
    finally:
        slot.ticking = False
        slot.last_tick = time.time()
        every = getattr(slot.feature, "tick_every_s", None) if slot.feature else None
        if every:
            slot.next_tick = slot.last_tick + float(every)


async def _hot_reload():
    for name, slot in list(_slots.items()):
        current = _mtimes(slot) if slot.feature is not None else {_own_file(slot): _safe_mtime(_own_file(slot))}
        changed = [p for p, m in current.items() if m > slot.mtimes.get(p, 0.0)]
        if changed:
            logger.info(f"[FEATURE] {name}: {', '.join(os.path.basename(p) for p in changed)} changed on disk — reloading")
            slot.mtimes = current
            await reload(name)


async def _run():
    while True:
        try:
            wanted = await _read_wanted()
            await _apply_wanted(wanted)
            await _discover_new(wanted)
            await _hot_reload()
            now = time.time()
            for slot in list(_slots.values()):
                if slot.running and slot.next_tick and now >= slot.next_tick and not slot.ticking:
                    asyncio.create_task(_tick(slot))
            await _report()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[FEATURE] registry pass failed: {e}")
        await asyncio.sleep(POLL_S)


async def start_all():
    """From main.py's lifespan, after the database and the systems."""
    global _started, _task, _core_version
    try:
        from core import version
        _core_version = version.label()
    except Exception:
        _core_version = ""
    wanted = await _read_wanted()
    core = discover()
    for name in core + [n for n in discover_sandboxed() if n not in core]:
        kind = "core" if name in core else "sandboxed"
        try:
            _slots[name] = _Slot(name, kind)
            slot = await _load(name)
        except Exception as e:
            slot = _slots[name]
            slot.error = f"load failed: {e}"
            slot.mtimes = {_own_file(slot): _safe_mtime(_own_file(slot))}
            logger.warning(f"[FEATURE] {name} {slot.error}")
            continue
        slot.wanted = _wants(wanted, name)
        slot.reload_seen = float((wanted.get(name) or {}).get("reload_at") or 0.0)
        if slot.wanted:
            await _start(slot)
        else:
            logger.info(f"[FEATURE] {name} is switched off (his setting)")
    _started = True
    _task = asyncio.create_task(_run())
    await _report(force=True)
    on = [n for n, s in _slots.items() if s.running]
    off = [n for n, s in _slots.items() if not s.running]
    logger.info(f"[FEATURE] {len(on)} running ({', '.join(on)})" + (f"; off: {', '.join(off)}" if off else ""))


async def stop_all():
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
    for slot in list(_slots.values()):
        await _stop(slot)
    await _report(force=True)
