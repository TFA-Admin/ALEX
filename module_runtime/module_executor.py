def run_module(module, command, state):
    try:
        if hasattr(module, "handle"):
            result = module.handle(command, state or {})

            # ✅ If module returns (response, state)
            if isinstance(result, tuple):
                return result

            # ✅ If module returns only response
            return result, state

    except Exception as e:
        return f"Module error: {e}", state

    return None, state