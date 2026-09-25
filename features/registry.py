# features/registry.py
"""
The registry: finds her features, starts the wanted ones, ticks them,
reloads them when their files change, switches them on and off on request,
and reports what is running.

Runs in HER process only. The Controller never imports her runtime; it
writes what it WANTS into the database (system_learning.features_wanted)
and reads what she REPORTS (system_learning.features_state). She polls the
wanted state every few seconds, so a switch at the Controller takes effect
while she runs, and the Controller works exactly the same when she is off
— the kill path never depends on her (SELF_MODIFICATION_ARCHITECTURE.md,
foundational decisions).

Two kinds of "off":

  wanted off   he switched it off (Controller, or "disable module X");
               it stays off across her restarts until switched on
  failed       wanted on, but start() or a reload raised; the error is
               in the state, the sweep lists it as a problem, and it is
               retried on the next reload or wanted change, not every
               five seconds

Hot reload: a feature file or any file of a module it owns changing on
disk (mtime) stops the feature, reloads those modules, and starts it
again — the same property systems/ have (core/system_manager.py). A
reload that raises leaves the feature stopped with the error recorded;
the previous code is not resurrected, because half-old half-new is worse
than off. `reload(name)` does the same on request, which is what "take it
offline, change it, bring it back" is made of.

Outside her process (the Controller, tests, the harness) nothing here is
started, and is_on() answers True for everything: the engines behave as
they always did.
"""
import asyncio
import importlib
import json
import os
import sys
import time

from config.logger_config import logger

FEATURES_DIR = os.path.dirname(os.path.abspath(__file__))
_SKIP = {"__init__", "base", "registry"}

WANTED_KEY = "features_wanted"
STATE_KEY = "features_state"

POLL_S = 5.0            # wanted-state and file checks
REPORT_S = 30.0         # how often the state is written for the Controller
TICK_TIMEOUT_S = 120.0  # a tick past this is stopped and counted as an error


class _Slot:
    def __init__(self, name: str):
        self.name = name
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
_started = False
_task = None
_last_report = 0.0


# ---------------------------------------------------------------- discovery
def discover() -> list:
    """Every features/<name>.py, in name order. Pure filesystem — safe for
    the Controller, which lists them even while she is off."""
    try:
        names = [f[:-3] for f in os.listdir(FEATURES_DIR)
                 if f.endswith(".py") and f[:-3] not in _SKIP and not f.startswith("_")]
    except OSError:
        return []
    return sorted(names)


def started() -> bool:
    return _started


def names() -> list:
    return list(_slots) if _started else discover()


def is_on(name: str) -> bool:
    """False only in her process when the feature is not running. Anywhere
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
def _files_of(slot: _Slot) -> list:
    paths = [os.path.join(FEATURES_DIR, slot.name + ".py")]
    feat = slot.feature
    for dotted in (getattr(feat, "owns", ()) or ()) if feat is not None else ():
        mod = sys.modules.get(dotted)
        f = getattr(mod, "__file__", None) if mod else None
        if f:
            paths.append(f)
    return paths


def _mtimes(slot: _Slot) -> dict:
    out = {}
    for p in _files_of(slot):
        try:
            out[p] = os.path.getmtime(p)
        except OSError:
            out[p] = 0.0
    return out


# ---------------------------------------------------------------- load
async def _load(name: str) -> _Slot:
    """Import (or re-import) features/<name>.py and the modules it owns.
    Raises on failure; callers record the error on the slot."""
    slot = _slots.get(name) or _Slot(name)
    _slots[name] = slot
    dotted = f"features.{name}"
    if dotted in sys.modules:
        module = importlib.reload(sys.modules[dotted])
    else:
        module = importlib.import_module(dotted)
    cls = getattr(module, "Feature", None)
    if cls is None:
        raise RuntimeError(f"features/{name}.py has no class Feature")
    owns = getattr(cls, "owns", ()) or ()
    for owned in owns:
        if owned in sys.modules:
            importlib.reload(sys.modules[owned])
        else:
            importlib.import_module(owned)
    feature = cls()
    if getattr(feature, "name", "") != name:
        raise RuntimeError(f"features/{name}.py names itself {feature.name!r}")
    slot.feature = feature
    slot.module = module
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
    try:
        from db.db import get_features_wanted
        return await get_features_wanted()
    except Exception as e:
        logger.warning(f"[FEATURE] could not read what is wanted: {e}")
        return {}


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
            await reload(name, _record=False)
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


# ---------------------------------------------------------------- public switches
async def enable(name: str, by: str = "", why: str = "") -> tuple:
    if name not in _slots:
        return False, f"no feature called {name}"
    try:
        from db.db import set_feature_wanted
        await set_feature_wanted(name, enabled=True, by=by, why=why)
    except Exception as e:
        logger.warning(f"[FEATURE] could not record enable of {name}: {e}")
    slot = _slots[name]
    slot.wanted = True
    ok = await _start(slot)
    return ok, (f"{name} is on" if ok else slot.error)


async def disable(name: str, by: str = "", why: str = "") -> tuple:
    if name not in _slots:
        return False, f"no feature called {name}"
    try:
        from db.db import set_feature_wanted
        await set_feature_wanted(name, enabled=False, by=by, why=why)
    except Exception as e:
        logger.warning(f"[FEATURE] could not record disable of {name}: {e}")
    slot = _slots[name]
    slot.wanted = False
    await _stop(slot)
    slot.error = ""
    return True, f"{name} is off"


async def reload(name: str, _record: bool = True) -> tuple:
    """Stop, re-import the feature and what it owns, start again if wanted.
    (ok, message)."""
    if name not in _slots and name not in discover():
        return False, f"no feature called {name}"
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
    if slot.wanted:
        ok = await _start(slot)
    else:
        ok = True
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
    """Her answer from the owning feature, or None when no running feature
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
        return False, f"no feature called {name}"
    if not slot.running:
        return False, slot.error or ("switched off" if not slot.wanted else "not running")
    try:
        result = await slot.feature.diagnose()
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1] or "")
        return bool(result), ""
    except Exception as e:
        return False, f"diagnose() raised: {e}"


def status() -> list:
    """One dict per feature, for the sweep and for features_state."""
    out = []
    for name in sorted(_slots):
        s = _slots[name]
        f = s.feature
        out.append({
            "name": name,
            "summary": getattr(f, "summary", "") if f else "",
            "owns": list(getattr(f, "owns", ()) or ()) if f else [],
            "order": getattr(f, "order", 50) if f else 50,
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
        if slot.feature is None and not slot.error:
            continue
        current = _mtimes(slot) if slot.feature is not None else {
            os.path.join(FEATURES_DIR, name + ".py"): _safe_mtime(os.path.join(FEATURES_DIR, name + ".py"))}
        changed = [p for p, m in current.items() if m > slot.mtimes.get(p, 0.0)]
        if changed:
            logger.info(f"[FEATURE] {name}: {', '.join(os.path.basename(p) for p in changed)} changed on disk — reloading")
            slot.mtimes = current
            await reload(name, _record=False)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


async def _run():
    while True:
        try:
            await _apply_wanted(await _read_wanted())
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
    global _started, _task
    wanted = await _read_wanted()
    for name in discover():
        try:
            slot = await _load(name)
        except Exception as e:
            slot = _Slot(name)
            _slots[name] = slot
            slot.error = f"load failed: {e}"
            slot.mtimes = {os.path.join(FEATURES_DIR, name + ".py"): _safe_mtime(os.path.join(FEATURES_DIR, name + ".py"))}
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
