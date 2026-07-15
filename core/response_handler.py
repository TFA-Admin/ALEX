# core/response_handler.py

"""
Response Handler

Responsible for:
- consuming system outputs
- handling streaming
- sending to websocket
- triggering TTS
- triggering post-response system hooks (memory, etc.)
"""

import json

print("🔥 RESPONSE HANDLER LOADED")

from speech.tts_engine import speak
from ws.ws_utils import split_speakable_text, enrich_profile
from core.alex_core import alex_core


class ResponseHandler:

    def __init__(self):
        pass

    # -------------------------
    # MAIN OUTPUT ROUTER
    # -------------------------
    async def handle(self, websocket, result, user_id, session_id=None, input_data=None):

        print("🔥 HANDLE CALLED")

        if not result:
            return

        result_type = result.get("type")
        print(f"[DEBUG] result_type = {result_type}")

        if result_type == "stream":
            await self._handle_stream(websocket, result, user_id, session_id, input_data)
            return

        if result_type == "response":
            await self._handle_simple(websocket, result, user_id, session_id, input_data)
            return

    # -------------------------
    # STREAMING HANDLER
    # -------------------------
    async def _handle_stream(self, websocket, result, user_id, session_id, input_data):

        print("ENTERED STREAM HANDLER")

        stream_fn = result.get("stream")

        if not stream_fn:
            return

        await websocket.send_text("__START__")

        full_response = ""
        speech_buffer = ""
        spoke_any = False

        async for chunk in stream_fn():
            full_response += chunk
            speech_buffer += chunk

            await websocket.send_text(chunk)

            # 🔊 TTS chunking
            while True:
                speakable, speech_buffer = split_speakable_text(speech_buffer)

                if not speakable:
                    break

                speak(speakable)
                spoke_any = True

        # 🔄 PROFILE UPDATE
        updated = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated))

        await websocket.send_text("__END__")

        # 🔊 remaining speech
        remaining = speech_buffer.strip()

        if remaining:
            speak(remaining)
        elif not spoke_any:
            speak(full_response)

        # 🔥 AFTER RESPONSE HOOK (THIS IS THE MISSING PIECE)
        if session_id and input_data:
            session = alex_core.get_session(session_id)

            await alex_core.systems.after_response(
                session,
                user_id,
                input_data,
                full_response
            )

    # -------------------------
    # SIMPLE RESPONSE
    # -------------------------
    async def _handle_simple(self, websocket, result, user_id, session_id, input_data):

        content = result.get("content", "")

        await websocket.send_text("__START__")
        await websocket.send_text(content)

        updated = await enrich_profile(user_id)
        await websocket.send_text("__PROFILE__" + json.dumps(updated))

        await websocket.send_text("__END__")

        if content:
            speak(content)

        print("STREAM COMPLETE")

        # 🔥 AFTER RESPONSE HOOK
        if session_id and input_data:
            session = alex_core.get_session(session_id)

            await alex_core.systems.after_response(
                session,
                user_id,
                input_data,
                content
            )


# -------------------------
# SINGLETON
# -------------------------
response_handler = ResponseHandler()