# core/claims.py
"""
A claim needs evidence.

2026-09-21, Craig: "I don't want to be constantly chasing her mess up's.
So is there a way for her to understand she is wrong and self correct so
we're not constantly chasing things she's made up?"

That night she said "I checked" eight times about a project list she had
never read, and each time the sentence went into her memory and steered
the next turn. Telling her not to had been tried at every strength.

So this is code, not a rule. Every turn already knows exactly what she
looked up (the deliberation pass) and which tools she called. Before the
first clause of a reply is spoken, it is scanned for a claim of work —
"I checked", "I looked", "I read the log", "the file exists", "there are
no completed ones". A claim with nothing behind it is not spoken: the
lookups the question called for are run, a decisions row records the
slip, and she answers again with the real thing in front of her. The
false version is never stored, so it cannot echo.

What this does not catch: a wrong fact stated without claiming a check,
and a wrong opinion. Those are the model's ceiling; the harness is where
they are measured.
"""
import re

from config.logger_config import logger

# Claims of having done something in this turn. Conservative: each
# alternative is a phrasing she has actually used to claim work, and a
# false positive costs one regeneration (~5s), a false negative costs
# what tonight cost.
_CLAIM_RE = re.compile(
    r"\b(?:"
    r"i(?:'ve| have| already| just| did)?\s+(?:already\s+)?"
    r"(?:checked|looked(?: at| into| through)?|reviewed|read (?:through |over )?(?:the|your|my|his|it|that|this|every)|ran|run|scanned|"
    r"searched|verified|confirmed|examined|pulled(?: it)? up|went (?:through|over)|inspected|audited|"
    r"accessed|opened|retrieved|queried|consulted|fetched|loaded|cross-referenced|analy[sz]ed|processed the)\b"
    r"|(?:the |your |my )?(?:file|list|log|record|registry|database|queue|entr(?:y|ies)|stream)s? (?:exists?|shows?|says?|contains?|has|have|describes?|indicates?|confirms?)\b"
    r"|there (?:are|is) (?:no|nothing|zero) (?:completed|finished|done|tracked|recorded)\b"
    # 2026-09-23 (Craig: "she claimed to have vision of a room when I had
    # not turned the camera on" — "The camera is awake... I see nothing
    # but the empty room"): seeing is a claim of work like any other; the
    # look tool is the evidence. "I see." and "I see what you mean" are
    # not claims and are left alone.
    r"|(?:i (?:can|do|now) see|i see|i am seeing|i'm seeing|i(?:'m| am) looking at|i(?:'ve| have) (?:a )?(?:clear )?(?:view|visual) of)"
    r"(?=\s+(?!what|why|how|that|your point|the point|no\b|why)\w)"
    r"|(?:the |my |your )?camera (?:is (?:awake|on|live|active|up|open)|shows|sees|reveals|remains (?:on|live|active|open))\b"
    r"|(?:the |your )?(?:room|frame) (?:is|looks|appears) \w"
    r")",
    re.I,
)

_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)|\n")

# How much of the reply is held back and scanned before anything is
# spoken: the first two sentences, or this many characters if they run
# long. Two, because tonight's claims came second — "You want me to see
# my own projects? Fine, I checked." Costs nothing the listener notices:
# the first clause was never spoken before it was complete anyway.
HOLD_SENTENCES = 2
_MAX_UNCHECKED_CHARS = 240


def first_clause(buf: str):
    """(head, rest) once HOLD_SENTENCES sentences are complete or the head
    is long enough, else (None, buf)."""
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(buf)]
    if len(ends) >= HOLD_SENTENCES:
        cut = ends[HOLD_SENTENCES - 1]
        return buf[:cut], buf[cut:]
    if len(buf) >= _MAX_UNCHECKED_CHARS:
        return buf, ""
    return None, buf


_NO_EVIDENCE_STARTS = ("That is for my creator", "There is no tool", "nothing found", "nothing stored",
                       "no matches", "no stored conversations", "search_memory needs")


