import os
import importlib.util

from module_runtime.validator import check_safety

MODULES_PATH = "modules"

loaded_modules = {}


async def load_module(name):
    """Loads (or re-loads) a module fresh off disk, EVERY call — no
    sys.modules caching, so an edit takes effect on the very next
    invocation, no restart needed.

    2026-07-16: also re-validates the code against the module's actual
    granted access_scope (from module_registry), every single call, not
    just once at install time. Found live: check_safety() previously
    only ever ran inside tools/pending_builds.py's install flow — once a
    module was installed, this function just re-executed whatever was on
    disk with zero further scope checking, so an edit adding a genuinely
    unapproved import (confirmed real: diagnostic_tool picked up `import
    os` — os_process scope — well after its actual approved grant was
    only db/network/introspection) would silently run with full access
    and nothing would ever catch it again. Refusing to load on a
    violation, and logging it as a real security event (surfaced to the
    creator the same way blocked module builds already are), closes that
    gap for good — no future edit, by Claude or anyone, can silently
    exceed what was actually approved.
    """
    module_path = os.path.join(MODULES_PATH, name, "module.py")

    if not os.path.exists(module_path):
        return None

    with open(module_path, encoding="utf-8") as f:
        code = f.read()

    # Local imports — db.db/config.logger_config aren't safe to import at
    # this file's top level (module_loader is itself loaded very early,
    # before those are guaranteed ready, and this keeps the dependency
    # narrow to just this one check).
    from db.db import get_module_registry_entry, log_security_event

    entry = await get_module_registry_entry(name)
    allowed_scopes = None
    if entry and entry.get("access_scope"):
        allowed_scopes = {s.strip() for s in entry["access_scope"].split(",") if s.strip()}

    safe, reason = check_safety(code, allowed_scopes=allowed_scopes)
    if not safe:
        print(f"🚫 Module '{name}' failed load-time safety check: {reason}")
        try:
            await log_security_event(
                name, "module_load_blocked",
                f"'{name}' failed re-validation against its granted scope "
                f"({allowed_scopes or 'none'}): {reason}"
            )
        except Exception:
            pass  # never let audit logging itself block a load decision
        return None

    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"❌ Module load failed: {e}")
        return None

    loaded_modules[name] = module
    return module


def get_module(name):
    """Last-loaded snapshot only — does NOT trigger a fresh load or
    re-validation. Kept for callers that just want to check "has
    anything with this name ever loaded successfully" without paying
    for a reload; anything that's about to actually RUN a module should
    call load_module() instead, not this."""
    return loaded_modules.get(name)


def list_modules():
    return list(loaded_modules.keys())


async def load_all_modules():
    if not os.path.exists(MODULES_PATH):
        return

    for name in os.listdir(MODULES_PATH):
        module_dir = os.path.join(MODULES_PATH, name)

        if os.path.isdir(module_dir):
            await load_module(name)
