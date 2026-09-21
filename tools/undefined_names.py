# tools/undefined_names.py
"""
A cheap undefined-name sweep, because pyflakes is not installed here and
py_compile cannot catch a NameError.

2026-09-21: found the onboarding crash — six `user_id`s in a function
whose parameter is `temp_user_id`, compiled fine, and closed every new
user's socket with nothing in her log. This would have caught it in a
second. Run it before committing a code change:

    python -X utf8 tools/undefined_names.py

It reports names LOADED in a function that are not its parameters or
locals, an enclosing function's, the module's globals or imports, or
builtins. Nested functions see their parents' names (closures), so the
usual `async def _run():` inside a handler is not a false positive. It
does not follow star-imports or names injected at runtime; treat a report
as something to look at, not proof.
"""
import ast
import builtins
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = ["core", "systems", "ws", "identity", "speech", "db", "llm", "modules",
        "module_runtime", "controller", "tools", "tests", "config", "api", "utils"]
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__builtins__"}


def _module_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    for node in tree.body:
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
    return names


def _own_names(fn):
    """Parameters and everything this function body binds, NOT descending
    into nested functions (they get their own scope, layered on this)."""
    names = set()
    args = fn.args
    for a in args.args + args.kwonlyargs + getattr(args, "posonlyargs", []):
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(child.name)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for a in child.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(child, ast.comprehension):
                for t in ast.walk(child.target):
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                names.update(child.names)
            walk(child)
    walk(fn)
    return names


def _check_fn(fn, scope, module, prefix, out):
    local = scope | _own_names(fn)

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _check_fn(child, local, module, prefix + fn.name + ".", out)
                continue
            if isinstance(child, ast.Lambda):
                lam = set(a.arg for a in child.args.args + child.args.kwonlyargs)
                for n in ast.walk(child.body):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                        if n.id not in local | lam | module | BUILTINS:
                            out.append((n.lineno, prefix + fn.name, n.id))
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id not in local and child.id not in module and child.id not in BUILTINS:
                    out.append((child.lineno, prefix + fn.name, child.id))
            walk(child)
    walk(fn)


def check_file(path) -> int:
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"{os.path.relpath(path, ROOT)}: SYNTAX {e}")
        return 1
    module = _module_names(tree)
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _check_fn(node, set(), module, "", out)
        elif isinstance(node, ast.ClassDef):
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _check_fn(m, set(), module, node.name + ".", out)
    seen = set()
    for lineno, fn, name in sorted(out):
        if (fn, name) in seen:
            continue
        seen.add((fn, name))
        print(f"{os.path.relpath(path, ROOT)}:{lineno}: {fn} uses undefined {name!r}")
    return len(seen)


def main():
    total = 0
    for path in glob.glob(os.path.join(ROOT, "*.py")):      # main.py, ALEX.py, the launcher
        total += check_file(path)
    for d in DIRS:
        for path in glob.glob(os.path.join(ROOT, d, "**", "*.py"), recursive=True):
            if "__pycache__" in path or os.sep + "dormant" + os.sep in path:
                continue
            total += check_file(path)
    print(f"{'clean' if not total else total} — undefined-name sweep done")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