def real_evidence(block: str) -> bool:
    """Did the deliberation block carry anything she could answer from?
    2026-09-21 (found on the first test): a lookup that came back as a
    refusal — read_log for someone who is not the creator — still counted
    as 'she looked', and she went on to describe log entries she never
    saw. A refusal, an error or an empty result is not evidence."""
    if not block:
        return False
    for line in block.splitlines():
        if not line.startswith("["):
            continue
        result = line.split("]", 1)[1].strip() if "]" in line else ""
        low = result.lower()
        if not result or " failed: " in low or low.startswith(tuple(x.lower() for x in _NO_EVIDENCE_STARTS)):
            continue
        return True
    return False


def first_sentence(buf: str):
    """(sentence, rest) once ONE sentence is complete, else (None, buf).
    For the scan after the head, where each sentence is checked as it
    completes and spoken or replaced at once."""
    m = _SENTENCE_END_RE.search(buf)
    if m:
        return buf[:m.end()], buf[m.end():]
    if len(buf) >= _MAX_UNCHECKED_CHARS:
        return buf, ""
    return None, buf


def unbacked(clause: str, evidence: dict) -> list:
    """The claims in `clause` that nothing in `evidence` supports.
    evidence = {"lookups": bool, "tools": [names]}. Any lookup or tool
    call this turn backs any claim of work — the check is "did she look
    at all", not "did she look at the right thing"; the regenerated
    answer carries what she looked at, so the second question answers
    itself."""
    if not clause:
        return []
    if evidence.get("lookups") or evidence.get("tools"):
        return []
    return [m.group(0) for m in _CLAIM_RE.finditer(clause)]


# What to go and look at when a claim is caught: the turn's own need
# scores first (anything at 3 or more, top two), then what the words
# point at. The same read-only tools as everywhere else.
_HINTS = (
    (re.compile(r"\bproject", re.I), ("my_projects", {})),
    (re.compile(r"\b(score|test|suite|bad at)", re.I), ("my_scores", {})),
    (re.compile(r"\bmodule", re.I), ("list_modules", {})),
    (re.compile(r"\blog\b", re.I), ("read_log", {"lines": 30})),
    (re.compile(r"\b(diagnos|system check|systems?\b.*\b(work|online|up)\b)", re.I), ("run_diagnostics", {})),
    (re.compile(r"\b(remember|recall|said|told|mention|discuss|talked)", re.I), ("search_memory", None)),
    (re.compile(r"\b(what time|time is it|the time|the date|today|what day|clock|o'clock)\b", re.I), ("current_time", {})),
    (re.compile(r"\b(what do you see|can you see|do you see|look at|looking at|what am i (holding|wearing)|who is (here|there|this)|your camera|through the camera)\b", re.I), ("look", None)),
)

_RESOURCE_TOOL = {
    "memory": ("search_memory", None), "modules": ("list_modules", {}), "state": ("my_state", {}),
    "log": ("read_log", {"lines": 30}), "diagnostics": ("run_diagnostics", {}),
    "projects": ("my_projects", {}), "scores": ("my_scores", {}), "time": ("current_time", {}),
    "sight": ("look", None),
}


def lookups_after_claim(user_input: str, clause: str, needs: dict = None, max_lookups: int = 3) -> list:
    picked = []
    seen = set()

    def add(name, args):
        if name in seen:
            return
        if args is None:
            args = {"query": user_input}
        seen.add(name)
        picked.append((name, args))

    if isinstance(needs, dict):
        ranked = sorted(((needs.get(r, 0), r) for r in _RESOURCE_TOOL), reverse=True)
        for score, r in ranked[:2]:
            if score >= 3:
                add(*_RESOURCE_TOOL[r])
    text = f"{user_input} {clause}"
    for rx, (name, args) in _HINTS:
        if rx.search(text):
            add(name, args)
    return picked[:max_lookups]


_TOOL_NOUN = {"read_log": "my log", "read_my_source": "my source code", "my_state": "my own state",
              "search_memory": "our earlier conversations", "recent_turns": "our recent exchanges",
              "list_modules": "my modules", "run_diagnostics": "my diagnostics",
              "my_projects": "his projects", "my_scores": "my scores", "current_time": "the clock", "look": "my camera"}


