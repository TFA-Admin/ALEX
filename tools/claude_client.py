# tools/claude_client.py
"""
Claude <-> A.L.E.X. channel (Component 8, first pass).

A text-only WebSocket client so Claude (running in a separate agent
environment on the same machine) can talk to A.L.E.X. directly. Registers
as an ordinary "claude" user profile — no special role, no elevated
trust, same as any other user (per the resolved design: advisory only).
She's fundamentally voice-first, but onboarding already tolerates a
client that never sends audio (identity/identity_manager.py's
receive_voice_sample() treats typed text as "give up this attempt, no
sample collected" rather than blocking), so no backend changes were
needed to make this work.

Usage:
    python tools/claude_client.py register        # one-time: creates the "claude" profile
    python tools/claude_client.py chat "message"   # send one message, print the reply, exit

Each invocation is a fresh connection — "chat" re-sends the {"user_name":
"claude"} handshake every time, which resolves instantly once the profile
exists (identity_manager.resolve_user_passive() matches by name), so no
persistent connection or session state needs to be kept between calls.
"""
import asyncio
import json
import ssl
import sys

import websockets

# She runs over WSS with a local self-signed cert (ALEX.py, certs/*.pem) —
# not verified against a CA since it's purely local/offline, same trust
# model as the rest of this project.
URI = "wss://127.0.0.1:5000/ws"
SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# Scripted replies for the one-time onboarding handshake — name, then
# confirmation. Anything after that (voice enrollment prompts) just needs
# ANY non-empty text reply to move on; receive_voice_sample() treats typed
# text as "no sample this attempt" and continues, so a generic filler is
# fine and harmless.
ONBOARDING_SCRIPT = ["claude", "yes"]
ONBOARDING_FILLER = "text-only client, no microphone available"


async def register():
    async with websockets.connect(URI, ssl=SSL_CONTEXT) as ws:
        await ws.send(json.dumps({"user_name": "claude"}))

        step = 0
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                print("Timed out waiting for A.L.E.X. — is the server running?")
                return

            print(f"<- {msg}")

            if msg.startswith("__PROFILE__"):
                print("\nRegistered — 'claude' profile created.")
                return

            if msg.startswith("__"):
                continue

            reply = ONBOARDING_SCRIPT[step] if step < len(ONBOARDING_SCRIPT) else ONBOARDING_FILLER
            step += 1
            print(f"-> {reply}")
            await ws.send(reply)


async def chat(message: str):
    async with websockets.connect(URI, ssl=SSL_CONTEXT) as ws:
        await ws.send(json.dumps({"user_name": "claude"}))

        # Drain handshake/profile messages until she's ready for real input.
        # A first-time (unregistered) connection would land in onboarding
        # here instead — run `register` once before using `chat`.
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                print("Timed out waiting for A.L.E.X. — is the server running?")
                return

            if msg.startswith("__PROFILE__"):
                break

            if not msg.startswith("__"):
                # Unregistered profile landed in onboarding instead of a
                # normal login — bail out with a clear message rather than
                # silently answering onboarding prompts as if they were chat.
                print("Not registered yet — run: python tools/claude_client.py register")
                return

        await ws.send(message)

        response = ""
        in_response = False

        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=60.0)
            except asyncio.TimeoutError:
                break

            if msg == "__START__":
                in_response = True
                continue
            if msg == "__END__":
                break
            if msg.startswith("__"):
                continue

            if in_response:
                response += msg

        print(response)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("register", "chat"):
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "register":
        asyncio.run(register())
    else:
        if len(sys.argv) < 3:
            print('Usage: python tools/claude_client.py chat "message"')
            sys.exit(1)
        asyncio.run(chat(" ".join(sys.argv[2:])))


if __name__ == "__main__":
    main()
