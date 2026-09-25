# core/values.py
"""
What he values — from what he thanked and what he corrected.

2026-09-25, Craig: "let reflection conclude 'he values short answers' or
'he values a look before a claim', and carry those as standing
preferences measured by whether the behaviours recur. It shapes her
durably, so it needs your yes." — "I like this if we can implement it
without creating an issue."

The issue is sycophancy: a preference learned from agreement teaches her
to seek agreement. So nothing here reads agreement. The signals are his
thanks and his corrections, and what is kept about each is the SHAPE of
the reply it followed, not its content: how long it was, whether she
looked something up before answering, whether it ended on a question.
A preference exists only when the tally is lopsided — at least three
signals, seventy percent one way — and it says the numbers. Thirty days
of signals count; the rest fall away (core/retention.py).

Rendered into her prompt as WHAT HE VALUES, and shown at Her -> Health.
Nothing is judged by the model; this is arithmetic on his own acts.
"""
import time

SHORT_WORDS = 25            # a reply this short is "short"
LONG_WORDS = 60             # this long is "long"
MIN_SIGNALS = 3
LOPSIDED = 0.7
WINDOW_DAYS = 30


def reply_shape(text: str, looked: bool) -> dict:
    words = len((text or "").split())
    return {"words": words, "looked": bool(looked), "asked": (text or "").rstrip().endswith("?"),
            "t": time.time()}


def conclude(signals: list) -> list:
    """The preferences the signals support. Each signal: {"kind":
    "thanks"|"correction", "words", "looked", "asked"}. Returns plain
    lines with the numbers in them."""
    out = []
    thanks = [s for s in signals if s.get("kind") == "thanks"]
    corrections = [s for s in signals if s.get("kind") == "correction"]

    # length
    t_short = sum(1 for s in thanks if s.get("words", 0) <= SHORT_WORDS)
    t_long = sum(1 for s in thanks if s.get("words", 0) >= LONG_WORDS)
    c_long = sum(1 for s in corrections if s.get("words", 0) >= LONG_WORDS)
    c_short = sum(1 for s in corrections if s.get("words", 0) <= SHORT_WORDS)
    for_short = t_short + c_long
    for_long = t_long + c_short
    total = for_short + for_long
    if total >= MIN_SIGNALS:
        if for_short / total >= LOPSIDED:
            out.append(f"He values short answers (thanked you after {t_short} short replies; corrected {c_long} long ones).")
        elif for_long / total >= LOPSIDED:
            out.append(f"He values full answers (thanked you after {t_long} long replies; corrected {c_short} short ones).")

    # looking before answering
    t_looked = sum(1 for s in thanks if s.get("looked"))
    t_blind = len(thanks) - t_looked
    if len(thanks) >= MIN_SIGNALS:
        if t_looked / len(thanks) >= LOPSIDED:
            out.append(f"He values an answer with a look behind it (thanked you after {t_looked} replies where you had looked something up first).")

    # questions at the end
    c_asked = sum(1 for s in corrections if s.get("asked"))
    if len(corrections) >= MIN_SIGNALS and c_asked / len(corrections) >= LOPSIDED:
        out.append(f"He does not want a question at the end of a reply ({c_asked} of {len(corrections)} corrections followed one).")

    return out[:3]


def render(lines: list) -> str:
    if not lines:
        return ""
    return ("\n\n    WHAT HE VALUES (from what he thanked and what he corrected in the last "
            f"{WINDOW_DAYS} days; numbers, not opinion):\n" + "\n".join(f"    - {ln}" for ln in lines))


async def note_signal(kind: str, user: str, last_reply: dict):
    """His thanks or correction, with the shape of the reply it followed."""
    if not last_reply or kind not in ("thanks", "correction"):
        return
    if time.time() - float(last_reply.get("t", 0)) > 600:
        return          # a thanks ten minutes after the reply is not about it
    from db.db import add_value_signal
    await add_value_signal(user, kind, int(last_reply.get("words", 0)),
                           bool(last_reply.get("looked")), bool(last_reply.get("asked")))


async def lines_for(user: str) -> list:
    from db.db import fetch_value_signals
    return conclude(await fetch_value_signals(user, days=WINDOW_DAYS))
