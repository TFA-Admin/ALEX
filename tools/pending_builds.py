# tools/pending_builds.py
"""
Claude's entry point for the module-building workflow (2026-07-16).

Local deepseek-coder generation was retired — it had a real capability
ceiling for anything beyond trivial scaffolds (see
SELF_MODIFICATION_ARCHITECTURE.md). Modules are now authored by Claude
directly: an approved build request just waits in `module_build_requests`
until a Claude session picks it up, reads the request, writes the code
with Write/Edit, and uses this script to validate + install + register +
resolve it.

2026-07-17: `systems/modules/system.py`'s `classify_module_gap()` — the
only thing that ever created a NEW request row via live conversation —
was removed (was costing ~2s of LLM classification on every single
turn, for implicit build-detection that stopped mattering once building
moved to Claude directly). That silently left this entire tool unusable:
`install`/`flag-access` both require an existing approved request row,
and nothing created one anymore. `propose` below is the fix — Craig
asking Claude directly to build something IS the approval now (same as
a creator's own confirmation always skipped straight to "approved"), so
proposing and approving collapse into one step instead of needing a
live classifier in between.

Usage:
    python tools/pending_builds.py propose "<module_name>" "<prompt>"
        Creates a new request, already approved (Claude authoring
        something because the creator asked directly IS the approval —
        no separate confirmation step needed). Prints the new request
        ID — use it with flag-access/install below.

    python tools/pending_builds.py list
        Shows approved-but-unresolved requests, and requests already
        flagged as needing elevated access that are still waiting on the
        creator's explicit approval for that access.

    python tools/pending_builds.py flag-access <request_id> "<description>"
        Call this INSTEAD of install when the module genuinely needs
        something beyond plain sandboxed logic (OS/process, network,
        hardware) — do not install it yourself first. This flags the
        request and leaves it for the creator to explicitly approve —
        say something like "approve request N" (2026-07-16: this is now
        a propose-then-confirm exchange, not a single exact phrase — she
        reads back what's being granted and only commits on an explicit
        "yes") — before anyone installs it. <description> should be a
        real, specific explanation (e.g.
        "needs os/subprocess to check running processes for
        diagnostics"), not a generic label — the creator sees this
        exact text when deciding.

    python tools/pending_builds.py install <request_id> [access_scope]
        Validates modules/<name>/module.py (check_safety, then actually
        runs it — a real execution test, not just "does it parse"),
        installs it, registers it in the module registry, and resolves
        the request. Write the module file with Write/Edit BEFORE running
        this. <access_scope> is optional — comma-separated if a module
        needs more than one (e.g. "db,network,introspection") — and only
        valid for a request that was already flagged AND creator-approved
        via approve_elevated_access; the script refuses to install with a
        non-empty access_scope otherwise.
"""
import asyncio
import inspect
import sys

sys.path.insert(0, ".")

from db.db import (
    init_db, fetch_approved_module_build_requests,
    fetch_requests_needing_access_approval, resolve_module_build_request,
    set_requested_access, register_module_version, get_module_registry_entry,
    create_module_build_request
)
from module_runtime.validator import check_safety
from module_runtime.module_installer import install_module
from module_runtime.module_loader import load_module


async def _get_request(request_id):
    all_approved = await fetch_approved_module_build_requests()
    for r in all_approved:
        if r["id"] == request_id:
            return r
    return None


async def _run_execution_test(code, allowed_scopes=None, test_user_id="craig"):
    """Adapted from module_generator.py's execution_test() to also
    support async, user-aware handle() — needed since Claude-authored
    privileged modules (real memory/diagnostics/etc.) aren't limited to
    the sync 2-arg contract generated modules were. Same principle as
    always: actually RUN the code, don't just check it parses."""
    safe, reason = check_safety(code, allowed_scopes)
    if not safe:
        return False, f"blocked by sandbox: {reason}"

    namespace = {}
    try:
        exec(code, namespace)
    except Exception as e:
        return False, f"code raised an exception on load: {e}"

    handle_fn = namespace.get("handle")
    if not callable(handle_fn):
        return False, "no callable handle() after running the code"

    accepts_user = len(inspect.signature(handle_fn).parameters) >= 3

    try:
        call = handle_fn("start", {}, test_user_id) if accepts_user else handle_fn("start", {})
        result = await call if inspect.iscoroutine(call) else call
    except Exception as e:
        return False, f"handle('start', {{}}) raised: {e}"

    if not isinstance(result, tuple) or len(result) != 2:
        return False, f"handle() returned {result!r}, expected a (response, state) tuple"

    response, _ = result
    if not response:
        return False, "handle() ran but returned an empty response"

    return True, None


