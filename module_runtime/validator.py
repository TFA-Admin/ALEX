import ast

# ALEX must never reach the network or touch the OS/process layer from a
# self-generated module, UNLESS a specific module has been explicitly
# granted that scope through the elevated-access approval flow (2026-07-16
# — see SELF_MODIFICATION_ARCHITECTURE.md's privilege-tier addendum). This
# is a hard product constraint for anything without that grant, not a
# suggestion — see project memory "alex-project-vision".
#
# Each blocked import maps to a scope, not a flat "blocked forever" list —
# approving a module for "network" access should let it import httpx
# without also silently permitting os/subprocess. check_safety()'s
# allowed_scopes parameter is what makes an elevated-access approval
# actually change what the sandbox permits, not just record that
# something was approved. eval/exec/compile/__import__ stay unconditionally
# blocked regardless of scope — they can dynamically pull in ANY module at
# runtime, sidestepping the whole scope system.
IMPORT_SCOPES = {
    "os": "os_process", "sys": "os_process", "subprocess": "os_process",
    "shutil": "os_process", "multiprocessing": "os_process",
    "ctypes": "os_process", "importlib": "os_process", "platform": "os_process",
    "asyncio": "os_process", "pickle": "os_process", "marshal": "os_process",

    "socket": "network", "ssl": "network", "select": "network", "selectors": "network",
    "http": "network", "urllib": "network", "ftplib": "network", "smtplib": "network",
    "poplib": "network", "imaplib": "network", "telnetlib": "network", "xmlrpc": "network",
    "requests": "network", "httpx": "network", "aiohttp": "network",
    "websocket": "network", "websockets": "network",

    # Direct DB access was never actually blocked before 2026-07-15 — a
    # generated module could import sqlite3 and read/write ANY table
    # (permissions, security_events, other users' profiles), not just its
    # own module state. A module's only sanctioned storage is its own
    # state dict via get_module_state()/set_module_state(), which the
    # platform persists on its behalf — a plain module should never need
    # direct DB access to do that. Only grantable as "db" scope for
    # deliberately privileged, Claude-authored modules (e.g. the real
    # memory module wraps db.py's own functions).
    "sqlite3": "db", "aiosqlite": "db",

    # Found 2026-07-16: the scope system above only ever covered known
    # external stdlib/third-party modules — it never blocked importing
    # the project's OWN code, so `from db.db import <anything>` sidestepped
    # the entire scope system (including the sqlite3/aiosqlite block right
    # above this comment) without ever importing a blocked name directly.
    # Closed for "db" when found (the real memory module); "core" and
    # "speech" closed here too since the real diagnostics module needs
    # both (reads alex_core.systems for loaded-system status, reads
    # speech.stt_engine.FORCE_STT_CPU for STT mode) — not blanket-closing
    # every first-party package speculatively, only the ones a real
    # module has actually needed.
    "db": "db",

    # Reading this application's own internal state (which systems are
    # loaded, STT mode) is real privileged access, but a meaningfully
    # different kind of risk than raw OS/subprocess control — a separate
    # "introspection" scope keeps that distinction honest instead of
    # bundling it into "os_process" and overstating what was granted.
    "core": "introspection", "speech": "introspection",

    # Found 2026-07-16 building the inquiry (search) module: calling the
    # local LLM was never blocked either — same first-party-bypass shape
    # as db/core/speech above, closed now that a real module (inquiry,
    # synthesizing search results) actually needs it. Bundled under
    # "network" rather than a new scope: Ollama is a real network service
    # call (localhost:11434), and any module already granted "network"
    # can already reach arbitrary hosts — this adds no new risk category.
    "llm": "network",

    # 2026-07-17: the same first-party-bypass gap closed for db/core/
    # speech/llm above still existed for these four — found by auditing
    # every top-level package for what capability importing it would
    # actually grant, not by a live incident. Read/manipulate access to
    # voice profiles, the websocket layer, role/permission gates, and the
    # module system's own load/run machinery is real privileged access,
    # same risk category as core/speech above — not raw OS control, but
    # not something an ordinary sandboxed module should have by default.
    # `module_runtime` specifically: confirmed live that diagnostic_tool
    # (already granted "introspection") legitimately needs
    # module_runtime.module_loader to check whether OTHER modules load
    # correctly, as part of its own aggregator job — grantable here
    # rather than unconditionally blocked, so that real, already-approved
    # use keeps working.
    "identity": "introspection", "systems": "introspection", "ws": "introspection",
    "module_runtime": "introspection",
}

BLOCKED_IMPORTS = set(IMPORT_SCOPES.keys())

# 2026-07-17: unlike everything in IMPORT_SCOPES above, this can never be
# granted via ANY elevated-access approval, at any scope. `tools` is
# Claude's own build-approval CLI (tools/pending_builds.py) — no ordinary
# module has a legitimate reason to import it; a module that could would
# be able to propose/approve/install arbitrary code through it, bypassing
# check_safety() itself rather than just being subject to it. Same
# reasoning eval/exec/compile stay unconditionally blocked below, just
# via the import path instead of a direct call. (module_runtime was
# considered for this same unconditional list but moved to IMPORT_SCOPES
# above instead — see that entry's comment for why.)
ALWAYS_BLOCKED_IMPORTS = {"tools"}

# Calls that can dynamically pull in a blocked module or execute arbitrary
# strings, sidestepping the import-based check entirely. Never scoped —
# no declared access level makes these safe.
BLOCKED_CALLS = {"eval", "exec", "compile", "__import__"}


def _root_module(name: str) -> str:
    # "os.path" and "socket.error" must be caught by the "os"/"socket" block —
    # importing a submodule still binds and exposes the full parent package.
    return name.split(".")[0] if name else ""


def check_safety(code: str, allowed_scopes=None):
    """
    Returns (is_safe, reason). reason is None when safe, otherwise a short
    machine-readable tag: "syntax_error", "blocked_import:<name>", or
    "blocked_call:<name>". Only the blocked_import/blocked_call reasons
    represent an actual attempt to exceed the sandbox — syntax_error just
    means the generated code was malformed, which is routine and not a
    security signal.

    `allowed_scopes` (iterable of scope names, e.g. {"network"}) is only
    ever passed for a module whose elevated access has actually been
    approved via the module_build_requests.access_approved flow — an
    import whose scope is in this set is permitted; everything else in
    IMPORT_SCOPES stays blocked. Default (None/empty) preserves the
    original fully-sandboxed behavior for ordinary modules.
    """
    allowed_scopes = set(allowed_scopes or [])

    try:
        tree = ast.parse(code)
    except Exception:
        return False, "syntax_error"

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = _root_module(alias.name)
                if mod in ALWAYS_BLOCKED_IMPORTS:
                    return False, f"blocked_import:{mod}"
                if mod in BLOCKED_IMPORTS and IMPORT_SCOPES[mod] not in allowed_scopes:
                    return False, f"blocked_import:{mod}"

        elif isinstance(node, ast.ImportFrom):
            mod = _root_module(node.module)
            if mod in ALWAYS_BLOCKED_IMPORTS:
                return False, f"blocked_import:{mod}"
            if mod in BLOCKED_IMPORTS and IMPORT_SCOPES[mod] not in allowed_scopes:
                return False, f"blocked_import:{mod}"

        elif isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                return False, f"blocked_call:{func.id}"

            if isinstance(func, ast.Attribute) and func.attr in BLOCKED_CALLS:
                return False, f"blocked_call:{func.attr}"

    return True, None


def is_safe(code: str, allowed_scopes=None):
    safe, _ = check_safety(code, allowed_scopes)
    return safe