def honest_lines(block: str) -> str:
    """The truth, written by code from what the lookups returned — for
    the case (seen on the first live test, 2026-09-21) where the second
    attempt STILL says "I accessed the system logs" with a refusal in
    front of it. One sentence per refused or empty lookup; nothing for a
    real result, because a real result backs the claim."""
    out = []
    for line in (block or "").splitlines():
        if not line.startswith("[") or "]" not in line:
            continue
        name = line[1:line.index("]")]
        result = line[line.index("]") + 1:].strip()
        low = result.lower()
        noun = _TOOL_NOUN.get(name, name)
        if low.startswith("that is for my creator"):
            out.append(f"I have not read {noun}; that is for my creator to see.")
        elif not result or " failed: " in low or low.startswith(("nothing stored", "no matches", "nothing found", "there is no tool")):
            out.append(f"I looked for {noun} on this and found nothing.")
    return " ".join(out)


def drop_claims(text: str) -> str:
    """The text without its claim sentences."""
    kept, start = [], 0
    for m in _SENTENCE_END_RE.finditer(text):
        sentence = text[start:m.end()]
        start = m.end()
        if not _CLAIM_RE.search(sentence):
            kept.append(sentence)
    tail = text[start:]
    if tail and not _CLAIM_RE.search(tail):
        kept.append(tail)
    return "".join(kept).strip()


async def gather_evidence(user_input: str, user_id: str, needs: dict, clause: str) -> str:
    """Runs the lookups and returns the context block for the second
    attempt. Never raises."""
    from core import tools as her_tools

    picked = lookups_after_claim(user_input, clause, needs)
    if not picked:
        return ("WHAT YOU LOOKED UP THIS TURN: nothing. There is no record, file, list or "
                "log in front of you.")
    parts = []
    for name, args in picked:
        try:
            result = await her_tools.run_tool(name, args, user_id)
        except Exception as e:
            result = f"{name} failed: {e}"
        parts.append(f"[{name}] {result}")
    logger.info(f"[CLAIM] looked up after the claim: {[n for n, _ in picked]}")
    return ("WHAT YOU LOOKED UP THIS TURN (real, just now — the only things you have "
            "checked; answer from this):\n" + "\n".join(parts))


# ---------------------------------------------------------------- sight
# 2026-09-23 (Craig: "She still does not seem to have knowledge of the
# camera"). She looked — the tool returned "A man with a beard sits in
# front of a bright window..." — and then said "The camera remains dark;
# I see only the void." The evidence was there; the check above passed
# her because she HAD looked. This is the one contradiction that is
# checkable by regex: a real picture in the evidence and a denial of
# sight in the clause.
_LOOK_LINE_RE = re.compile(r"^\[look\]\s*(.+)$", re.M)
_LOOK_NOTHING = ("has its eyes closed", "No page of", "could not make anything",
                 "cannot see anything", "nothing to look through")
_SIGHT_DENIAL_RE = re.compile(
    r"\b(?:"
    r"(?:the |my |your )?camera (?:remains|is|stays|is still) (?:dark|off|blind|closed|dead|down)"
    r"|i (?:can(?:not|'t)|cannot|do not|don't|am unable to) see"
    r"|(?:i )?see (?:only |nothing but |just )?(?:the )?(?:void|darkness|static|nothing|blackness)"
    r"|(?:there is |i have )?no (?:visual|image|frame|picture|feed|input) "
    r"|(?:your|the) eyes (?:are|remain|stay) (?:closed|shut)"
    r"|(?:i am|i'm) blind"
    r")\b",
    re.I,
)


def look_saw(looked_block: str) -> str:
    """The description a look returned this turn, or "" if she did not
    look or the look came back with nothing to see."""
    for m in _LOOK_LINE_RE.finditer(looked_block or ""):
        text = m.group(1).strip()
        if text and not any(x in text for x in _LOOK_NOTHING):
            return text
    return ""


def sight_denied(clause: str, looked_block: str) -> str:
    """The denial of sight in `clause` when a look this turn returned a
    real picture — "" otherwise. What is returned is the phrase she used."""
    saw = look_saw(looked_block)
    if not saw or not clause:
        return ""
    m = _SIGHT_DENIAL_RE.search(clause)
    return m.group(0) if m else ""