async def cmd_propose(module_name, prompt):
    await init_db()

    request_id = await create_module_build_request(
        "claude", module_name, prompt, status="approved", origin="claude_session"
    )
    print(f"Created request #{request_id} for '{module_name}', already approved.")
    print(f"Write modules/{module_name}/module.py, then run install {request_id} "
          f"(or flag-access {request_id} first if it needs elevated access).")


async def cmd_list():
    await init_db()

    approved = await fetch_approved_module_build_requests()
    access_pending = await fetch_requests_needing_access_approval()
    access_pending_ids = {r["id"] for r in access_pending}

    ready = [r for r in approved if r["id"] not in access_pending_ids]

    print(f"=== Ready to build ({len(ready)}) ===")
    for r in ready:
        print(f"#{r['id']} '{r['module_name']}' (requested by {r['requested_by']}): {r['prompt']}")

    print(f"\n=== Waiting on creator's elevated-access approval ({len(access_pending)}) ===")
    for r in access_pending:
        print(f"#{r['id']} '{r['module_name']}': needs {r['requested_access']}")


async def cmd_flag_access(request_id, description):
    await init_db()

    req = await _get_request(request_id)
    if not req:
        print(f"Request #{request_id} not found among approved requests.")
        return

    await set_requested_access(request_id, description)
    print(f"Flagged request #{request_id} ('{req['module_name']}') as needing: {description}")
    print(f"Waiting on the creator to say \"approve request {request_id}\" (then "
          "confirm with \"yes\" when she reads back the grant) before this can be installed.")


async def cmd_install(request_id, access_scope=None):
    await init_db()

    req = await _get_request(request_id)
    if not req:
        print(f"Request #{request_id} not found among approved requests.")
        return

    if access_scope:
        access_pending = await fetch_requests_needing_access_approval()
        matching = next((r for r in access_pending if r["id"] == request_id), None)
        # Once approved, the request drops out of fetch_requests_needing_access_approval()
        # (access_approved flips to 1) — so absence here with a non-empty
        # access_scope means either it was never flagged, or it's still
        # waiting on approval. Either way, refuse rather than guess.
        if matching:
            print(f"Request #{request_id} still needs creator approval for: {matching['requested_access']}")
            print(f"Ask the creator to say \"approve request {request_id}\" first.")
            return

    module_name = req["module_name"]
    code_path = f"modules/{module_name}/module.py"

    try:
        with open(code_path, encoding="utf-8") as f:
            code = f.read()
    except FileNotFoundError:
        print(f"No file at {code_path} — write the module first with Write/Edit.")
        return

    allowed_scopes = {s.strip() for s in access_scope.split(",")} if access_scope else None

    ok, reason = await _run_execution_test(code, allowed_scopes=allowed_scopes)
    if not ok:
        print(f"Execution test failed: {reason}")
        print("Fix the module and re-run install — nothing was installed or resolved.")
        return

    success, install_reason = await install_module(
        module_name, code, req["requested_by"], allowed_scopes=allowed_scopes
    )
    if not success:
        print(f"install_module() rejected it: {install_reason}")
        return

    # Registry write MUST happen before load_module() — as of 2026-07-16,
    # load_module() re-validates the code against the module's registry
    # access_scope every call, so loading it before the registry actually
    # has this row (or still shows the OLD scope, on an update) would
    # reject a module that was just correctly approved and installed.
    version = await register_module_version(
        module_name, code, req["requested_by"], source="claude_authored",
        build_request_id=request_id, access_scope=access_scope
    )
    await load_module(module_name)

    await resolve_module_build_request(request_id, "built", f"{module_name} v{version} installed by Claude")

    print(f"Installed '{module_name}' v{version} (access_scope={access_scope or 'none'}), request #{request_id} resolved.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "propose" and len(sys.argv) >= 4:
        asyncio.run(cmd_propose(sys.argv[2], " ".join(sys.argv[3:])))
    elif cmd == "list":
        asyncio.run(cmd_list())
    elif cmd == "flag-access" and len(sys.argv) >= 4:
        asyncio.run(cmd_flag_access(int(sys.argv[2]), " ".join(sys.argv[3:])))
    elif cmd == "install" and len(sys.argv) >= 3:
        access_scope = sys.argv[3] if len(sys.argv) >= 4 else None
        asyncio.run(cmd_install(int(sys.argv[2]), access_scope))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
