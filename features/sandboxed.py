# features/sandboxed.py
"""
A sandboxed module (modules/<name>/module.py) seen through the same shape
as everything else in her.

2026-09-25, Craig: "why is there a separation between the built-in
modules and the other ones? Shouldn't they all be in the same basket? ...
something like a diagnostic is not core but her pet is?" Right. The
difference between recall and the pet is how each RUNS, not what it is
to her: recall's code was authored through the build flow and runs inside
the access scope he granted, re-validated on every load
(module_runtime/module_loader.py); the pet is code with full access. That
is a property of a module — its scope — not a second basket.

So this adapter puts a sandboxed module into the registry beside the
rest. What it contributes is a command (routed exactly as before by
systems/modules/system.py, systems/diagnostics, systems/inquiry — nothing
about that routing changes) and a self-check. On and off stay in
module_registry.status, where the build flow, rollback and
"disable module X" have always kept them; the registry reads that as the
module's wanted state. A reload re-validates it against its scope.
"""
from features.base import Feature as Base


class Sandboxed(Base):
    kind = "sandboxed"
    order = 70

    def __init__(self, name: str, entry: dict = None):
        super().__init__()
        entry = entry or {}
        self.name = name
        self.owns = ()
        self.scope = (entry.get("access_scope") or "").strip() or "none"
        self.version = f"v{entry.get('version')}" if entry.get("version") is not None else ""
        self.summary = ""
        self._module = None

    async def start(self):
        await super().start()
        from module_runtime.module_loader import load_module
        module = await load_module(self.name)
        if module is None:
            raise RuntimeError("failed to load — blocked by its access scope, or broken")
        self._module = module
        try:
            self.summary = str(module.help()).strip() if hasattr(module, "help") else ""
        except Exception:
            self.summary = ""
        if not self.summary:
            try:
                self.summary = str(module.init()).strip() if hasattr(module, "init") else ""
            except Exception:
                self.summary = ""
        self.summary = self.summary or f"the {self.name} module"

    async def diagnose(self):
        from module_runtime.module_loader import load_module
        from modules.diagnostic_tool.module import _call_diagnose
        module = await load_module(self.name)          # fresh, re-validated
        if module is None:
            return False, "failed to load — blocked by its access scope, or broken"
        outcome = await _call_diagnose(module)
        if outcome is None:
            return True, "loaded; no self-check of its own"
        ok, msg = outcome
        return bool(ok), msg or ""

    async def status_line(self) -> str:
        ok, msg = await self.diagnose()
        return f"{self.name} ({self.version}, sandboxed: {self.scope}) — {'ok' if ok else 'issue'}{(' — ' + msg) if msg else ''}"
