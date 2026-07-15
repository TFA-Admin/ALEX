# core/system_base.py

"""
System Base Class

Every system MUST inherit from this.

This guarantees:
- consistent interface
- safe routing
- hot-swappability
"""


class BaseSystem:

    name = "base"
    priority = 100  # lower = higher priority

    # -------------------------
    # INIT (OPTIONAL)
    # -------------------------
    async def init(self):
        """
        Called when system is loaded
        """
        pass

    # -------------------------
    # MAIN HANDLER (REQUIRED)
    # -------------------------
    async def handle(self, session, user_id: str, input_data: dict):
        """
        MUST return one of:
        - None → not handled
        - dict → handled

        Example:
        {
            "type": "response",
            "content": "hello"
        }
        """
        raise NotImplementedError

    # -------------------------
    # OPTIONAL CLEANUP
    # -------------------------
    async def shutdown(self):
        """
        Called when system is unloaded
        """
        pass

    # -------------------------
    # OPTIONAL: SESSION HOOK
    # -------------------------
    async def on_session_start(self, session, user_id: str):
        pass

    async def on_session_end(self, session, user_id: str):
        pass