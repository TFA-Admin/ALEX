import inspect


async def run_module(module, command, state, user_id=None):
    """Now async, and passes user_id through when a module's handle()
    accepts it. Needed for privileged, hand-authored modules (e.g. the
    real memory module) that wrap genuine per-user platform functions —
    generated modules never get here since the sandbox blocks the
    imports (asyncio/aiosqlite/db access to reach them) that would make
    async or user-aware handle() useful, but the calling convention
    itself needed to support both without breaking every existing
    sync, 2-arg, no-user-id module already installed."""
    try:
        if not hasattr(module, "handle"):
            return None, state

        handle_fn = module.handle
        accepts_user = len(inspect.signature(handle_fn).parameters) >= 3

        call = (
            handle_fn(command, state or {}, user_id)
            if accepts_user
            else handle_fn(command, state or {})
        )

        result = await call if inspect.iscoroutine(call) else call

        # ✅ If module returns (response, state)
        if isinstance(result, tuple):
            return result

        # ✅ If module returns only response
        return result, state

    except Exception as e:
        return f"Module error: {e}", state
