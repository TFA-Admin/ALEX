# ws/ws_chat.py

import asyncio

from core.alex_core import alex_core
from core.response_handler import response_handler


# -------------------------
# MAIN CHAT HANDLER
# -------------------------
async def handle_chat(websocket, msg, user_id, session_id):

    print("🔥 HANDLE_CHAT ENTERED")

    input_data = {
        "type": "text",
        "text": msg
    }

    result = await alex_core.handle_input(
        session_id=session_id,
        user_id=user_id,
        input_data=input_data
    )

    print("🔥 RESULT FROM CORE:", result)

    await response_handler.handle(
        websocket=websocket,
        result=result,
        user_id=user_id,
        session_id=session_id,
        input_data=input_data
    )

    print("🔥 RESPONSE_HANDLER FINISHED")