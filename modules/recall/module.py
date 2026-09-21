from db.db import fetch_recent_memory, fetch_vector_memories


def init():
    return "memory module ready — backed by real conversation history"


async def diagnose():
    """Real self-check, not a presence check — actually exercises the
    same DB call handle() depends on, against a real known user, so a
    genuine break (bad query, DB issue) shows up in a diagnostic run
    instead of only being discovered the next time someone asks to
    recall something."""
    try:
        await fetch_recent_memory("craig", limit=1)
        return True, ""
    except Exception as e:
        return False, str(e)


async def handle(command, state, user_id=None):
    if state is None:
        state = {}

    if not user_id:
        return "I don't know whose memory to check.", state

    # "in cmd" everywhere, never startswith()/exact-equality — the
    # classifier passes the user's full sentence through as the command
    # ("use your memory module list memories"), not just the keyword, so
    # anything anchored to position 0 or requiring an exact match misses
    # real phrasing. Confirmed live: the same bug already fixed once
    # tonight in the base generation scaffold, reproduced here by hand.
    cmd = command.lower().strip()

    if "about" in cmd and ("recall" in cmd or "remember" in cmd):
        topic = cmd.rsplit("about", 1)[1].strip()

        if not topic:
            return "What should I recall?", state

        memories = await fetch_vector_memories(user_id)
        matches = [
            m for m in memories
            if topic in m["prompt"].lower() or topic in m["response"].lower()
        ]

        if not matches:
            return f"I don't have anything stored about '{topic}'.", state

        lines = [f"- {m['prompt']} -> {m['response']}" for m in matches[:5]]
        return f"Here's what I remember about '{topic}':\n" + "\n".join(lines), state

    if "remember" in cmd or "recall" in cmd or "memories" in cmd:
        recent = await fetch_recent_memory(user_id, limit=10)

        if not recent:
            return "I don't have any stored memory for you yet.", state

        # 2026-09-20: rendered as speech, and the internal marker hidden.
        # Craig asked what she remembered and got rows like
        #   - (unprompted — you spoke first) -> Did you say "mentioned how"?
        # which is db.remember_own_utterance()'s marker, added the same day,
        # leaking straight into a user-facing answer. The arrow format is
        # also the shape that was just removed from her prompt context for
        # inviting her to continue it; there is no reason to show it to him
        # either.
        lines = []
        for m in recent:
            said = (m["prompt"] or "").strip()
            replied = (m["response"] or "").strip()
            if said.startswith("(unprompted"):
                lines.append(f'- I said, unprompted: "{replied}"')
            else:
                lines.append(f'- You: "{said}"\n  Me: "{replied}"')

        return "Here's what I remember from our recent conversations:\n" + "\n".join(lines), state

    return "Ask me what I remember, or what I remember about a specific topic.", state
