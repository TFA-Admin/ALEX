import ast

# ALEX must never reach the network or touch the OS/process layer from a
# self-generated module. This is a hard product constraint, not a suggestion —
# see project memory "alex-project-vision".
BLOCKED_IMPORTS = {
    "os", "sys", "subprocess", "shutil",
    "socket", "ssl", "select", "selectors",
    "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "xmlrpc", "asyncio", "multiprocessing",
    "ctypes", "importlib", "pickle", "marshal", "platform",
    "requests", "httpx", "aiohttp", "websocket", "websockets",
}

# Calls that can dynamically pull in a blocked module or execute arbitrary
# strings, sidestepping the import-based check entirely.
BLOCKED_CALLS = {"eval", "exec", "compile", "__import__"}


def _root_module(name: str) -> str:
    # "os.path" and "socket.error" must be caught by the "os"/"socket" block —
    # importing a submodule still binds and exposes the full parent package.
    return name.split(".")[0] if name else ""


def check_safety(code: str):
    """
    Returns (is_safe, reason). reason is None when safe, otherwise a short
    machine-readable tag: "syntax_error", "blocked_import:<name>", or
    "blocked_call:<name>". Only the blocked_import/blocked_call reasons
    represent an actual attempt to exceed the sandbox — syntax_error just
    means the generated code was malformed, which is routine and not a
    security signal.
    """
    try:
        tree = ast.parse(code)
    except Exception:
        return False, "syntax_error"

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = _root_module(alias.name)
                if mod in BLOCKED_IMPORTS:
                    return False, f"blocked_import:{mod}"

        elif isinstance(node, ast.ImportFrom):
            mod = _root_module(node.module)
            if mod in BLOCKED_IMPORTS:
                return False, f"blocked_import:{mod}"

        elif isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                return False, f"blocked_call:{func.id}"

            if isinstance(func, ast.Attribute) and func.attr in BLOCKED_CALLS:
                return False, f"blocked_call:{func.attr}"

    return True, None


def is_safe(code: str):
    safe, _ = check_safety(code)
    return safe